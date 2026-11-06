"""Public persistence behavior for inference through the ordinary Job allocator."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.contracts.protocol import PROTOCOL_VERSION, Failed, InferenceResult, terminal_digest
from app.contracts.receipts import ResultInbox, decode_result_inbox
from app.domain.inference_models import (
    InferenceCapability,
    InferenceFailure,
    InferenceMessage,
    InferenceOutput,
    InferenceTask,
)
from app.domain.models import DeliveryState, FlowState, JobKind, JobState, WorkerStatus, utcnow
from app.persistence.database import Database
from app.persistence.inference_notices import InferenceNoticeStore
from app.persistence.jobs import JobStore
from app.persistence.schema import SCHEMAS
from tests.test_review_batches import claim, feedback, ingest
from tests.test_review_batches import review_flow as review_flow  # noqa: PLC0414

LOCAL = "__local_bub__"


def task(**changes):
    return InferenceTask(messages=[InferenceMessage(role="user", content="Summarize this.")],
                         **changes)


def ready(store, worker="worker", *, execute=True, capability=None):
    store.workers.worker_connected(worker, PROTOCOL_VERSION, "epoch", execute=execute)
    store.workers.worker_ready(worker, "epoch", capability or InferenceCapability(adapter_version="amp/v1"))


def result(job, failure=None):
    current = job.inference.current_task
    return InferenceResult(
        type="tool_result", method="worker.infer", message_id=uuid4().hex,
        reply_to=current.request_id, job_id=job.id, assignment_token=job.assignment_token,
        failure=InferenceFailure(code=failure) if failure else None,
        output=None if failure else InferenceOutput(
            request_id=current.request_id, request_digest=current.digest,
            text="An answer.", adapter_version="amp/v1",
        ),
    )


def finish(store, job, failure=None):
    assert store.workers.record_assignment_result(
        job.id, job.assigned_worker_id, job.assignment_token, "epoch", result(job, failure),
    ) == "pending"
    assert store.workers.complete_inference_result(job.id)
    return store.jobs.get_job(job.id)


def test_local_preference_is_transactional_and_busy_falls_back(store):
    ready(store, LOCAL, execute=False)
    store.jobs.enqueue_inference(task())
    finish(store, store.workers.assign_job(LOCAL, "warmup", "epoch", slot="infer"))
    ready(store, "external")
    first = store.jobs.enqueue_inference(task())
    # External is first to poll and has waited longer; local preference still wins.
    assert store.workers.assign_job("external", "external-first", "epoch", slot="infer") is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda token: store.workers.assign_job(LOCAL, token, "epoch", slot="infer"),
                               ["local-one", "local-two"]))
    assigned, = [item for item in claims if item is not None]
    assert assigned.id == first.id and assigned.attempt == 1
    second = store.jobs.enqueue_inference(task())
    assert store.workers.assign_job("external", "fallback", "epoch", slot="infer").id == second.id


@pytest.mark.parametrize("condition", ["draining", "offline", "unready", "mismatch", "excluded"])
def test_ineligible_local_does_not_reserve_the_queue(store, condition):
    ready(store, "external")
    ready(store, LOCAL, execute=False)
    job = store.jobs.enqueue_inference(task())
    if condition == "draining":
        store.workers.drain(LOCAL, True)
    elif condition == "offline":
        store.workers.worker_disconnected(LOCAL, connection_epoch="epoch")
    elif condition == "unready":
        store.workers.worker_connected(LOCAL, PROTOCOL_VERSION, "next", execute=False)
    elif condition == "mismatch":
        store.workers.worker_ready(LOCAL, "epoch", InferenceCapability(adapter_version="bub/v1", max_output_bytes=1))
    else:
        assigned = store.workers.assign_job(LOCAL, "one", "epoch", slot="infer")
        assert finish(store, assigned, "busy").state == JobState.QUEUED
    assert store.workers.assign_job("external", "two", "epoch", slot="infer").id == job.id


def test_repo_free_inference_and_coding_have_independent_slots(store):
    ready(store, LOCAL, execute=False)
    ready(store)
    flow = store.flows.create_flow("owner/repo", 1, None)
    coding = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    # An infer-only worker sorted first must not starve the coding fairness selector.
    assert store.workers.assign_job(LOCAL, "forbidden", "epoch") is None
    assert store.workers.assign_job("worker", "coding", "epoch").id == coding.id
    store.workers.drain(LOCAL, True)
    generic = store.jobs.enqueue_inference(task())
    inference = store.workers.assign_job("worker", "inference", "epoch", slot="infer")
    assert inference.id == generic.id and inference.flow_id is None
    binding = store.workers.get_flow_execution(flow.id)
    assert finish(store, inference).state == JobState.DONE
    worker = next(w for w in store.workers.list_workers() if w.worker_id == "worker")
    assert worker.status == WorkerStatus.BUSY and worker.current_job_id == coding.id
    assert worker.current_inference_job_id is None
    assert store.workers.get_flow_execution(flow.id) == binding


def test_request_identity_and_budget_survive_restart_without_fourth_attempt(store):
    original = task(timeout_seconds=600)
    job = store.jobs.enqueue_inference(original)
    ids = []
    for index in range(3):
        worker = f"worker-{index}"
        ready(store, worker)
        assigned = store.workers.assign_job(worker, f"token-{index}", "epoch", slot="infer")
        ids.append(assigned.inference.current_task.request_id)
        assert assigned.attempt == index + 1
        store.workers.recover_abandoned_assignments()
        saved = store.jobs.enqueue_inference(original)
        assert saved.id == job.id and saved.inference.deadline_at == job.inference.deadline_at
        assert saved.inference.task == original
    failed = store.jobs.get_job(job.id)
    assert failed.state == JobState.FAILED and failed.inference.summary.attempts == 3
    assert failed.inference.summary.last_failure_code == "connection_closed"
    assert len(set(ids + [original.request_id])) == 4
    ready(store, "fourth")
    assert store.workers.assign_job("fourth", "fourth", "epoch", slot="infer") is None


def test_raw_result_is_fenced_and_recovered_without_another_attempt(store):
    ready(store)
    store.jobs.enqueue_inference(task())
    job = store.workers.assign_job("worker", "token", "epoch", slot="infer")
    receipt = result(job)
    for token, epoch in [("wrong", "epoch"), ("token", "old")]:
        assert store.workers.record_assignment_result(job.id, "worker", token, epoch, receipt) == "rejected"
    wrong_reply = receipt.model_copy(update={"reply_to": uuid4().hex})
    assert store.workers.record_assignment_result(job.id, "worker", "token", "epoch", wrong_reply) == "rejected"
    assert store.workers.record_assignment_result(job.id, "worker", "token", "epoch", receipt) == "pending"
    store.workers.recover_abandoned_assignments()
    assert [j.id for j in store.jobs.list_unapplied_results()] == [job.id]
    assert store.workers.complete_inference_result(job.id)
    saved = store.jobs.get_job(job.id)
    assert saved.state == JobState.DONE and saved.attempt == 1
    assert InferenceResult.model_validate(saved.result_payload) == receipt
    assert not store.workers.complete_inference_result(job.id)
    assert store.workers.record_assignment_result(job.id, "worker", "token", "epoch", receipt) == "rejected"


def test_deadline_requests_cleanup_without_touching_coding(store, monkeypatch):
    ready(store)
    now = store.now()
    monkeypatch.setattr(store.database, "_clock", lambda: now)
    flow = store.flows.create_flow("owner/repo", 1, None)
    coding = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    store.workers.assign_job("worker", "coding", "epoch")
    store.jobs.enqueue_inference(task(timeout_seconds=1), flow_id=flow.id)
    job = store.workers.assign_job("worker", "infer", "epoch", slot="infer")
    now += timedelta(seconds=2)
    store.workers.sweep_expired_assignments()
    assert store.workers.inference_needs_cancel(job.id)
    assert store.jobs.get_job(job.id).state == JobState.RUNNING
    assert store.workers.list_workers()[0].current_inference_job_id == job.id
    failed = finish(store, job, "cancelled")
    assert failed.inference.summary.last_failure_code == "execution_timeout"
    assert store.jobs.get_job(coding.id).state == JobState.RUNNING
    assert store.flows.get_flow(flow.id).state == FlowState.ACTIVE
    assert store.workers.list_workers()[0].current_job_id == coding.id


def test_review_round_identity_and_notice_are_durable_and_deduplicated(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = claim(store, clock)[0]
    original = task()
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: store.jobs.enqueue_inference(
            task(), flow_id=flow.id, batch_id=batch.id, validation="review"), range(2)))
    first = jobs[0]
    assert jobs[1].id == first.id
    assert store.reviews.get_batch(batch.id).job_id is None
    assert store.reviews.get_batch(batch.id).inference_job_id == first.id
    assert store.reviews.current(batch)
    regenerated = InferenceTask(
        messages=[InferenceMessage(role="user", content="A regenerated review prompt.")],
        output_schema={"type": "object"}, timeout_seconds=900,
    )
    replay = store.jobs.enqueue_inference(regenerated, flow_id=flow.id, batch_id=batch.id, validation="review")
    assert replay.id == first.id and replay.inference == first.inference
    for index in range(3):
        ready(store, f"worker-{index}")
        assigned = store.workers.assign_job(f"worker-{index}", f"token-{index}", "epoch", slot="infer")
        finish(store, assigned, "execution_timeout")
    failed = store.jobs.enqueue_inference(original, flow_id=flow.id, batch_id=batch.id, validation="review")
    assert failed.id == first.id and failed.state == JobState.FAILED
    notices = InferenceNoticeStore(store.database)
    notice, = notices.page(flow.id).items
    assert notice.attempts == 3
    assert not notices.append(flow.id, batch.id, failed.inference.summary)
    assert store.jobs.latest_failed_job(flow.id) is None
    store.jobs.consume_inference(first.id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        next_round = list(pool.map(lambda _: store.jobs.enqueue_inference(
            task(), flow_id=flow.id, batch_id=batch.id, validation="review"), range(2)))
    assert next_round[0].id == next_round[1].id != first.id
    assert next_round[0].inference.task.request_id != failed.inference.task.request_id
    assert next_round[0].attempt == 0
    snapshot = notices.page(flow.id).snapshot
    for index in range(3):
        assigned = store.workers.assign_job(f"worker-{index}", f"round-two-{index}", "epoch", slot="infer")
        finish(store, assigned, "execution_timeout")
    updated = notices.page(flow.id)
    assert updated.snapshot > snapshot and len(updated.items) == 1
    assert updated.items[0].key == notice.key


def test_cancel_retains_running_slot_and_does_not_block_archive_or_reopen(store):
    ready(store)
    flow = store.flows.create_flow("owner/repo", 1, None)
    queued = store.jobs.enqueue_inference(task(), flow_id=flow.id)
    running = store.workers.assign_job("worker", "token", "epoch", slot="infer")
    pending = store.jobs.enqueue_inference(task(), flow_id=flow.id)
    store.jobs.cancel_jobs(flow.id)
    assert store.jobs.get_job(pending.id).state == JobState.FAILED
    assert store.jobs.get_job(queued.id).state == JobState.RUNNING
    assert store.workers.inference_needs_cancel(running.id)
    assert not store.jobs.reopen_blocked(flow.id)
    assert not store.jobs.has_unsettled_jobs(flow.id, excluding_job_id=0)
    assert finish(store, running, "cancelled").state == JobState.FAILED


@pytest.mark.parametrize("failure", ["authentication", "configuration", "invalid_response", "cancelled"])
def test_consumed_permanent_failure_never_creates_another_review_round(store, review_flow, failure):
    # A crash after observing the error but before saving WAIT must not retry a permanent failure.
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = claim(store, clock)[0]
    first = store.jobs.enqueue_inference(task(), flow_id=flow.id, batch_id=batch.id, validation="review")
    ready(store)
    assigned = store.workers.assign_job("worker", "one", "epoch", slot="infer")
    finish(store, assigned, failure)
    store.jobs.consume_inference(first.id)
    again = store.jobs.enqueue_inference(task(), flow_id=flow.id, batch_id=batch.id, validation="review")
    assert again.id == first.id and again.attempt == 1
    assert len(store.jobs.list_jobs(flow.id)) == 1


def test_queue_deadline_and_provider_backoff_do_not_consume_attempts(store, monkeypatch):
    now = store.now()
    monkeypatch.setattr(store.database, "_clock", lambda: now)
    never_sent = store.jobs.enqueue_inference(task(timeout_seconds=1))
    now += timedelta(seconds=2)
    store.workers.sweep_expired_assignments()
    expired = store.jobs.get_job(never_sent.id)
    assert expired.state == JobState.FAILED and expired.attempt == 0
    assert not expired.inference.summary.notify
    ready(store)
    original = task(timeout_seconds=10)
    store.jobs.enqueue_inference(original)
    assigned = store.workers.assign_job("worker", "one", "epoch", slot="infer")
    receipt = result(assigned, "rate_limited").model_copy(update={
        "failure": InferenceFailure(code="rate_limited", retry_after_seconds=3),
    })
    store.workers.record_assignment_result(assigned.id, "worker", "one", "epoch", receipt)
    store.workers.complete_inference_result(assigned.id)
    now += timedelta(seconds=2)
    store.workers.recover_abandoned_assignments()
    ready(store)
    assert store.workers.assign_job("worker", "early", "epoch", slot="infer") is None
    with pytest.raises(ValueError, match="logical inference request"):
        store.jobs.enqueue_inference(original.model_copy(update={"timeout_seconds": 900}))
    same = store.jobs.enqueue_inference(original)
    assert same.inference.task == original and same.attempt == 1
    now += timedelta(seconds=1)
    second = store.workers.assign_job("worker", "two", "epoch", slot="infer")
    assert second.attempt == 2 and second.inference.current_task.timeout_seconds == 7
    finish(store, second, "execution_timeout")
    now += timedelta(seconds=8)
    store.workers.sweep_expired_assignments()
    # An old applied receipt must not keep a requeued call alive past its original deadline.
    assert store.jobs.get_job(second.id).state == JobState.FAILED
    assert store.workers.assign_job("worker", "late", "epoch", slot="infer") is None


def test_disconnect_excludes_unknown_worker_and_old_epoch_cannot_release_new_slot(store):
    ready(store, "a")
    ready(store, "b")
    first = store.jobs.enqueue_inference(task())
    assigned = store.workers.assign_job("a", "old", "epoch", slot="infer")
    store.workers.worker_connected("a", PROTOCOL_VERSION, "new")
    assert store.jobs.get_job(first.id).state == JobState.QUEUED
    store.workers.worker_ready("a", "new", InferenceCapability(adapter_version="amp/v1"))
    assert store.workers.assign_job("a", "repeat", "new", slot="infer") is None
    replacement = store.workers.assign_job("b", "next", "epoch", slot="infer")
    assert replacement.id == first.id and replacement.attempt == 2
    second = store.jobs.enqueue_inference(task())
    assert store.workers.assign_job("a", "new-call", "new", slot="infer").id == second.id
    store.workers.worker_disconnected("a", connection_epoch="epoch")
    assert store.jobs.get_job(second.id).state == JobState.RUNNING
    assert store.workers.record_assignment_result(first.id, "a", "old", "epoch", result(assigned)) == "rejected"


def test_notice_write_failure_keeps_inbox_and_attempt_budget_for_application_retry(store, review_flow, monkeypatch):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = claim(store, clock)[0]
    job = store.jobs.enqueue_inference(task(), flow_id=flow.id, batch_id=batch.id, validation="review")
    ready(store)
    for index in range(2):
        finish(store, store.workers.assign_job("worker", f"token-{index}", "epoch", slot="infer"), "execution_timeout")
    final = store.workers.assign_job("worker", "third", "epoch", slot="infer")
    store.workers.record_assignment_result(final.id, "worker", "third", "epoch", result(final, "execution_timeout"))

    def unavailable(*args):
        raise RuntimeError("notice storage interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(InferenceNoticeStore, "append", unavailable)
        with pytest.raises(RuntimeError, match="notice storage"):
            store.workers.complete_inference_result(final.id)
    stored = store.jobs.get_job(job.id)
    assert stored.attempt == 3 and stored.state == JobState.RUNNING and stored.result_payload is not None
    assert store.jobs.enqueue_inference(task(), flow_id=flow.id, batch_id=batch.id).id == job.id
    assert store.workers.complete_inference_result(job.id)
    assert store.jobs.get_job(job.id).state == JobState.FAILED
    assert len(InferenceNoticeStore(store.database).page(flow.id).items) == 1


def test_backend_identity_and_caller_validation_fail_inside_the_attempt(store):
    ready(store)
    job = store.jobs.enqueue_inference(task())
    assigned = store.workers.assign_job("worker", "token", "epoch", slot="infer")
    receipt = result(assigned)
    receipt = receipt.model_copy(update={"output": receipt.output.model_copy(update={"adapter_version": "other/v1"})})
    assert store.workers.record_assignment_result(job.id, "worker", "token", "epoch", receipt) == "pending"
    assert store.workers.complete_inference_result(job.id)
    failed = store.jobs.get_job(job.id)
    assert failed.inference.summary.last_failure_code == "invalid_response" and failed.attempt == 1
    assert InferenceResult.model_validate(failed.result_payload) == receipt


def test_on_time_result_survives_deadline_and_source_retirement_requires_cleanup(store, review_flow):
    flow, clock = review_flow
    ready(store)
    store.jobs.enqueue_inference(task(timeout_seconds=1))
    assigned = store.workers.assign_job("worker", "generic", "epoch", slot="infer")
    store.workers.record_assignment_result(assigned.id, "worker", "generic", "epoch", result(assigned))
    clock.now += timedelta(seconds=2)
    store.workers.recover_abandoned_assignments()
    assert store.workers.complete_inference_result(assigned.id)
    assert store.jobs.get_job(assigned.id).state == JobState.DONE
    ready(store)
    ingest(store, flow, feedback(clock))
    batch = claim(store, clock)[0]
    store.jobs.enqueue_inference(task(), flow_id=flow.id, batch_id=batch.id, validation="review")
    assigned = store.workers.assign_job("worker", "review", "epoch", slot="infer")
    store.reviews.cancel(flow.id)
    assert store.workers.inference_needs_cancel(assigned.id)
    assert store.workers.list_workers()[0].current_inference_job_id == assigned.id
    assert finish(store, assigned).inference.summary.last_failure_code == "cancelled"
    assert store.flows.get_flow(flow.id).state == FlowState.ACTIVE


def test_schema_upgrade_is_repeatable_and_preserves_coding_receipts(postgresql_url):
    db = Database(postgresql_url)
    try:
        now = utcnow()
        terminal = Failed(job_id=1, assignment_token="old-token", error="old coding failure")
        inbox = ResultInbox(source="worker", terminal=terminal)
        digest = terminal_digest(terminal)
        with db.connection() as connection:
            for schema in SCHEMAS[:-1]:
                connection.execute(schema)
            connection.execute(
                "INSERT INTO flows(id,repo,issue_number,state,created_at,updated_at) VALUES (1,'o/r',1,'active',%s,%s)",
                (now, now),
            )
            connection.execute(
                "INSERT INTO jobs(id,flow_id,kind,state,assignment_token,result_payload,result_digest,"
                "result_received_at,created_at,updated_at) VALUES (1,1,'initial','running','old-token',%s,%s,%s,%s,%s)",
                (Jsonb(inbox.model_dump(mode="json")), digest, now, now, now),
            )
        db.init_db()
        db.init_db()
        saved = JobStore(db).get_job(1)
        assert decode_result_inbox(saved.result_payload, job_id=1, assignment_token="old-token",
                                   result_digest=saved.result_digest) == inbox
        assert saved.inference is None and saved.required_flow_id == 1
        with db.connection() as connection:
            connection.execute("SELECT setval(pg_get_serial_sequence('jobs','id'),1)")
        generic = JobStore(db).enqueue_inference(task())
        assert generic.flow_id is None
        with pytest.raises(ValueError, match="coding Flow authority"):
            _ = generic.required_flow_id
    finally:
        db.close()


def test_workflow_lock_rediscovers_a_concurrent_new_inference_owner(store, monkeypatch):
    # Discovery can see no owner immediately before another transaction claims a job.
    # The workflow must release/retry, not take that newly published worker after Flow.
    ready(store)
    flow = store.flows.create_flow("owner/race", 1, None)
    job = store.jobs.enqueue_inference(task(), flow_id=flow.id)
    discovered, assigned = Event(), Event()
    execute = psycopg.Connection.execute
    paused = False

    def observe(connection, query, *args, **kwargs):
        nonlocal paused
        cursor = execute(connection, query, *args, **kwargs)
        if not paused and query.startswith("SELECT worker_id FROM workers WHERE worker_id IN") and "FOR UPDATE" in query:
            paused = True
            discovered.set()
            assert assigned.wait(5)
        return cursor

    monkeypatch.setattr(psycopg.Connection, "execute", observe)

    def cancel():
        store.jobs.cancel_jobs(flow.id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cancel)
        assert discovered.wait(5)
        try:
            assert store.workers.assign_job("worker", "claim", "epoch", slot="infer").id == job.id
        finally:
            assigned.set()
        future.result(timeout=5)
    assert store.workers.inference_needs_cancel(job.id)
    assert store.workers.list_workers()[0].current_inference_job_id == job.id


def test_inference_heartbeat_is_fenced_and_never_renews_total_budget(store, monkeypatch):
    now = store.now()
    monkeypatch.setattr(store.database, "_clock", lambda: now)
    ready(store)
    store.jobs.enqueue_inference(task(timeout_seconds=5))
    job = store.workers.assign_job("worker", "token", "epoch", slot="infer")
    deadline = job.inference.deadline_at
    now += timedelta(seconds=4)
    assert not store.workers.heartbeat_assignment(job.id, "worker", "wrong", "epoch")
    assert not store.workers.heartbeat_assignment(job.id, "worker", "token", "old")
    assert store.workers.heartbeat_assignment(job.id, "worker", "token", "epoch")
    saved = store.jobs.get_job(job.id)
    assert saved.heartbeat_at == now
    assert saved.lease_expires_at == saved.inference.deadline_at == deadline
    assert store.workers.list_workers()[0].last_seen_at == now
    now += timedelta(seconds=2)
    store.workers.sweep_expired_assignments()
    assert store.workers.inference_needs_cancel(job.id)


def test_provider_retry_after_beyond_budget_is_terminal_without_datetime_overflow(store):
    ready(store)
    store.jobs.enqueue_inference(task())
    job = store.workers.assign_job("worker", "token", "epoch", slot="infer")
    receipt = result(job, "rate_limited").model_copy(update={
        "failure": InferenceFailure(code="rate_limited", retry_after_seconds=1e300),
    })
    store.workers.record_assignment_result(job.id, "worker", "token", "epoch", receipt)
    assert store.workers.complete_inference_result(job.id)
    saved = store.jobs.get_job(job.id)
    assert saved.state == JobState.FAILED and saved.attempt == 1
    assert saved.inference.summary.stop_reason == "deadline"
    assert saved.inference.summary.last_failure_code == "rate_limited"


def test_inference_does_not_block_archive_and_coding_completion_keeps_inference_slot(store):
    ready(store)
    flow = store.flows.create_flow("owner/repo", 1, None)
    initial = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    store.workers.assign_job("worker", "initial", "epoch")
    store.workers.finish_assignment(initial.id, "worker", "initial", JobState.DONE, "epoch")
    store.jobs.enqueue_inference(task(), flow_id=flow.id)
    inference = store.workers.assign_job("worker", "infer", "epoch", slot="infer")
    delivery = store.deliveries.get_or_create_delivery(flow.id, "flow/1")
    delivery.state = DeliveryState.MERGED
    store.deliveries.update_delivery(delivery)
    archive = store.jobs.enqueue_job(flow.id, JobKind.ARCHIVE)
    assert store.workers.assign_job("worker", "archive", "epoch").id == archive.id
    store.workers.finish_assignment(archive.id, "worker", "archive", JobState.DONE, "epoch")
    worker, = store.workers.list_workers()
    assert worker.current_job_id is None and worker.current_inference_job_id == inference.id
    assert worker.status == WorkerStatus.ONLINE
    assert store.workers.get_flow_execution(flow.id).released_at is None


@pytest.mark.parametrize("changed", ["messages", "schema", "output_limit", "timeout", "validation", "flow"])
def test_generic_request_id_cannot_return_an_answer_for_a_different_call(store, changed):
    # Reusing an ID after success must not silently answer new input with an old output.
    original = task()
    queued = store.jobs.enqueue_inference(original)
    ready(store)
    assigned = store.workers.assign_job("worker", "token", "epoch", slot="infer")
    completed = finish(store, assigned)
    incoming, validation, flow_id = original, "text", None
    if changed == "messages":
        incoming = original.model_copy(update={
            "messages": [InferenceMessage(role="user", content="A different question.")],
        })
    elif changed == "schema":
        incoming = original.model_copy(update={"output_schema": {"type": "object"}})
    elif changed == "output_limit":
        incoming = original.model_copy(update={"max_output_bytes": 100})
    elif changed == "timeout":
        incoming = original.model_copy(update={"timeout_seconds": 900})
    elif changed == "validation":
        validation = "review"
    else:
        flow_id = store.flows.create_flow("owner/repo", 1, None).id
    with pytest.raises(ValueError, match="logical inference request"):
        store.jobs.enqueue_inference(incoming, flow_id=flow_id, validation=validation)
    replay = store.jobs.enqueue_inference(original)
    assert replay == completed and replay.id == queued.id
    assert replay.inference.deadline_at == queued.inference.deadline_at
