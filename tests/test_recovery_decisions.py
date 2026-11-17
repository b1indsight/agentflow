"""Recovery decisions must be applied to fresh facts in one worker-fenced transaction."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import psycopg
import pytest

from app.contracts.protocol import ExecutionStopped, Failed
from app.controler.workflow.service import ControlService
from app.domain.models import FlowState, Job, JobKind, JobState
from app.domain.recovery_policy import decide_preparation_retry
from app.github import GitHubError, GitHubErrorKind
from tests.flow_fixtures import enqueue_job
from tests.workspace_fixtures import register_worker, report_settled

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def expiring(store, *, started=False):
    store._db._clock = lambda: NOW
    flow = store.flows.create_flow("owner/recovery", 1, 1)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.set_current_attempt(flow)
    register_worker(store, "w", repo=flow.repo, epoch="epoch")
    job = enqueue_job(store, flow.id, JobKind.INITIAL)
    assert store.assignments.assign_job("w", "token", "epoch").id == job.id
    if started:
        assert store.assignments.start_assignment(job.id, attempt.id, "w", "token", "epoch")
    with store.connection() as connection:
        connection.execute(
            "UPDATE jobs SET lease_expires_at=%s,recovery_deadline=%s WHERE id=%s",
            (NOW, NOW, job.id),
        )
        connection.execute(
            "UPDATE flow_executions SET recovery_deadline=%s WHERE flow_id=%s", (NOW, flow.id)
        )
    return flow, job


@pytest.mark.parametrize("receipt", ["result", "stop"])
def test_sweep_waiting_on_worker_reads_committed_receipt(store, monkeypatch, receipt):
    # Serve may receive a terminal/stop receipt while the sweeper waits on the
    # worker lock. Decisions must see the receipt, not the earlier timeout facts.
    flow, job = expiring(store, started=True)
    if receipt == "stop":
        ControlService(database=store.database).cancel(flow)
    waiting = Event()
    execute = psycopg.Connection.execute

    def observed(connection, query, *args, **kwargs):
        if query == "SELECT worker_id,workspace FROM workers ORDER BY worker_id,workspace FOR UPDATE":
            waiting.set()
        return execute(connection, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", observed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.transaction() as tx:
            tx.lock_workflow(flow.id)
            sweep = pool.submit(store.assignments.sweep_expired_assignments)
            assert waiting.wait(5)
            assert not sweep.done()
            if receipt == "result":
                terminal = Failed(job_id=job.id, assignment_token="token", error="executor failed")
                assert (
                    tx.assignments.record_assignment_result(job.id, "w", "token", "epoch", terminal)
                    == "pending"
                )
            else:
                stop = tx.stops.pending("w")
                assert tx.stops.confirm(
                    "w",
                    ExecutionStopped(
                        request_id=stop.request_id,
                        flow_id=flow.id,
                        job_id=job.id,
                        assignment_token="token",
                        execution_nonce="executor",
                    ),
                    "epoch",
                )
                report_settled(tx, job.id, epoch="epoch", stopped=True)
        assert sweep.result(timeout=5) == (0, 0)
    assert store.flows.get_flow(flow.id).state is (
        FlowState.RUNNING if receipt == "result" else FlowState.ARCHIVED
    )
    if receipt == "result":
        assert store.jobs.get_job(job.id).result_received_at == NOW
        assert store.flows.get_flow_execution(flow.id).released_at is None
    else:
        assert store.jobs.get_job(job.id).last_error == "execution stopped"
        assert store.flows.get_flow_execution(flow.id).released_at == NOW


def test_sweep_failure_rolls_back_prior_job_updates(store, monkeypatch):
    # A crash after requeue but before clearing its exact claim must not commit
    # only the first half of a sweep or prematurely clear the worker slot.
    flow, job = expiring(store)

    execute = psycopg.Connection.execute

    def interrupted(connection, query, *args, **kwargs):
        if query.startswith("UPDATE workers SET current_job_id=NULL"):
            raise RuntimeError("claim cleanup interrupted")
        return execute(connection, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", interrupted)
    with pytest.raises(RuntimeError, match="claim cleanup interrupted"):
        store.assignments.sweep_expired_assignments()
    assert store.jobs.get_job(job.id).state is JobState.RUNNING
    assert store.jobs.get_job(job.id).assignment_token == "token"
    assert store.flows.get_flow(flow.id).state is FlowState.RUNNING
    assert any(w.current_job_id == job.id for w in store.workers.list_workspaces("w"))
    assert store.flows.get_flow_execution(flow.id).released_at is None


def preparation_retry(error, *, failures=0, first_failed=None, last_error=None):
    job = Job(id=1, flow_id=1, kind=JobKind.REVISION, state=JobState.RUNNING, prepare_failures=failures,
              prepare_first_failed_at=NOW - timedelta(seconds=first_failed) if first_failed is not None else None,
              prepare_last_error=last_error)
    return decide_preparation_retry(job, error, now=NOW, max_failures=3, window_seconds=600)


def test_transient_preparation_read_backs_off_within_count_and_window():
    transient = GitHubError(GitHubErrorKind.TRANSIENT, "GET /issues")

    first = preparation_retry(transient)
    assert (first.failures, first.first_failed_at, first.next_at) == (1, NOW, NOW + timedelta(seconds=2))
    assert first.cause == first.reason == str(transient)
    # The window is anchored at the first failure, not at each retry.
    second = preparation_retry(transient, failures=1, first_failed=595)
    assert (second.failures, second.first_failed_at) == (2, NOW - timedelta(seconds=595))
    assert second.next_at == NOW + timedelta(seconds=4)


@pytest.mark.parametrize(("error", "previous", "prefixed"), [
    # The third counted failure spends the budget.
    (GitHubError(GitHubErrorKind.TRANSIENT, "GET /issues"), {"failures": 2, "first_failed": 10}, True),
    # A retry due exactly when the window closes is not scheduled.
    (GitHubError(GitHubErrorKind.TRANSIENT, "GET /issues"), {"failures": 1, "first_failed": 596}, True),
    (GitHubError(GitHubErrorKind.RATE_LIMITED, "GET /issues", retry_after="900"), {}, True),
    # A permanent error reports itself, not an exhausted budget.
    (GitHubError(GitHubErrorKind.NOT_FOUND, "GET /issues"), {}, False),
])
def test_preparation_stops_at_its_budget_window_or_a_permanent_error(error, previous, prefixed):
    stopped = preparation_retry(error, **previous)

    assert stopped.next_at is None and stopped.cause == str(error)
    assert stopped.reason == (f"preparation retry budget/window exhausted: {error}" if prefixed else str(error))


def test_expired_preparation_window_stops_without_counting_another_failure():
    stopped = preparation_retry(None, failures=2, first_failed=600, last_error="GitHub GET /issues failed")

    assert (stopped.failures, stopped.next_at, stopped.cause) == (2, None, "GitHub GET /issues failed")
    assert stopped.reason == "preparation retry budget/window exhausted: GitHub GET /issues failed"
    assert preparation_retry(None).cause == "preparation window expired"
