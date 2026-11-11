"""Recovery decisions must be applied to fresh facts in one worker-fenced transaction."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

import psycopg
import pytest

from app.contracts.protocol import ExecutionStopped, Failed
from app.controler.workflow.service import ControlService
from app.domain.models import FlowState, JobKind, JobState
from tests.workspace_fixtures import register_worker, report_settled

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def expiring(store, *, started=False):
    store._db._clock = lambda: NOW
    flow = store.flows.create_flow("owner/recovery", 1, 1)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    register_worker(store, "w", repo=flow.repo, epoch="epoch")
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("w", "token", "epoch").id == job.id
    if started:
        assert store.workers.start_assignment(job.id, attempt.id, "w", "token", "epoch")
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
            sweep = pool.submit(store.workers.sweep_expired_assignments)
            assert waiting.wait(5)
            assert not sweep.done()
            if receipt == "result":
                terminal = Failed(job_id=job.id, assignment_token="token", error="executor failed")
                assert (
                    tx.workers.record_assignment_result(job.id, "w", "token", "epoch", terminal)
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
        assert store.workers.get_flow_execution(flow.id).released_at is None
    else:
        assert store.jobs.get_job(job.id).last_error == "execution stopped"
        assert store.workers.get_flow_execution(flow.id).released_at == NOW


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
        store.workers.sweep_expired_assignments()
    assert store.jobs.get_job(job.id).state is JobState.RUNNING
    assert store.jobs.get_job(job.id).assignment_token == "token"
    assert store.flows.get_flow(flow.id).state is FlowState.RUNNING
    assert any(w.current_job_id == job.id for w in store.workers.list_workspaces("w"))
    assert store.workers.get_flow_execution(flow.id).released_at is None
