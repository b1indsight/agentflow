"""PostgreSQL regressions for workflow commit boundaries; use the existing isolated fixture."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from threading import Barrier

import pytest

from app.contracts.protocol import PROTOCOL_VERSION, RevisionTask, Succeeded, terminal_digest
from app.controler.workflow.service import ControlService
from app.domain.models import (
    DeliveryState,
    FlowState,
    Job,
    JobKind,
    JobState,
    ReviewItemKind,
)
from app.domain.review_models import FeedbackUpdate
from app.persistence.jobs import JobStore
from app.persistence.store import Store
from tests.github import RecordingGitHub
from tests.review_memory import review_context
from tests.test_coordinator import coordinator
from tests.test_revision_delivery import verified_result


def test_functional_store_cannot_escape_its_transaction(store):
    # A service retaining a transaction's child repository must not borrow a fresh connection later.
    with store.database.transaction() as tx:
        flows = store.flows.using(tx)
        flow = flows.create_flow("owner/repo", 707, 42)
    with pytest.raises(RuntimeError, match="outside its context"):
        flows.get_flow(flow.id)


def test_owned_repository_keeps_its_connection_scope_on_rollback(store):
    # Binding a transaction must not enroll a previously retained ordinary repository in it.
    flows = store.flows
    with pytest.raises(RuntimeError, match="abort transaction"), store.database.transaction() as tx:
        rolled_back = flows.using(tx).create_flow("owner/repo", 708, 42)
        committed = flows.create_flow("owner/repo", 709, 42)
        raise RuntimeError("abort transaction")
    assert flows.get_flow(rolled_back.id) is None
    assert flows.get_flow(committed.id) is not None


def test_trigger_rolls_back_flow_and_job_on_failure(store: Store, monkeypatch) -> None:
    # Reservation without its first job would permanently suppress delivery retries.
    def fail_enqueue(*args: object, **kwargs: object) -> Job:
        raise RuntimeError("enqueue interrupted")

    monkeypatch.setattr(JobStore, "enqueue_job", fail_enqueue)
    with pytest.raises(RuntimeError, match="enqueue interrupted"):
        ControlService(
            database=store.database,
            deliveries=store.deliveries,
            flows=store.flows,
            jobs=store.jobs,
            result_store=store.results,
            reviews=store.reviews,
            stops=store.stops,
            webhook_store=store.webhooks,
            workers=store.workers,
        ).on_trigger("owner/repo", 700, 42)
    assert store.flows.get_active_flow("owner/repo", 700) is None


def test_assignment_skips_a_flow_locked_for_cancellation(store: Store):
    # The first assignment has no binding yet, so lifecycle serialization must include the Flow row.
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "epoch-a")
    flow = ControlService(
        database=store.database,
        deliveries=store.deliveries,
        flows=store.flows,
        jobs=store.jobs,
        result_store=store.results,
        reviews=store.reviews,
        stops=store.stops,
        webhook_store=store.webhooks,
        workers=store.workers,
    ).on_trigger("owner/repo", 705, 42)
    assert flow is not None
    with store.transaction() as tx:
        assert tx.lock_workflow(flow.id) is not None
        assert store.workers.assign_job("worker-a", "token-a", "epoch-a") is None
        ControlService(
            database=tx.database,
            deliveries=tx.deliveries,
            flows=tx.flows,
            jobs=tx.jobs,
            result_store=tx.results,
            reviews=tx.reviews,
            stops=tx.stops,
            webhook_store=tx.webhooks,
            workers=tx.workers,
        ).cancel(flow)
    assert store.workers.get_flow_execution(flow.id) is None


def test_retry_rolls_back_with_real_database(store: Store, monkeypatch) -> None:
    flow = store.flows.create_flow("owner/repo", 702, 42)
    flow.state = FlowState.FAILED
    store.flows.update_flow(flow)

    def fail_enqueue(*args: object, **kwargs: object) -> Job:
        raise RuntimeError("enqueue interrupted")

    monkeypatch.setattr(JobStore, "enqueue_job", fail_enqueue)
    with pytest.raises(RuntimeError, match="enqueue interrupted"):
        ControlService(
            database=store.database,
            deliveries=store.deliveries,
            flows=store.flows,
            jobs=store.jobs,
            result_store=store.results,
            reviews=store.reviews,
            stops=store.stops,
            webhook_store=store.webhooks,
            workers=store.workers,
        ).retry(flow)
    persisted = store.flows.get_flow(flow.id)
    assert persisted is not None and persisted.state is FlowState.FAILED
    assert store.jobs.list_jobs(flow.id) == []


def test_trigger_and_operator_retry_share_issue_reservation(store: Store, postgresql_url: str):
    # CLI retry uses its own Store and can race the webhook consumer on the same issue.
    flow = ControlService(
        database=store.database,
        deliveries=store.deliveries,
        flows=store.flows,
        jobs=store.jobs,
        result_store=store.results,
        reviews=store.reviews,
        stops=store.stops,
        webhook_store=store.webhooks,
        workers=store.workers,
    ).on_trigger("owner/repo", 706, 42)
    assert flow is not None
    ControlService(
        database=store.database,
        deliveries=store.deliveries,
        flows=store.flows,
        jobs=store.jobs,
        result_store=store.results,
        reviews=store.reviews,
        stops=store.stops,
        webhook_store=store.webhooks,
        workers=store.workers,
    ).cancel(flow)
    barrier = Barrier(2)

    with closing(Store(postgresql_url)) as operator, ThreadPoolExecutor(max_workers=2) as pool:
        def trigger():
            barrier.wait(timeout=5)
            return ControlService(
                database=store.database,
                deliveries=store.deliveries,
                flows=store.flows,
                jobs=store.jobs,
                result_store=store.results,
                reviews=store.reviews,
                stops=store.stops,
                webhook_store=store.webhooks,
                workers=store.workers,
            ).on_trigger(flow.repo, flow.issue_number, 42)

        def retry():
            barrier.wait(timeout=5)
            try:
                ControlService(
                    database=operator.database,
                    deliveries=operator.deliveries,
                    flows=operator.flows,
                    jobs=operator.jobs,
                    result_store=operator.results,
                    reviews=operator.reviews,
                    stops=operator.stops,
                    webhook_store=operator.webhooks,
                    workers=operator.workers,
                ).retry(flow)
            except ValueError as error:
                assert "already active" in str(error)
                return False
            return True

        triggered = pool.submit(trigger)
        retried = pool.submit(retry)
        created, retry_won = triggered.result(timeout=5), retried.result(timeout=5)

    active = store.flows.get_active_flow(flow.repo, flow.issue_number)
    assert active is not None and active.id == (created.id if created else flow.id)
    assert retry_won is (created is None)
    assert len([job for job in store.jobs.list_jobs(active.id) if job.state is JobState.QUEUED]) == 1


def bound_review_flow(store: Store):
    """Keep the worker ownership left by a finished initial assignment."""
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "epoch-a")
    flow = store.flows.create_flow("owner/repo", 703, 42)
    attempt = store.flows.create_attempt(flow.id)
    attempt.codex_session_id = "session"
    store.flows.update_attempt(attempt)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.state, delivery.base_branch, delivery.pr_number = DeliveryState.OPEN, "trunk", 17
    delivery.head_sha = "abc123"
    store.deliveries.update_delivery(delivery)
    initial = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("worker-a", "initial-token", "epoch-a") is not None
    assert store.workers.start_assignment(initial.id, attempt.id, "worker-a", "initial-token", "epoch-a")
    assert store.workers.finish_assignment(
        initial.id, "worker-a", "initial-token", JobState.DONE, "epoch-a"
    )
    return flow


def accepted_coding(store: Store, *, receive: bool = True, started: bool = True):
    """A remote coding assignment for worker journal/stop recovery tests."""
    flow = bound_review_flow(store)
    store.reviews.ingest(flow.id, "coding-review", "pull_request_review", {}, FeedbackUpdate(
        kind=ReviewItemKind.REVIEW_BODY, github_id=101, author="alice", body="Fix the bound",
        updated_at=store.now(), signal="request_changes", submitted=True,
    ), settle_seconds=0, submit_grace_seconds=0)
    batch, = store.reviews.claim_due()
    job = store.workers.assign_job("worker-a", "triage-token", "epoch-a")
    assert job is not None and job.assignment_token is not None and job.payload is not None
    binding = store.workers.get_flow_execution(flow.id)
    assert flow.current_attempt_id is not None and binding is not None
    task = RevisionTask(
        batch_id=batch.id, attempt_id=flow.current_attempt_id, feedback=batch.feedback,
        flow_id=flow.id, repo=flow.repo, branch=f"flow/{flow.id}", repo_identity=binding.repo_identity,
        pr_number=17, base_branch="trunk", expected_head_sha="abc123", session_id="session",
        session_generation=0, instruction=job.payload["instruction"], issue_title="Issue",
        issue_body="", previous_summary="", open_points=batch.open_points,
    )
    with store.transaction() as tx:
        assert tx.lock_workflow(flow.id) is not None
        tx.reviews.freeze_revision(job, task)
        if started:
            assert tx.workers.start_assignment(job.id, task.attempt_id, "worker-a", "triage-token", "epoch-a")
            assert tx.reviews.started(job)
    result = verified_result(task)
    if receive:
        assert started
        message = Succeeded(job_id=job.id, assignment_token=job.assignment_token,
                            connection_epoch="epoch-a", result=result)
        assert store.workers.record_assignment_result(job.id, "worker-a", job.assignment_token, "epoch-a",
            message.model_dump(mode="json"), terminal_digest(message)) == "pending"
    return store.jobs.get_job(job.id), result


def test_expired_cancelled_executor_does_not_overwrite_cancelled_flow(store: Store, monkeypatch):
    job, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(job.flow_id)
    assert flow is not None
    ControlService(
        database=store.database,
        deliveries=store.deliveries,
        flows=store.flows,
        jobs=store.jobs,
        result_store=store.results,
        reviews=store.reviews,
        stops=store.stops,
        webhook_store=store.webhooks,
        workers=store.workers,
    ).cancel(flow)
    running = store.jobs.get_job(job.id)
    assert running is not None and running.state is JobState.RUNNING
    store.workers.worker_disconnected("worker-a", "offline", "epoch-a")
    later = store.now() + timedelta(minutes=10)
    monkeypatch.setattr(store._db, "_clock", lambda: later)
    assert store.workers.sweep_expired_assignments() == (0, 0)
    execution = store.workers.get_flow_execution(flow.id)
    assert execution is not None and execution.released_at is None
    cancelled = store.flows.get_flow(job.flow_id)
    assert cancelled is not None and cancelled.state is FlowState.CANCELLED




async def test_accepted_result_survives_expired_worker_recovery(
    store: Store, monkeypatch, recording_resources,
) -> None:
    # Once control accepted the terminal result, an offline executor cannot invalidate its delivery.
    job, _ = accepted_coding(store)
    store.workers.worker_disconnected("worker-a", "connection lost", "epoch-a")
    later = store.now() + timedelta(minutes=10)
    monkeypatch.setattr(store._db, "_clock", lambda: later)
    assert store.workers.sweep_expired_assignments() == (0, 0)
    flow = store.flows.get_flow(job.flow_id)
    assert flow is not None and flow.state is FlowState.ACTIVE
    github = RecordingGitHub()
    pull_request = review_context().pull_request
    assert pull_request is not None
    github.pull_request = pull_request.model_copy(update={
        "head_branch": f"flow/{flow.id}", "head_sha": "abc123", "base_branch": "trunk",
    })
    await coordinator(store, github).results.apply(job.id)
    finished = store.jobs.get_job(job.id)
    assert finished is not None and finished.state is JobState.DONE
    execution = store.workers.get_flow_execution(job.flow_id)
    assert execution is not None and execution.released_at is None
