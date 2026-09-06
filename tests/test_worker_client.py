import asyncio

import pytest

from app.models import JobState, VcsKind, VcsOutcome
from app.protocol import (
    Assignment,
    ExecutionResult,
    ExecutionTask,
    FlowToolErrorKind,
    FlowToolRequest,
    FlowToolResponse,
    Heartbeat,
    InitialResult,
    InitialTask,
    Registered,
    ResultAccepted,
    Started,
    Succeeded,
    parse_worker_message,
)
from app.worker.client import FlowToolCallError, WorkerClient


def test_flow_tool_call_error_preserves_retry_metadata() -> None:
    error = FlowToolCallError(
        FlowToolResponse(
            call_id="call-1",
            error="GitHub get ref failed",
            error_kind=FlowToolErrorKind.RATE_LIMITED,
            retryable=True,
            retry_after="30",
        )
    )

    assert error.kind is FlowToolErrorKind.RATE_LIMITED
    assert error.retryable is True
    assert error.retry_after == "30"
    assert str(error).endswith("retryable=true; retry_after=30")


class RecordingExecutor:
    async def execute(self, task: ExecutionTask, secret=None) -> ExecutionResult:
        assert isinstance(task, InitialTask)
        assert task.repo == "owner/repo"
        return InitialResult(session_id="session-1", last_message="done", exit_code=0)


class ScriptedConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.messages = [
            Registered(heartbeat_interval=60, connection_epoch="epoch-1").model_dump_json(),
            Assignment(
                job_id=5,
                flow_id=3,
                assignment_token="assignment-1",
                connection_epoch="epoch-1",
                task=InitialTask(
                    flow_id=3,
                    attempt_id=7,
                    repo="owner/repo",
                    issue_number=8,
                    issue_title="Issue",
                    issue_body="Body",
                    branch="flow/issue-8",
                ),
            ).model_dump_json(),
            ResultAccepted(
                job_id=5,
                state=JobState.DONE,
                assignment_token="assignment-1",
                connection_epoch="epoch-1",
            ).model_dump_json(),
        ]

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if not self.messages:
            raise ConnectionError("script complete")
        return self.messages.pop(0)


@pytest.mark.asyncio
async def test_client_acknowledges_and_executes_one_assignment() -> None:
    connection = ScriptedConnection()
    client = WorkerClient(
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="ws://serve/internal/workers/connect",
        executor=RecordingExecutor(),
    )

    with pytest.raises(ConnectionError, match="script complete"):
        await client.run_connection(connection)

    messages = [parse_worker_message(raw) for raw in connection.sent]
    assert isinstance(messages[1], Started)
    assert isinstance(messages[2], Succeeded)
    assert messages[2].assignment_token == "assignment-1"


class ToolCallingExecutor:
    def __init__(self) -> None:
        self.client: WorkerClient | None = None

    async def execute(self, task: ExecutionTask, secret=None) -> ExecutionResult:
        assert self.client is not None
        result = await self.client.call_tool(
            "flow.open_pr",
            {"title": "Implement", "body": "Done"},
            vcs_outcome=VcsOutcome(
                declared_backend=VcsKind.GIT,
                verified_backend=VcsKind.GIT,
                branch="flow/3",
                commit_sha="abc",
                remote_sha="abc",
            ),
        )
        assert result["pr_number"] == 17
        return InitialResult(session_id="session-1", last_message="done", exit_code=0)


class ToolConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.step = 0

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        self.step += 1
        if self.step == 1:
            return Registered(heartbeat_interval=60, connection_epoch="epoch-1").model_dump_json()
        if self.step == 2:
            return Assignment(
                job_id=5,
                flow_id=3,
                assignment_token="assignment-1",
                connection_epoch="epoch-1",
                task=InitialTask(
                    flow_id=3,
                    attempt_id=7,
                    repo="owner/repo",
                    issue_number=8,
                    issue_title="Issue",
                    issue_body="Body",
                    branch="flow/3",
                ),
            ).model_dump_json()
        if self.step == 3:
            request = parse_worker_message(self.sent[-1])
            assert isinstance(request, FlowToolRequest)
            return FlowToolResponse(
                call_id=request.call_id, result={"pr_number": 17}
            ).model_dump_json()
        if self.step == 4:
            terminal = parse_worker_message(self.sent[-1])
            assert isinstance(terminal, Succeeded)
            return ResultAccepted(
                job_id=5,
                state=JobState.DONE,
                assignment_token="assignment-1",
                connection_epoch="epoch-1",
            ).model_dump_json()
        raise ConnectionError("script complete")


@pytest.mark.asyncio
async def test_client_multiplexes_tool_call_while_executor_is_running() -> None:
    executor = ToolCallingExecutor()
    client = WorkerClient(
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="ws://serve/internal/workers/connect",
        executor=executor,
    )
    executor.client = client
    connection = ToolConnection()

    with pytest.raises(ConnectionError, match="script complete"):
        await client.run_connection(connection)

    assert any(
        isinstance(parse_worker_message(raw), FlowToolRequest)
        for raw in connection.sent
    )


class CancellableExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def execute(self, task: ExecutionTask, secret=None) -> ExecutionResult:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        raise AssertionError("unreachable")


class BrokenHeartbeatConnection:
    async def send(self, data: str) -> None:
        if isinstance(parse_worker_message(data), Heartbeat):
            raise ConnectionError("connection lost")

    async def recv(self) -> str:
        raise AssertionError("result acknowledgement should not be awaited")


@pytest.mark.asyncio
async def test_heartbeat_failure_preserves_the_running_executor_for_reconnect() -> None:
    executor = CancellableExecutor()
    client = WorkerClient(
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="ws://unused",
        executor=executor,
    )
    assignment = Assignment(
        job_id=5,
        flow_id=3,
        assignment_token="assignment-1",
        connection_epoch="epoch-1",
        task=InitialTask(
            flow_id=3,
            attempt_id=7,
            repo="owner/repo",
            issue_number=8,
            issue_title="Issue",
            issue_body="Body",
            branch="flow/issue-8",
        ),
    )

    execution = asyncio.create_task(
        client._execute_assignment(
            BrokenHeartbeatConnection(), assignment, heartbeat_interval=0.01
        )
    )
    await executor.started.wait()
    try:
        with pytest.raises(ConnectionError, match="connection lost"):
            await execution
        assert executor.cancelled.is_set() is False
    finally:
        await client._cancel_execution()
    assert executor.cancelled.is_set() is True
