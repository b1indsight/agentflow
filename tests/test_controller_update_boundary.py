"""Controller cutover keeps the real Worker driver and its local executor alive."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.contracts.protocol import (
    PROTOCOL_VERSION,
    Assignment,
    ExecutionStopped,
    FlowToolRequest,
    FlowToolResponse,
    Heartbeat,
    InitialTask,
    InitialTaskResult,
    Register,
    Registered,
    ResultAccepted,
    Started,
    StopAccepted,
    StopExecution,
    Succeeded,
    WorkerMessage,
    parse_server_message,
)
from app.contracts.updates import MaintenanceReply
from app.controler.runtime import ServerRuntime
from app.controler.workers.conversation import ConversationBroker
from app.controler.workers.gateway import WorkerGateway
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.tools import FlowToolService
from app.domain.delivery_models import PreparedDelivery
from app.domain.models import JobState, VcsKind, VcsOutcome
from app.persistence.maintenance import MaintenanceStore
from app.worker.state.session import WorkerSession
from app.worker.transport.client import WorkerClient
from tests.test_worker_connection import Socket
from tests.test_worker_updates import request as maintenance_request


class DuplexSocket(Socket):
    def __init__(self, blocked=None):
        super().__init__()
        self.headers = {"x-flowtool-worker-id": "w", "authorization": "Bearer test"}
        self.blocked = blocked
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def send_text(self, raw):
        if self.blocked is not None and isinstance(parse_server_message(raw), self.blocked):
            self.entered.set()
            await self.release.wait()
        await super().send_text(raw)

    async def close(self, code=1000):
        await super().close(code)
        await self.sent.put(ConnectionError(f"controller closed: {code}"))

    async def send(self, raw):
        await self.incoming.put(raw)

    async def recv(self):
        message = await self.sent.get()
        if isinstance(message, Exception):
            raise message
        return message.model_dump_json()


def scenario(store):
    flow = store.flows.create_flow("owner/repo", 1, 1)
    attempt = store.flows.create_attempt(flow.id)
    job = store.jobs.enqueue_job(flow.id, "initial")
    started = asyncio.Event()

    async def prepare(job, worker, token, epoch):
        return Assignment(
            job_id=job.id,
            flow_id=flow.id,
            assignment_token=token,
            connection_epoch=epoch,
            task=InitialTask(
                flow_id=flow.id,
                attempt_id=attempt.id,
                repo="owner/repo",
                issue_number=1,
                issue_title="Test",
                issue_body="",
                branch="flow/1",
            ),
        )

    async def apply(job_id):
        assert store.jobs.complete_persisted_result(job_id, JobState.DONE)
        return JobState.DONE

    async def tool(message, *, worker_id):
        return FlowToolResponse(
            call_id=message.call_id, result=PreparedDelivery(head_sha="abc", delivery_version=1)
        )

    coordinator = Mock(
        spec=JobCoordinator,
        prepare_for_assignment=AsyncMock(side_effect=prepare),
        mark_started=Mock(side_effect=lambda *args: started.set()),
    )
    tools = Mock(spec=FlowToolService, execute=AsyncMock(side_effect=tool))
    results = Mock(spec=ResultApplication, apply=AsyncMock(side_effect=apply))

    def gateway():
        return WorkerGateway(
            coordinator,
            database=store.database,
            worker_tokens={"w": "test"},
            heartbeat_interval=60,
            poll_interval=0.01,
            tools=tools,
            results=results,
            conversations=ConversationBroker(),
        )

    return SimpleNamespace(job=job, started=started, gateway=gateway, tools=tools, results=results)


@pytest.mark.parametrize("terminal_during_pause", [False, True])
async def test_running_worker_reconnects_to_new_controller_and_finishes_once(
    store, tmp_path, terminal_during_pause
):
    # A late tool or terminal frame is refused by the old controller, then replayed
    # by the same Worker executor/journal against a replacement controller epoch.
    case = scenario(store)
    proceed = asyncio.Event()
    invocations = []

    class Executor:
        async def execute(self, task, secret=None, *, tool_caller):
            invocations.append(task)
            await proceed.wait()
            if not terminal_during_pause:
                await tool_caller.call_tool(
                    "flow.open_pr",
                    {},
                    call_id="same-call",
                    vcs_outcome=VcsOutcome(
                        backend=VcsKind.GIT, branch="flow/1", commit_sha="abc", remote_sha="abc"
                    ),
                )
            return InitialTaskResult(session_id="same-session", last_message="done", exit_code=0)

    session = WorkerSession(tmp_path)
    client = WorkerClient(
        worker_id="w",
        worker_token="test",
        serve_ws_url="ws://unused",
        executor=Executor(),
        session=session,
    )
    maintenance = MaintenanceStore(store.database)
    maintenance.start_instance()
    tasks = []
    try:
        old = DuplexSocket()
        server = asyncio.create_task(case.gateway().handle(old))
        worker = asyncio.create_task(client.run_connection(old))
        tasks.extend([server, worker])
        await asyncio.wait_for(case.started.wait(), 2)
        async with asyncio.timeout(2):
            while not invocations:
                await asyncio.sleep(0)
        execution = client._execution
        assert maintenance.active() == []
        maintenance.pause("update")
        proceed.set()
        with pytest.raises(ConnectionError, match="1013"):
            await asyncio.wait_for(worker, 2)
        await asyncio.wait_for(server, 2)
        assert case.tools.execute.await_count == case.results.apply.await_count == 0
        assert store.jobs.get_job(case.job.id).state == "running"
        assert not session.load().acknowledged

        maintenance.start_instance()
        store.workers.recover_abandoned_assignments()
        maintenance.resume("update")
        new = DuplexSocket()
        tasks.extend(
            [
                asyncio.create_task(case.gateway().handle(new)),
                asyncio.create_task(client.run_connection(new)),
            ]
        )
        async with asyncio.timeout(2):
            while not session.load().acknowledged:
                await asyncio.sleep(0)
        assert len(invocations) == 1
        assert execution is not None and execution.done() and not execution.cancelled()
        assert store.jobs.get_job(case.job.id).state == "done"
        assert case.results.apply.await_count == 1
        assert case.tools.execute.await_count == int(not terminal_during_pause)
        assert store.workers.get_flow_execution(case.job.flow_id).released_at is None
        assert maintenance.active() == []
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client._cancel_execution()


@pytest.mark.parametrize(
    "boundary", [Assignment, FlowToolResponse, ResultAccepted, MaintenanceReply, StopAccepted]
)
async def test_cutover_waits_through_reply_but_not_heartbeats_or_local_execution(store, boundary):
    case = scenario(store)
    maintenance = MaintenanceStore(store.database)
    socket = DuplexSocket(boundary)
    await socket.send(Register(worker_id="w", update_version=1).model_dump_json())
    task = asyncio.create_task(case.gateway().handle(socket))
    try:
        assert isinstance(await asyncio.wait_for(socket.sent.get(), 2), Registered)
        if boundary is Assignment:
            await asyncio.wait_for(socket.entered.wait(), 2)
            maintenance.pause("update")
            assert maintenance.active() == ["worker-dispatch"]
            socket.release.set()
        assignment = await asyncio.wait_for(socket.sent.get(), 2)
        assert isinstance(assignment, Assignment)
        credentials = Started(
            job_id=assignment.job_id,
            assignment_token=assignment.assignment_token,
            connection_epoch=assignment.connection_epoch,
        ).model_dump(exclude={"type"})
        await socket.send(Started(**credentials).model_dump_json())
        await asyncio.wait_for(case.started.wait(), 2)
        assert maintenance.active() == []
        if boundary is Assignment:
            before = store.jobs.get_job(case.job.id).heartbeat_at
            await socket.send(Heartbeat(**credentials).model_dump_json())
            async with asyncio.timeout(2):
                while store.jobs.get_job(case.job.id).heartbeat_at == before:
                    await asyncio.sleep(0)
            assert maintenance.active() == [] and not task.done()
            return
        if boundary is StopAccepted:
            with store.transaction() as tx:
                tx.lock_workflow(case.job.flow_id)
                tx.stops.request(case.job.flow_id)
            await socket.send(Heartbeat(**credentials).model_dump_json())
            stop = await asyncio.wait_for(socket.sent.get(), 2)
            assert isinstance(stop, StopExecution)
            message: WorkerMessage = ExecutionStopped(
                **stop.model_dump(exclude={"type"}), execution_nonce="executor"
            )
        elif boundary is FlowToolResponse:
            message = FlowToolRequest(
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
            )
        elif boundary is ResultAccepted:
            message = Succeeded(
                **credentials,
                result=InitialTaskResult(session_id="session", last_message="done", exit_code=0),
            )
        else:
            message = maintenance_request().model_copy(
                update={"connection_epoch": assignment.connection_epoch}
            )
        await socket.send(message.model_dump_json())
        await asyncio.wait_for(socket.entered.wait(), 2)
        maintenance.pause("update")
        assert maintenance.active() == [
            {
                FlowToolResponse: "worker-tool",
                ResultAccepted: "worker-result",
                MaintenanceReply: "worker-maintenance",
                StopAccepted: "worker-stop",
            }[boundary]
        ]
        socket.release.set()
        assert isinstance(await asyncio.wait_for(socket.sent.get(), 2), boundary)
        assert maintenance.active() == []
    finally:
        socket.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_paused_registration_does_not_replace_live_worker_epoch(store):
    case = scenario(store)
    gateway = case.gateway()
    gateway.workers.worker_connected("w", PROTOCOL_VERSION, "original")
    gateway.maintenance.pause("update")
    socket = DuplexSocket()
    await socket.send(Register(worker_id="w").model_dump_json())
    await gateway.handle(socket)
    assert gateway.workers.list_workers()[0].connection_epoch == "original"
    assert isinstance(await socket.sent.get(), ConnectionError)
    assert gateway.maintenance.active() == []


async def test_paused_controller_does_not_start_result_recovery(store):
    maintenance = MaintenanceStore(store.database)
    maintenance.pause("update")
    runtime = ServerRuntime.__new__(ServerRuntime)
    runtime.maintenance = maintenance
    runtime.settings = SimpleNamespace(
        result_recovery_concurrency=1, result_recovery_batch_size=1, worker_sweep_interval=60
    )
    runtime.results = SimpleNamespace(recover_pending_results=AsyncMock())
    runtime.gateway = SimpleNamespace(notify_jobs=Mock())
    task = asyncio.create_task(runtime._sweep_results())
    try:
        await asyncio.sleep(0)
        runtime.results.recover_pending_results.assert_not_awaited()
        runtime.gateway.notify_jobs.assert_called_once()
        assert maintenance.active() == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
