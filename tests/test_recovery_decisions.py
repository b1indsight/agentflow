"""Recovery decisions must be applied to fresh facts in one worker-fenced transaction."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

import psycopg
import pytest

from app.contracts.protocol import PROTOCOL_VERSION, ExecutionStopped
from app.domain.models import FlowState, JobKind, JobState, WorkerStatus

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def expiring(store, *, started=False):
    store._db._clock = lambda: NOW
    flow = store.flows.create_flow("owner/recovery", 1, 1)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    store.workers.worker_connected("w", PROTOCOL_VERSION, "epoch")
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("w", "token", "epoch").id == job.id
    if started:
        assert store.workers.start_assignment(job.id, attempt.id, "w", "token", "epoch")
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET lease_expires_at=%s,recovery_deadline=%s WHERE id=%s",
                           (NOW, NOW, job.id))
        connection.execute("UPDATE flow_executions SET recovery_deadline=%s WHERE flow_id=%s",
                           (NOW, flow.id))
    return flow, job


@pytest.mark.parametrize("receipt", ["result", "stop"])
def test_sweep_waiting_on_worker_reads_committed_receipt(store, monkeypatch, receipt):
    # Serve may receive a terminal/stop receipt while the sweeper waits on the
    # worker lock. Decisions must see the receipt, not the earlier timeout facts.
    flow, job = expiring(store, started=True)
    if receipt == "stop":
        with store.transaction() as tx:
            tx.lock_workflow(flow.id)
            tx.stops.request(flow.id)
    waiting = Event()
    execute = psycopg.Connection.execute

    def observed(connection, query, *args, **kwargs):
        if query == "SELECT worker_id FROM workers ORDER BY worker_id FOR UPDATE":
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
                assert tx.workers.record_assignment_result(job.id, "w", "token", "epoch", {}, "receipt") == "pending"
            else:
                stop = tx.stops.pending("w")
                assert tx.stops.confirm("w", ExecutionStopped(
                    request_id=stop.request_id, flow_id=flow.id, job_id=job.id,
                    assignment_token="token", connection_epoch="epoch", execution_nonce="executor",
                ))
        assert sweep.result(timeout=5) == (0, 0)
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    if receipt == "result":
        assert store.jobs.get_job(job.id).result_received_at == NOW
        assert store.workers.get_flow_execution(flow.id).released_at is None
    else:
        assert store.jobs.get_job(job.id).last_error == "execution stopped"
        assert store.workers.get_flow_execution(flow.id).released_at == NOW


def test_binding_decision_failure_rolls_back_prior_job_updates(store, monkeypatch):
    # A crash after requeue but before deciding binding release must not commit
    # only the first half of a sweep or prematurely clear the worker slot.
    flow, job = expiring(store)

    def interrupted(*args):
        raise RuntimeError("binding decision interrupted")

    monkeypatch.setattr("app.persistence.workers.should_release_binding", interrupted)
    with pytest.raises(RuntimeError, match="binding decision interrupted"):
        store.workers.sweep_expired_assignments()
    assert store.jobs.get_job(job.id).state is JobState.RUNNING
    assert store.jobs.get_job(job.id).assignment_token == "token"
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.workers.list_workers()[0].status is WorkerStatus.BUSY
    assert store.workers.get_flow_execution(flow.id).released_at is None
