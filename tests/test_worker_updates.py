"""Worker cutovers use connection-local fences and existing execution facts."""

import asyncio

import pytest

from app.contracts.protocol import PROTOCOL_VERSION
from app.contracts.updates import ReleaseEvent, UpdateIdentity, WorkerMaintenance
from app.controler.workers.updates import WorkerUpdateSession
from app.domain.models import JobKind
from app.persistence.maintenance import MaintenanceStore
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


def test_target_pause_keeps_binding_and_other_workers_running(store):
    # Waiting for review occupies a binding indefinitely; it must not block a cutover.
    flow = idle_binding(store)
    updates = WorkerUpdateSession(store.workers, "w", "epoch")
    assert updates.handle(request()).idle
    store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    assert updates.paused
    assert store.workers.assign_job("w", "revision", "epoch", excluded=["w"]) is None
    store.workers.worker_connected("other", PROTOCOL_VERSION, "other-epoch")
    other = store.flows.create_flow("owner/other", 2, 1)
    store.jobs.enqueue_job(other.id, JobKind.INITIAL)
    assert store.workers.assign_job("other", "initial", "other-epoch", excluded=["w"])
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert updates.handle(request("abort")).accepted
    assert not updates.paused
    assert store.workers.assign_job("w", "revision", "epoch")


def test_cutover_requires_idle_and_new_version_identity_before_resume(store):
    idle_binding(store)
    old = WorkerUpdateSession(store.workers, "w", "epoch")
    assert old.handle(request()).idle
    assert old.handle(request("commit")).accepted
    assert not old.handle(request("abort")).accepted
    assert not old.handle(request("resume")).accepted
    assert not old.handle(request("resume", instance_id="c" * 32, running_sha="2" * 40)).accepted
    store.workers.worker_connected("w", PROTOCOL_VERSION, "new-epoch")
    updates = WorkerUpdateSession(store.workers, "w", "new-epoch", old.pending)
    new = request(
        "resume", connection_epoch="new-epoch", instance_id="c" * 32, running_sha="2" * 40
    )
    assert not updates.handle(new.model_copy(update={"instance_id": "b" * 32})).accepted
    assert not updates.handle(new.model_copy(update={"running_sha": "3" * 40})).accepted
    wrong = new.identity.model_copy(update={"root": ("/worker", 1, 3)})
    assert not updates.handle(new.model_copy(update={"identity": wrong})).accepted
    global_pause = MaintenanceStore(store.database)
    global_pause.pause("server")
    assert updates.handle(new).accepted
    assert updates.handle(new).accepted  # A lost resume reply must leave dispatch paused.
    assert updates.paused
    store.workers.worker_connected("w", PROTOCOL_VERSION, "third-epoch")
    updates = WorkerUpdateSession(store.workers, "w", "third-epoch", old.pending)
    reconnect = new.model_copy(update={"connection_epoch": "third-epoch"})
    assert updates.handle(reconnect).accepted
    ready = reconnect.model_copy(update={"action": "ready"})
    assert updates.handle(ready).accepted and not updates.paused
    assert updates.handle(ready).accepted
    assert global_pause.status().operation_id == "server"
    assert not updates.handle(request()).accepted


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
    updates = WorkerUpdateSession(store.workers, "w", "epoch")
    assert not updates.handle(request()).idle
    assert not updates.handle(request("commit")).accepted


async def test_reconnected_paused_worker_does_not_block_fair_dispatch_to_other_workers(store):
    from app.contracts.protocol import Assignment, Register, Registered
    from tests.test_controller_update_boundary import DuplexSocket, scenario

    # Restore the local operation in registration before any dispatch. The older
    # paused connection must also be excluded from the scheduler's fairness choice.
    case = scenario(store)
    gateway = case.gateway()
    gateway.worker_tokens["z-other"] = "test"
    paused, other = DuplexSocket(), DuplexSocket()
    other.headers["x-flowtool-worker-id"] = "z-other"
    await paused.send(
        Register(worker_id="w", update_version=1, update_pending=request()).model_dump_json()
    )
    await other.send(Register(worker_id="z-other").model_dump_json())
    tasks = []
    try:
        tasks.append(asyncio.create_task(gateway.handle(paused)))
        assert isinstance(await asyncio.wait_for(paused.sent.get(), 2), Registered)
        tasks.append(asyncio.create_task(gateway.handle(other)))
        assert isinstance(await asyncio.wait_for(other.sent.get(), 2), Registered)
        assignment = await asyncio.wait_for(other.sent.get(), 2)
        assert isinstance(assignment, Assignment) and assignment.job_id == case.job.id
        assert paused.sent.empty()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
