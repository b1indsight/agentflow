"""Ordinary Controller replacement preserves the Worker executor and journal."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.contracts.protocol import (
    Assignment,
    FlowToolResponse,
    InitialTask,
    InitialTaskResult,
)
from app.controler.workers.conversation import ConversationBroker
from app.controler.workers.gateway import WorkerGateway
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.tools import FlowToolService
from app.domain.delivery_models import PreparedDelivery
from app.domain.models import JobState, VcsKind, VcsOutcome
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.state.execution import ExecutionState
from app.worker.transport.client import WorkerClient
from tests.test_worker_connection import Socket


class DuplexSocket(Socket):
    def __init__(self):
        super().__init__()
        self.headers = {"x-flowtool-worker-id": "w", "authorization": "Bearer test"}

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


async def test_running_worker_reconnects_to_new_controller_and_finishes_once(store, tmp_path):
    # Normal disconnect must not cancel the executor. Tools and terminal journal
    # replay on the new connection without a deployment pause or second execution.
    case = scenario(store)
    proceed = asyncio.Event()
    invocations = []

    class Executor:
        async def execute(self, task, secret=None, *, tool_caller):
            invocations.append(task)
            await proceed.wait()
            await tool_caller.call_tool(
                "flow.open_pr",
                {},
                call_id="same-call",
                vcs_outcome=VcsOutcome(
                    backend=VcsKind.GIT, branch="flow/1", commit_sha="abc", remote_sha="abc"
                ),
            )
            return InitialTaskResult(session_id="same-session", last_message="done", exit_code=0)

    session = ExecutionState(tmp_path)
    owner = WorkerExecution(worker_id="w", executor=Executor(), state=session)
    client = WorkerClient(
        worker_id="w",
        worker_token="test",
        serve_ws_url="ws://unused",
        execution=owner,
        read_history=session.read,
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
        await old.close(code=1001)
        with pytest.raises(ConnectionError, match="1001"):
            await asyncio.wait_for(worker, 2)
        await asyncio.wait_for(server, 2)
        assert owner.lease() is not None
        assert case.tools.execute.await_count == case.results.apply.await_count == 0
        assert store.jobs.get_job(case.job.id).state == "running"

        proceed.set()
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
        assert owner.lease() is None
        assert store.jobs.get_job(case.job.id).state == "done"
        assert case.results.apply.await_count == 1
        assert case.tools.execute.await_count == 1
        assert store.workers.get_flow_execution(case.job.flow_id).released_at is None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await owner.shutdown()
