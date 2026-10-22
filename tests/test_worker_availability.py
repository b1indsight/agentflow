"""Pause new assignments without changing execution or reconnect semantics."""

import asyncio

from app.contracts.protocol import (
    Assignment,
    AvailabilityAccepted,
    ExecutionStopped,
    FlowToolRequest,
    FlowToolResponse,
    Heartbeat,
    Register,
    Registered,
    Started,
    StopAccepted,
    StopExecution,
    WorkerAvailability,
)
from app.domain.models import VcsKind
from tests.test_controller_update_boundary import DuplexSocket, scenario


async def test_pause_during_preparation_fences_dispatch_and_normal_reconnect_resumes(store):
    # Preparation awaits remote I/O. Its assignment must not overtake a pause ACK
    # and reach an idle Worker that is about to restart.
    case = scenario(store)
    gateway = case.gateway()
    entered, release = asyncio.Event(), asyncio.Event()
    prepare = gateway.coordinator.prepare_for_assignment.side_effect

    async def blocked(*args):
        entered.set()
        await release.wait()
        return await prepare(*args)

    gateway.coordinator.prepare_for_assignment.side_effect = blocked
    socket = DuplexSocket()
    await socket.send(Register(worker_id="w").model_dump_json())
    tasks = [asyncio.create_task(gateway.handle(socket))]
    try:
        registered = await asyncio.wait_for(socket.sent.get(), 2)
        assert isinstance(registered, Registered)
        await asyncio.wait_for(entered.wait(), 2)
        await socket.send(
            WorkerAvailability(
                paused=True,
                request_id="pause",
                connection_epoch=registered.connection_epoch,
            ).model_dump_json()
        )
        ack = await asyncio.wait_for(socket.sent.get(), 2)
        assert isinstance(ack, AvailabilityAccepted) and ack.paused
        release.set()
        async with asyncio.timeout(2):
            while store.jobs.get_job(case.job.id).state != "queued":
                await asyncio.sleep(0)
        assert socket.sent.empty()
        assert store.workers.list_workers()[0].current_job_id is None

        # A new process sends an ordinary Register, with no upgrade identity or intent.
        replacement = DuplexSocket()
        await replacement.send(Register(worker_id="w").model_dump_json())
        tasks.append(asyncio.create_task(gateway.handle(replacement)))
        new = await asyncio.wait_for(replacement.sent.get(), 2)
        assert isinstance(new, Registered) and new.connection_epoch != registered.connection_epoch
        assignment = await asyncio.wait_for(replacement.sent.get(), 2)
        assert isinstance(assignment, Assignment) and assignment.job_id == case.job.id
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_authorized_assignment_precedes_pause_ack_even_when_send_is_blocked(store):
    # The existing send lock orders a frame already being transmitted ahead of
    # the pause ACK, so the Worker sees it before making its final idle decision.
    gateway = scenario(store).gateway()
    socket = DuplexSocket(Assignment)
    await socket.send(Register(worker_id="w").model_dump_json())
    task = asyncio.create_task(gateway.handle(socket))
    try:
        registered = await asyncio.wait_for(socket.sent.get(), 2)
        await asyncio.wait_for(socket.entered.wait(), 2)
        await socket.send(
            WorkerAvailability(
                paused=True,
                request_id="pause",
                connection_epoch=registered.connection_epoch,
            ).model_dump_json()
        )
        async with asyncio.timeout(2):
            while not gateway._sessions["w"][1].dispatch_paused:
                await asyncio.sleep(0)
        assert socket.sent.empty()
        socket.release.set()
        assert isinstance(await asyncio.wait_for(socket.sent.get(), 2), Assignment)
        assert isinstance(await asyncio.wait_for(socket.sent.get(), 2), AvailabilityAccepted)
    finally:
        socket.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_paused_worker_preserves_busy_tools_and_stop_acknowledgement(store):
    case = scenario(store)
    gateway = case.gateway()
    socket = DuplexSocket()
    await socket.send(Register(worker_id="w").model_dump_json())
    task = asyncio.create_task(gateway.handle(socket))
    try:
        await asyncio.wait_for(socket.sent.get(), 2)
        assignment = await asyncio.wait_for(socket.sent.get(), 2)
        credentials = {
            "job_id": assignment.job_id,
            "assignment_token": assignment.assignment_token,
            "connection_epoch": assignment.connection_epoch,
        }
        await socket.send(Started(**credentials).model_dump_json())
        await asyncio.wait_for(case.started.wait(), 2)
        await socket.send(
            WorkerAvailability(
                paused=True, request_id="pause", connection_epoch=assignment.connection_epoch
            ).model_dump_json()
        )
        assert isinstance(await asyncio.wait_for(socket.sent.get(), 2), AvailabilityAccepted)
        assert store.workers.list_workers()[0].status == "busy"
        await socket.send(
            FlowToolRequest(
                **credentials,
                flow_id=case.job.flow_id,
                attempt_id=1,
                call_id="call",
                name="flow.open_pr",
                arguments={},
                vcs_backend=VcsKind.GIT,
                branch="flow/1",
                commit_sha="abc",
                remote_sha="abc",
            ).model_dump_json()
        )
        assert isinstance(await asyncio.wait_for(socket.sent.get(), 2), FlowToolResponse)
        with store.transaction() as tx:
            tx.lock_workflow(case.job.flow_id)
            tx.stops.request(case.job.flow_id)
        gateway.notify_jobs()
        # An execution frame wakes the running job's ordinary stop check.
        await socket.send(Heartbeat(**credentials).model_dump_json())
        stop = await asyncio.wait_for(socket.sent.get(), 2)
        assert isinstance(stop, StopExecution)
        await socket.send(
            ExecutionStopped(
                **stop.model_dump(exclude={"type"}), execution_nonce="executor"
            ).model_dump_json()
        )
        assert isinstance(await asyncio.wait_for(socket.sent.get(), 2), StopAccepted)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_paused_idle_worker_does_not_block_fair_dispatch(store):
    case = scenario(store)
    # Register and pause before creating work so neither Worker owns a binding yet.
    with store.connection() as db:
        db.execute("DELETE FROM jobs WHERE id=%s", (case.job.id,))
    gateway = case.gateway()
    gateway.worker_tokens["z-other"] = "test"
    paused, other = DuplexSocket(), DuplexSocket()
    other.headers["x-flowtool-worker-id"] = "z-other"
    tasks = []
    try:
        await paused.send(Register(worker_id="w").model_dump_json())
        tasks.append(asyncio.create_task(gateway.handle(paused)))
        registered = await asyncio.wait_for(paused.sent.get(), 2)
        await paused.send(
            WorkerAvailability(
                paused=True, request_id="pause", connection_epoch=registered.connection_epoch
            ).model_dump_json()
        )
        assert isinstance(await asyncio.wait_for(paused.sent.get(), 2), AvailabilityAccepted)
        job = store.jobs.enqueue_job(case.job.flow_id, "initial")
        await other.send(Register(worker_id="z-other").model_dump_json())
        tasks.append(asyncio.create_task(gateway.handle(other)))
        assert isinstance(await asyncio.wait_for(other.sent.get(), 2), Registered)
        assignment = await asyncio.wait_for(other.sent.get(), 2)
        assert isinstance(assignment, Assignment) and assignment.job_id == job.id
        assert paused.sent.empty()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
