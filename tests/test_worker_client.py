import asyncio

import pytest

from app.models import JobState
from app.protocol import (
    Assignment,
    ExecutionResult,
    ExecutionTask,
    Heartbeat,
    InitialResult,
    InitialTask,
    Registered,
    ResultAccepted,
    Started,
    Succeeded,
    parse_worker_message,
)
from app.worker.client import WorkerClient


class RecordingExecutor:
    async def execute(self, task: ExecutionTask) -> ExecutionResult:
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


class CancellableExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def execute(self, task: ExecutionTask) -> ExecutionResult:
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
