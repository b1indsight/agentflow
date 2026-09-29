"""Recovery decisions must be applied to fresh facts in one worker-fenced transaction."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

import psycopg
import pytest

from app.models import DeliveryState, FlowState, JobKind, JobState, WorkerStatus
from app.protocol import PROTOCOL_VERSION, ExecutionStopped

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def expiring(store, *, started=False):
    store._clock = lambda: NOW
    flow = store.create_flow("owner/recovery", 1, 1)
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    store.worker_connected("w", PROTOCOL_VERSION, "epoch")
    job = store.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.assign_job("w", "token", "epoch").id == job.id
    if started:
        assert store.start_assignment(job.id, attempt.id, "w", "token", "epoch")
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET lease_expires_at=%s,recovery_deadline=%s WHERE id=%s",
                           (NOW, NOW, job.id))
        connection.execute("UPDATE flow_executions SET recovery_deadline=%s WHERE flow_id=%s",
                           (NOW, flow.id))
    return flow, job


@pytest.mark.parametrize("started,delivery,pending,counts,job_state,flow_state,worker_state,released", [
    (False, None, None, (1, 1), JobState.QUEUED, FlowState.FAILED, WorkerStatus.FAILED, True),
    (True, None, None, (0, 1), JobState.FAILED, FlowState.FAILED, WorkerStatus.FAILED, True),
    (False, DeliveryState.CLOSED, None, (1, 0), JobState.QUEUED, FlowState.ACTIVE, WorkerStatus.OFFLINE, False),
    (True, DeliveryState.CLOSED, None, (0, 0), JobState.RUNNING, FlowState.ACTIVE, WorkerStatus.BUSY, False),
    (True, DeliveryState.MERGED, None, (0, 0), JobState.RUNNING, FlowState.ACTIVE, WorkerStatus.BUSY, False),
    (True, None, "result", (0, 0), JobState.RUNNING, FlowState.ACTIVE, WorkerStatus.BUSY, False),
    (True, None, "stop", (0, 0), JobState.RUNNING, FlowState.ACTIVE, WorkerStatus.BUSY, False),
    (False, None, "dispatch", (0, 0), JobState.RUNNING, FlowState.ACTIVE, WorkerStatus.BUSY, False),
])
def test_sweep_preserves_states_and_counts(
    store, started, delivery, pending, counts, job_state, flow_state, worker_state, released,
):
    # A single sweep may requeue an unstarted job and then expire its idle binding;
    # preserve that order and count, while durable evidence keeps ownership intact.
    flow, job = expiring(store, started=started)
    if delivery is not None:
        record = store.get_or_create_delivery(flow.id, "flow/1")
        record.state = delivery
        store.update_delivery(record)
    if pending == "result":
        assert store.record_assignment_result(job.id, "w", "token", "epoch", {}, "receipt") == "pending"
    elif pending == "stop":
        with store.transaction() as tx:
            tx.lock_workflow(flow.id)
            tx.stops.request(flow.id)
    elif pending == "dispatch":
        with store.connection() as connection:
            connection.execute("UPDATE jobs SET dispatch_authorized_at=%s WHERE id=%s", (NOW, job.id))
    assert store.sweep_expired_assignments() == counts
    actual_job = store.get_job(job.id)
    assert actual_job.state is job_state
    assert store.get_flow(flow.id).state is flow_state
    assert store.list_workers()[0].status is worker_state
    assert (store.get_flow_execution(flow.id).released_at is not None) is released
    if started and delivery is not None:
        assert actual_job.last_error == "archive blocked: original worker executor status is unknown"
    elif job_state is JobState.FAILED:
        assert actual_job.last_error == "worker recovery deadline expired"


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
            sweep = pool.submit(store.sweep_expired_assignments)
            assert waiting.wait(5)
            assert not sweep.done()
            if receipt == "result":
                assert tx.record_assignment_result(job.id, "w", "token", "epoch", {}, "receipt") == "pending"
            else:
                stop = tx.stops.pending("w")
                assert tx.stops.confirm("w", ExecutionStopped(
                    request_id=stop.request_id, flow_id=flow.id, job_id=job.id,
                    assignment_token="token", connection_epoch="epoch", execution_nonce="executor",
                ))
        assert sweep.result(timeout=5) == (0, 0)
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    if receipt == "result":
        assert store.get_job(job.id).result_received_at == NOW
        assert store.get_flow_execution(flow.id).released_at is None
    else:
        assert store.get_job(job.id).last_error == "execution stopped"
        assert store.get_flow_execution(flow.id).released_at == NOW


def test_binding_decision_failure_rolls_back_prior_job_updates(store, monkeypatch):
    # A crash after requeue but before deciding binding release must not commit
    # only the first half of a sweep or prematurely clear the worker slot.
    flow, job = expiring(store)

    def interrupted(*args):
        raise RuntimeError("binding decision interrupted")

    monkeypatch.setattr("app.store.should_release_binding", interrupted)
    with pytest.raises(RuntimeError, match="binding decision interrupted"):
        store.sweep_expired_assignments()
    assert store.get_job(job.id).state is JobState.RUNNING
    assert store.get_job(job.id).assignment_token == "token"
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.list_workers()[0].status is WorkerStatus.BUSY
    assert store.get_flow_execution(flow.id).released_at is None
