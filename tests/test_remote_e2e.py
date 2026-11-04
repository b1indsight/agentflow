import asyncio
from collections.abc import Mapping
from unittest.mock import AsyncMock, Mock

import pytest

from app.communication.gateway import Gateway
from app.contracts.protocol import (
    AssignmentArguments,
    ExecutionResult,
    ExecutionTask,
    FlowToolResponse,
    InitialTask,
    InitialTaskResult,
    Succeeded,
    TerminalPayload,
    result_header,
    terminal_digest,
)
from app.contracts.receipts import ResultInbox, decode_result_inbox
from app.controler.workers.transport import serve_worker
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.tools import FlowToolService
from app.domain.delivery_models import OpenedDelivery
from app.domain.models import FlowDelivery, Job, JobKind, JobState, VcsKind, VcsOutcome
from app.persistence.stops import StopStore
from app.persistence.store import Store
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.state.execution import ExecutionState
from app.worker.transport.mcp import FlowToolCaller
from tests.persistence_fakes import RepositoryViews
from tests.worker_fakes import (
    NoPendingStops,
    connected_worker,
    result_application,
    run_worker,
    server_runtime,
    worker_runtime,
)


class QueueSocket:
    def __init__(self, worker_to_serve: asyncio.Queue[str], serve_to_worker: asyncio.Queue[str]):
        self._incoming = worker_to_serve
        self._outgoing = serve_to_worker
        self._headers = {
            "x-flowtool-worker-id": "worker-1",
            "authorization": "Bearer worker-secret",
        }

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers

    async def accept(self) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        pass

    async def send_text(self, data: str) -> None:
        await self._outgoing.put(data)

    async def receive_text(self) -> str:
        return await self._incoming.get()


class QueueConnection:
    def __init__(self, worker_to_serve: asyncio.Queue[str], serve_to_worker: asyncio.Queue[str]):
        self._outgoing = worker_to_serve
        self._incoming = serve_to_worker

    async def send(self, data: str) -> None:
        await self._outgoing.put(data)

    async def recv(self) -> str:
        return await self._incoming.get()

    async def close(self, code: int = 1000) -> None:
        pass


class RemoteStore(RepositoryViews, Store):
    @property
    def stops(self) -> StopStore:
        return NoPendingStops()

    def __init__(self) -> None:
        self.job = Job(id=5, flow_id=3, kind=JobKind.INITIAL, state=JobState.QUEUED)
        self.done = asyncio.Event()

    def get_job(self, job_id: int) -> Job | None:
        return self.job if self.job.id == job_id else None

    def get_delivery(self, flow_id: int) -> FlowDelivery | None:
        return None

    def get_worker_execution(self, worker_id: str):
        return None

    def worker_connected(self, worker_id: str, protocol_version: int, connection_epoch: str = ""):
        return connected_worker(worker_id, protocol_version, connection_epoch)

    def worker_disconnected(
        self,
        worker_id: str,
        reason: str | None = None,
        connection_epoch: str = "",
    ) -> None:
        pass

    def assign_job(self, worker_id: str, token: str, connection_epoch: str = "") -> Job | None:
        if self.job.state is not JobState.QUEUED:
            return None
        self.job.state = JobState.RUNNING
        self.job.assigned_worker_id = worker_id
        self.job.assignment_token = token
        return self.job

    def start_assignment(
        self,
        job_id: int,
        attempt_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str = "",
    ) -> bool:
        return self.assignment_active(job_id, worker_id, token)

    def heartbeat_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str = ""
    ) -> bool:
        return self.assignment_active(job_id, worker_id, token)

    def resume_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str
    ) -> bool:
        return self.assignment_active(job_id, worker_id, token)

    def record_assignment_result(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str,
        terminal: TerminalPayload,
    ) -> str:
        digest = terminal_digest(terminal)
        if (
            self.job.id != job_id
            or self.job.assigned_worker_id != worker_id
            or self.job.assignment_token != token
        ):
            return "rejected"
        if self.job.result_digest is not None:
            if self.job.result_digest != digest:
                return "conflict"
            return "applied" if self.job.result_applied_at is not None else "pending"
        if not self.assignment_active(job_id, worker_id, token):
            return "rejected"
        assert (terminal.job_id, terminal.assignment_token) == (job_id, token)
        self.job.result_payload = ResultInbox(source="worker", terminal=terminal).model_dump(
            mode="json"
        )
        self.job.result_digest = digest
        return "pending"

    def unapplied_result_ceiling(self) -> int:
        return 0

    def list_unapplied_results(
        self,
        *,
        after_id: int = 0,
        through_id: int | None = None,
        limit: int = 64,
    ) -> list[Job]:
        return []

    def complete_persisted_result(self, job_id: int, state: JobState) -> bool:
        self.job.state = state
        self.job.result_applied_at = self.job.updated_at
        self.done.set()
        return True

    def assignment_active(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str = ""
    ) -> bool:
        return (
            self.job.state is JobState.RUNNING
            and self.job.id == job_id
            and self.job.assigned_worker_id == worker_id
            and self.job.assignment_token == token
        )

    def finish_assignment(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        state: JobState,
        connection_epoch: str = "",
    ) -> bool:
        if not self.assignment_active(job_id, worker_id, token):
            return False
        self.job.state = state
        self.done.set()
        return True

    def requeue_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str = ""
    ) -> bool:
        self.job.state = JobState.QUEUED
        return True


class RemoteCoordinator(JobCoordinator):
    async def prepare_for_assignment(self, job, worker, token, epoch):
        task = await self.prepare(job)
        if isinstance(task, JobState):
            return task
        return AssignmentArguments(
            job_id=job.id, flow_id=job.flow_id, assignment_token=token, task=task
        )

    def __init__(self, store: RemoteStore) -> None:
        self.jobs = store.jobs
        self.tools = Mock(spec=FlowToolService, execute=AsyncMock(side_effect=self._tool_response))
        self.results = result_application(store, self._commit_result)
        self.result: InitialTaskResult | None = None
        self.tool_calls = 0

    async def prepare(self, job: Job) -> ExecutionTask | JobState:
        return InitialTask(
            flow_id=job.flow_id,
            attempt_id=7,
            repo="owner/repo",
            issue_number=8,
            issue_title="Issue",
            issue_body="Body",
            branch="flow/issue-8",
        )

    async def _tool_response(self, request, *, worker_id: str, connection_epoch: str):
        self.tool_calls += 1
        return FlowToolResponse(
            **result_header(request.message_id),
            method=request.method,
            call_id=request.arguments.call_id,
            result=OpenedDelivery(
                pr_number=17,
                head_sha="abc123",
                url="https://github.com/owner/repo/pull/17",
                delivery_version=1,
            ),
        )

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None:
        pass

    async def _commit_result(self, job_id: int) -> JobState:
        job = self.jobs.get_job(job_id)
        assert job is not None and job.result_payload is not None and job.result_digest is not None
        inbox = decode_result_inbox(
            job.result_payload,
            job_id=job.id,
            assignment_token=job.assignment_token,
            result_digest=job.result_digest,
        )
        assert isinstance(inbox.terminal, Succeeded)
        result = inbox.terminal.result
        assert isinstance(result, InitialTaskResult)
        self.result = result
        self.jobs.complete_persisted_result(job.id, JobState.DONE)
        return JobState.DONE

    async def fail(self, job: Job, reason: str) -> None:
        pass


class FakeExecutor:
    async def execute(
        self, task: ExecutionTask, secret=None, *, tool_caller: FlowToolCaller
    ) -> ExecutionResult:
        assert isinstance(task, InitialTask)
        tool = await tool_caller.call_tool(
            "flow.open_pr",
            {"title": "Implement", "body": "Done"},
            vcs_outcome=VcsOutcome(
                backend=VcsKind.GIT,
                branch=task.branch,
                commit_sha="abc",
                remote_sha="abc",
            ),
        )
        assert isinstance(tool, OpenedDelivery) and tool.pr_number == 17
        return InitialTaskResult(session_id="session-1", last_message="done", exit_code=0)

    async def reclaim_workspace(self) -> None:
        raise AssertionError("no stop was requested")


@pytest.mark.asyncio
async def test_control_gateway_and_worker_client_complete_one_remote_job(tmp_path) -> None:
    worker_to_serve: asyncio.Queue[str] = asyncio.Queue()
    serve_to_worker: asyncio.Queue[str] = asyncio.Queue()
    store = RemoteStore()
    coordinator = RemoteCoordinator(store)
    handler = server_runtime(
        coordinator,
        database=store.database,
        peers=Gateway(),
        poll_interval=0.01,
        tools=coordinator.tools,
        results=coordinator.results,
    )
    executor = FakeExecutor()
    state = ExecutionState(tmp_path)
    client = worker_runtime(
        read_history=state.read,
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="ws://unused",
        execution=WorkerExecution(worker_id="worker-1", executor=executor, state=state),
    )

    tasks = [
        asyncio.create_task(
            serve_worker(
                QueueSocket(worker_to_serve, serve_to_worker),
                worker_tokens={"worker-1": "worker-secret"},
                serve=handler.serve_worker_session,
            )
        ),
        asyncio.create_task(
            run_worker(client, QueueConnection(worker_to_serve, serve_to_worker))
        ),
    ]
    try:
        await asyncio.wait_for(store.done.wait(), timeout=1)
        # A server commit alone is not end-to-end completion: the worker must consume
        # the correlated A result before its journal can retire the assignment.
        async with asyncio.timeout(1):
            while True:
                journal = state.load()
                if journal is not None and journal.acknowledged:
                    break
                await asyncio.sleep(0)
        assert state.assignment is None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert store.job.state is JobState.DONE
    assert coordinator.result is not None
    assert coordinator.result.session_id == "session-1"
    assert coordinator.tool_calls == 1
