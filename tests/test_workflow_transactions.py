"""PostgreSQL regressions for workflow commit boundaries; use the existing isolated fixture."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta

import pytest

from app.control.service import ControlService
from app.models import DeliveryState, FlowState, Job, JobKind, JobState, TriageVerdict, Verdict
from app.protocol import Succeeded, TriageResult, terminal_digest
from app.result_models import EffectState
from app.store import Store
from tests.review_memory import judge_decision
from tests.test_coordinator import RecordingGitHub, coordinator


def test_transaction_view_reuses_its_connection_without_leaking_to_other_callers(
    monkeypatch,
) -> None:
    """The transaction view must neither commit per method nor bind unrelated callers implicitly."""
    borrowed = []
    ended = []

    class Connection:
        @contextmanager
        def transaction(self):
            try:
                yield
            except RuntimeError:
                ended.append("rollback")
                raise

    class Pool:
        def __init__(self, **kwargs):
            pass

        def __class_getitem__(cls, item):
            return cls

        @contextmanager
        def connection(self):
            connection = Connection()
            borrowed.append(connection)
            yield connection

    monkeypatch.setattr("app.store.ConnectionPool", Pool)
    subject = Store("unused")
    with pytest.raises(RuntimeError, match="abort"), subject.transaction() as tx:
        with tx.connection() as first, tx.connection() as second:
            assert first is second
            with subject.connection() as unrelated:
                assert unrelated is not first
        raise RuntimeError("abort")
    assert len(borrowed) == 2 and ended == ["rollback"]
    with pytest.raises(RuntimeError, match="outside its context"), tx.connection():
        pass


def test_trigger_rolls_back_flow_and_job_on_failure(store: Store, monkeypatch) -> None:
    # Reservation without its first job would permanently suppress delivery retries.
    def fail_enqueue(*args: object, **kwargs: object) -> Job:
        raise RuntimeError("enqueue interrupted")

    monkeypatch.setattr(Store, "enqueue_job", fail_enqueue)
    with pytest.raises(RuntimeError, match="enqueue interrupted"):
        ControlService(store).on_trigger("owner/repo", 700, 42)
    assert store.get_active_flow("owner/repo", 700) is None


def test_concurrent_trigger_enqueues_once(store: Store) -> None:
    # Independent callers must not rely on the event loop serializing check/create/enqueue.
    service = ControlService(store)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.on_trigger("owner/repo", 701, 42), range(2)))
    flow = store.get_active_flow("owner/repo", 701)
    assert flow is not None
    assert sum(result is not None for result in results) == 1
    assert [job.kind for job in store.list_jobs(flow.id)] == [JobKind.INITIAL]


def test_assignment_skips_a_flow_locked_for_cancellation(store: Store):
    # The first assignment has no binding yet, so lifecycle serialization must include the Flow row.
    store.worker_connected("worker-a", 4, "epoch-a")
    flow = ControlService(store).on_trigger("owner/repo", 705, 42)
    assert flow is not None
    with store.transaction() as tx:
        assert tx.lock_workflow(flow.id) is not None
        assert store.assign_job("worker-a", "token-a", "epoch-a") is None
        ControlService(tx).cancel(flow)
    assert store.get_flow_execution(flow.id) is None


def test_retry_rolls_back_with_real_database(store: Store, monkeypatch) -> None:
    flow = store.create_flow("owner/repo", 702, 42)
    flow.state = FlowState.FAILED
    store.update_flow(flow)

    def fail_enqueue(*args: object, **kwargs: object) -> Job:
        raise RuntimeError("enqueue interrupted")

    monkeypatch.setattr(Store, "enqueue_job", fail_enqueue)
    with pytest.raises(RuntimeError, match="enqueue interrupted"):
        ControlService(store).retry(flow)
    persisted = store.get_flow(flow.id)
    assert persisted is not None and persisted.state is FlowState.FAILED
    assert store.list_jobs(flow.id) == []


def accepted_triage(store: Store, *, receive: bool = True) -> tuple[Job, TriageResult]:
    """Establish the same lifecycle binding a completed INITIAL leaves for its next job."""
    store.worker_connected("worker-a", 4, "epoch-a")
    flow = store.create_flow("owner/repo", 703, 42)
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    delivery = store.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.state, delivery.base_branch, delivery.pr_number = DeliveryState.OPEN, "trunk", 17
    store.update_delivery(delivery)
    initial = store.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.assign_job("worker-a", "initial-token", "epoch-a") is not None
    assert store.start_assignment(initial.id, attempt.id, "worker-a", "initial-token", "epoch-a")
    assert store.finish_assignment(
        initial.id, "worker-a", "initial-token", JobState.DONE, "epoch-a"
    )
    from app.models import ReviewItemKind
    from app.review_models import FeedbackUpdate

    store.reviews.ingest(flow.id, "review-event", "pull_request_review_comment", {},
        FeedbackUpdate(kind=ReviewItemKind.INLINE, github_id=101, author="alice",
                       body="Fix it", updated_at=store.now()),
        settle_seconds=0, submit_grace_seconds=0)
    store.reviews.claim_due()
    job = store.assign_job("worker-a", "triage-token", "epoch-a")
    assert job is not None
    assert store.start_assignment(job.id, attempt.id, "worker-a", "triage-token", "epoch-a")
    from tests.review_memory import review_context
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.freeze_context(job, review_context(store.reviews.for_job(job).id))
    result = TriageResult(
        batch_id=store.reviews.for_job(job).id,
        feedback_refs=list(store.reviews.for_job(job).feedback),
        decision=judge_decision(TriageVerdict(
            verdict=Verdict.REVISE,
            confidence=0.9,
            instruction="Fix it",
        ))
    )
    message = Succeeded(
        job_id=job.id,
        assignment_token="triage-token",
        connection_epoch="epoch-a",
        result=result,
    )
    if receive:
        assert (
            store.record_assignment_result(
                job.id,
                "worker-a",
                "triage-token",
                "epoch-a",
                message.model_dump(mode="json"),
                terminal_digest(message),
            )
            == "pending"
        )
    return job, result


def test_expired_cancelled_executor_does_not_overwrite_cancelled_flow(store: Store, monkeypatch):
    job, _ = accepted_triage(store, receive=False)
    flow = store.get_flow(job.flow_id)
    assert flow is not None
    ControlService(store).cancel(flow)
    running = store.get_job(job.id)
    assert running is not None and running.state is JobState.RUNNING
    store.worker_disconnected("worker-a", "offline", "epoch-a")
    later = store.now() + timedelta(minutes=10)
    monkeypatch.setattr(store, "_clock", lambda: later)
    assert store.sweep_expired_assignments() == (0, 0)
    execution = store.get_flow_execution(flow.id)
    assert execution is not None and execution.released_at is None
    cancelled = store.get_flow(job.flow_id)
    assert cancelled is not None and cancelled.state is FlowState.CANCELLED


async def test_result_and_derived_job_commit_together(store: Store, monkeypatch) -> None:
    # Fail after the final SQL update: the derived job and marker must both roll back in PostgreSQL.
    job, result = accepted_triage(store)
    github = RecordingGitHub()
    subject = coordinator(store, github)
    original = Store.complete_persisted_result

    def interrupted(self, job_id, state):
        original(self, job_id, state)
        raise RuntimeError("commit interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "complete_persisted_result", interrupted)
        with pytest.raises(RuntimeError, match="commit interrupted"):
            await subject.apply_result(job, result)
    pending = store.get_job(job.id)
    assert pending is not None and pending.result_applied_at is None
    assert all(item.kind is not JobKind.REVISION for item in store.list_jobs(job.flow_id))
    assert store.results.list_effects(job.id)[0].state is EffectState.SUCCEEDED
    await subject.apply_result(job, result)
    await subject.apply_result(job, result)
    derived = [item for item in store.list_jobs(job.flow_id) if item.kind is JobKind.REVISION]
    assert len(derived) == len(github.comments) == 1
    assert derived[0].source_job_id == job.id
    assert derived[0].source_key == "revision"
    assert (
        store.enqueue_job(
            job.flow_id,
            JobKind.REVISION,
            {"batch_id": result.batch_id, "instruction": "Fix it"},
            source_job_id=job.id,
            source_key="revision",
        ).id
        == derived[0].id
    )


async def test_accepted_result_survives_expired_worker_recovery(store: Store, monkeypatch) -> None:
    # Once control accepted the terminal result, an offline executor cannot invalidate its delivery.
    job, result = accepted_triage(store)
    store.worker_disconnected("worker-a", "connection lost", "epoch-a")
    later = store.now() + timedelta(minutes=10)
    monkeypatch.setattr(store, "_clock", lambda: later)
    assert store.sweep_expired_assignments() == (0, 0)
    flow = store.get_flow(job.flow_id)
    assert flow is not None and flow.state is FlowState.ACTIVE
    await coordinator(store, RecordingGitHub()).apply_result(job, result)
    finished = store.get_job(job.id)
    assert finished is not None and finished.state is JobState.DONE
    execution = store.get_flow_execution(job.flow_id)
    assert execution is not None and execution.recovery_deadline is not None
    assert execution.recovery_deadline > later
