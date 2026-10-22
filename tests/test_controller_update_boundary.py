"""Ordinary Controller replacement preserves the Worker executor and journal."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.contracts.protocol import (
    PROTOCOL_VERSION,
    Assignment,
    FlowToolResponse,
    InitialTask,
    InitialTaskResult,
    parse_server_message,
)
from app.controler.workers.conversation import ConversationBroker
from app.controler.workers.gateway import WorkerGateway
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.tools import FlowToolService
from app.domain.delivery_models import PreparedDelivery
from app.domain.models import JobState, VcsKind, VcsOutcome
from app.worker.state.session import WorkerSession
from app.worker.transport.client import WorkerClient
from tests.test_worker_connection import Socket
from tests.worker_fakes import result_application


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


@pytest.mark.parametrize("terminal_while_offline", [False, True])
async def test_running_worker_reconnects_to_new_controller_and_finishes_once(
    store, tmp_path, terminal_while_offline
):
    # Normal disconnect must not cancel the executor. Tools and terminal journal
    # replay on the new connection without a deployment pause or second execution.
    case = scenario(store)
    proceed = asyncio.Event()
    invocations = []

    class Executor:
        async def execute(self, task, secret=None, *, tool_caller):
            invocations.append(task)
            await proceed.wait()
            if not terminal_while_offline:
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
        await old.close(code=1001)
        with pytest.raises(ConnectionError, match="1001"):
            await asyncio.wait_for(worker, 2)
        await asyncio.wait_for(server, 2)
        assert execution is not None and not execution.done()
        assert case.tools.execute.await_count == case.results.apply.await_count == 0
        assert store.jobs.get_job(case.job.id).state == "running"

        proceed.set()
        if terminal_while_offline:
            await asyncio.wait_for(execution, 2)
            # The completed executor is journaled when the next registration is built.
            assert not session.load().acknowledged
        store.workers.recover_abandoned_assignments()
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
        assert execution.done() and not execution.cancelled()
        assert store.jobs.get_job(case.job.id).state == "done"
        assert case.results.apply.await_count == 1
        assert case.tools.execute.await_count == int(not terminal_while_offline)
        assert store.workers.get_flow_execution(case.job.flow_id).released_at is None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client._cancel_execution()


async def test_unapplied_result_recovers_after_ordinary_controller_restart(store, tmp_path):
    # A received result is already durable. Ordinary recovery must preserve it
    # after a long outage even without deployment-specific deadline extensions.
    case = scenario(store)
    store.workers.worker_connected("w", PROTOCOL_VERSION, "old-epoch")
    assert store.workers.assign_job("w", "token", "old-epoch")
    assignment = await case.gateway().coordinator.prepare_for_assignment(
        case.job, "w", "token", "old-epoch"
    )
    assert store.workers.start_assignment(
        case.job.id, assignment.task.attempt_id, "w", "token", "old-epoch"
    )
    session = WorkerSession(tmp_path)
    session.record_received(assignment)
    session.record_started()
    terminal = session.complete(
        InitialTaskResult(session_id="same-session", last_message="done", exit_code=0)
    )
    assert (
        store.workers.record_assignment_result(
            case.job.id,
            "w",
            "token",
            "old-epoch",
            terminal.model_dump(mode="json"),
            session.load().result_digest,
        )
        == "pending"
    )

    store.workers.worker_disconnected("w", "controller restart", "old-epoch")
    store._db._clock = lambda: case.job.created_at + timedelta(days=1)
    store.workers.recover_abandoned_assignments()
    assert store.jobs.get_job(case.job.id).state == "running"
    assert store.workers.get_flow_execution(case.job.flow_id).released_at is None
    # After the ordinary reconnect deadline, the Server's result recovery loop
    # applies its durable inbox; it does not extend the Worker's expired lease.
    await result_application(store, case.results.apply).recover_pending_results()

    executor = Mock(execute=AsyncMock(side_effect=AssertionError("terminal must not rerun")))
    client = WorkerClient(
        worker_id="w",
        worker_token="test",
        serve_ws_url="ws://unused",
        executor=executor,
        session=session,
    )
    socket = DuplexSocket()
    tasks = [
        asyncio.create_task(case.gateway().handle(socket)),
        asyncio.create_task(client.run_connection(socket)),
    ]
    try:
        async with asyncio.timeout(2):
            while (record := session.load()) is not None and not record.acknowledged:
                await asyncio.sleep(0)
        assert record is not None, (
            "controller rejected the persisted result and cleared its journal"
        )
        assert session.load().terminal == terminal
        assert store.jobs.get_job(case.job.id).state == "done"
        assert store.workers.get_flow_execution(case.job.flow_id).released_at is None
        case.results.apply.assert_awaited_once_with(case.job.id)
        executor.execute.assert_not_awaited()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client._cancel_execution()
