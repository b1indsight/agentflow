"""Worker cutovers fence executions, not long-lived Flow bindings."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest

from app.contracts.protocol import PROTOCOL_VERSION
from app.contracts.updates import ReleaseEvent, UpdateIdentity, WorkerMaintenance
from app.domain.models import JobKind
from app.persistence.maintenance import MaintenanceStore
from app.persistence.worker_updates import WorkerUpdateStore
from app.persistence.workers import WorkerStore
from tests.test_maintenance import idle_binding


def request(action="pause", **changes):
    values = {
        "action": action,
        "request_id": "request",
        "operation_id": "a" * 32,
        "instance_id": "b" * 32,
        "connection_epoch": "epoch",
        "running_sha": "1" * 40,
        "release": ReleaseEvent(run_id=1, attempt=1, run_number=1, sha="2" * 40),
        "identity": UpdateIdentity(worker_id="w", root=("/worker", 1, 2), home="/home"),
    }
    values.update(changes)
    return WorkerMaintenance(**values)


def test_publication_accepts_new_attempt_without_regressing_or_changing_run_identity(store):
    updates = WorkerUpdateStore(store.database)
    release = request().release
    updates.publish(release)
    updates.publish(release)
    retry = release.model_copy(update={"attempt": 2})
    updates.publish(retry)
    assert updates.latest() == retry
    for stale in (release, retry.model_copy(update={"sha": "3" * 40})):
        with pytest.raises(ValueError, match="stale or conflicts"):
            updates.publish(stale)
    assert updates.latest() == retry


def test_target_pause_keeps_binding_and_other_workers_running(store):
    # Waiting for review occupies a binding indefinitely; it must not block a cutover.
    flow = idle_binding(store)
    updates = WorkerUpdateStore(store.database)
    assert updates.handle("w", "epoch", request()).idle
    store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    assert store.workers.assign_job("w", "revision", "epoch") is None
    store.workers.worker_connected("other", PROTOCOL_VERSION, "other-epoch")
    other = store.flows.create_flow("owner/other", 2, 1)
    store.jobs.enqueue_job(other.id, JobKind.INITIAL)
    assert store.workers.assign_job("other", "initial", "other-epoch")
    store.workers.worker_disconnected("w", "update", "epoch")
    store._db._clock = lambda: flow.created_at + timedelta(days=1)
    store.workers.sweep_expired_assignments()
    assert store.workers.get_flow_execution(flow.id).released_at is None
    store.workers.worker_connected("w", PROTOCOL_VERSION, "epoch")
    assert updates.handle("w", "epoch", request("abort")).accepted
    assert store.workers.assign_job("w", "revision", "epoch")


def test_cutover_requires_idle_and_new_version_identity_before_resume(store):
    idle_binding(store)
    updates = WorkerUpdateStore(store.database)
    assert updates.handle("w", "epoch", request()).idle
    assert updates.handle("w", "epoch", request("commit")).accepted
    assert not updates.handle("w", "epoch", request("abort")).accepted
    assert not updates.handle("w", "epoch", request("resume")).accepted
    assert not updates.handle(
        "w", "epoch", request("resume", instance_id="c" * 32, running_sha="2" * 40)
    ).accepted
    store.workers.worker_connected("w", PROTOCOL_VERSION, "new-epoch")
    new = request(
        "resume", connection_epoch="new-epoch", instance_id="c" * 32, running_sha="2" * 40
    )
    assert not updates.handle(
        "w", "new-epoch", new.model_copy(update={"instance_id": "b" * 32})
    ).accepted
    assert not updates.handle(
        "w", "new-epoch", new.model_copy(update={"running_sha": "3" * 40})
    ).accepted
    wrong = new.identity.model_copy(update={"root": ("/worker", 1, 3)})
    assert not updates.handle("w", "new-epoch", new.model_copy(update={"identity": wrong})).accepted
    global_pause = MaintenanceStore(store.database)
    global_pause.pause("server")
    assert updates.handle("w", "new-epoch", new).accepted
    assert updates.handle("w", "new-epoch", new).accepted  # lost resume reply
    store.workers.worker_connected("w", PROTOCOL_VERSION, "third-epoch")
    reconnect = new.model_copy(update={"connection_epoch": "third-epoch"})
    assert updates.handle("w", "third-epoch", reconnect).accepted
    assert global_pause.status().operation_id == "server"
    assert not updates.handle("w", "epoch", request()).accepted


@pytest.mark.parametrize("fact", ["slot", "dispatch", "result", "stop", "effect"])
def test_unsettled_execution_facts_prevent_cutover(store, fact):
    flow = idle_binding(store)
    with store.connection() as connection:
        job = store.jobs.list_jobs(flow.id)[0]
        if fact == "slot":
            connection.execute("UPDATE workers SET current_job_id=%s", (job.id,))
        elif fact == "dispatch":
            connection.execute(
                "UPDATE jobs SET dispatch_authorized_at=now() WHERE id=%s", (job.id,)
            )
        elif fact == "result":
            connection.execute("UPDATE jobs SET result_received_at=now() WHERE id=%s", (job.id,))
        elif fact == "stop":
            connection.execute(
                "INSERT INTO execution_stops(request_id,flow_id,worker_id,job_id,assignment_token,"
                "binding_at,state,requested_at)"
                " SELECT 'stop',%s,'w',%s,'token',bound_at,'pending',now()"
                " FROM flow_executions WHERE flow_id=%s",
                (flow.id, job.id, flow.id),
            )
        else:
            connection.execute(
                "INSERT INTO result_applications(job_id,digest,plan,created_at) VALUES(%s,'digest','{}',now())",
                (job.id,),
            )
            connection.execute(
                "INSERT INTO result_effects(job_id,effect_key,position,payload,state,dispatched_at,next_attempt_at)"
                " VALUES(%s,'effect',0,'{}','unknown',now(),now())",
                (job.id,),
            )
    updates = WorkerUpdateStore(store.database)
    assert not updates.handle("w", "epoch", request()).idle
    assert not updates.handle("w", "epoch", request("commit")).accepted


@pytest.mark.parametrize("first", ["assignment", "pause"])
def test_pause_and_assignment_are_serialized_in_both_commit_orders(store, first):
    store.workers.worker_connected("w", PROTOCOL_VERSION, "epoch")
    flow = store.flows.create_flow("owner/repo", 1, 1)
    store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    updates = WorkerUpdateStore(store.database)
    entered = Event()

    def second():
        entered.set()
        if first == "assignment":
            return updates.handle("w", "epoch", request())
        return store.workers.assign_job("w", "token", "epoch")

    # Hold the first operation uncommitted. The second must neither claim behind
    # a pause nor report idle ahead of an already committed assignment.
    with ThreadPoolExecutor() as executor:
        with store.transaction() as tx:
            if first == "assignment":
                assert WorkerStore(tx.database).assign_job("w", "token", "epoch")
            else:
                assert updates.using(tx.database).handle("w", "epoch", request()).accepted
            future = executor.submit(second)
            assert entered.wait(3)
            if first == "assignment":
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.05)
        result = future.result(timeout=3)
    if first == "assignment":
        assert result.accepted and not result.idle
    else:
        assert result is None
