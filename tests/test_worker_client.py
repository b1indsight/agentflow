import asyncio

import pytest

from app.contracts.protocol import (
    Assignment,
    ExecutionResult,
    ExecutionTask,
    Failed,
    FlowToolErrorKind,
    FlowToolRequest,
    FlowToolResponse,
    Heartbeat,
    InitialTask,
    InitialTaskResult,
    Registered,
    ResultAccepted,
    Started,
    Succeeded,
    parse_worker_message,
)
from app.domain.delivery_models import OpenedDelivery
from app.domain.models import JobState, VcsKind, VcsOutcome
from app.worker.state.session import WorkerSession
from app.worker.transport.client import FlowToolCallError, WorkerClient
from app.worker.transport.mcp import FlowToolCaller


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
    async def execute(self, task: ExecutionTask, secret=None, *, tool_caller=None) -> ExecutionResult:
        assert isinstance(task, InitialTask)
        assert task.repo == "owner/repo"
        return InitialTaskResult(session_id="session-1", last_message="done", exit_code=0)


class ScriptedConnection:
    def __init__(self) -> None:
        self.result_ready = asyncio.Event()
        self.tool_ready = asyncio.Event()
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
        message = parse_worker_message(data)
        if isinstance(message, (Succeeded, Failed)):
            self.result_ready.set()
        if isinstance(message, FlowToolRequest):
            self.tool_ready.set()

    async def recv(self) -> str:
        if not self.messages:
            raise ConnectionError("script complete")
        if '"type":"result_accepted"' in self.messages[0]:
            await self.result_ready.wait()
        return self.messages.pop(0)


@pytest.mark.asyncio
async def test_client_acknowledges_and_executes_one_assignment(tmp_path) -> None:
    connection = ScriptedConnection()
    client = WorkerClient(session=WorkerSession(tmp_path),
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


@pytest.mark.parametrize("fails", [False, True])
async def test_worker_redacts_echoed_push_capability_before_journal_and_wire(tmp_path, fails):
    from app.contracts.protocol import Failed, parse_server_message
    from app.worker.state.session import WorkerSession
    from tests.test_worker_session import _assignment

    class EchoingExecutor:
        async def execute(self, task, secret=None, *, tool_caller=None):
            if fails:
                raise RuntimeError("command echoed push-secret")
            return InitialTaskResult(session_id="session", last_message="push-secret", exit_code=0)

    class InspectJournal(WorkerSession):
        def record_terminal(self, terminal):
            assert "push-secret" not in terminal.model_dump_json()
            return super().record_terminal(terminal)

    connection = ScriptedConnection()
    assignment = parse_server_message(connection.messages[1])
    assert isinstance(assignment, Assignment)
    assignment.secret = _assignment().secret
    connection.messages[1] = assignment.model_dump_json()
    client = WorkerClient(worker_id="worker-1", worker_token="worker-secret",
                           serve_ws_url="ws://serve/internal/workers/connect",
                           executor=EchoingExecutor(), session=InspectJournal(tmp_path))
    with pytest.raises(ConnectionError, match="script complete"):
        await client.run_connection(connection)
    terminal = parse_worker_message(connection.sent[2])
    assert isinstance(terminal, Failed if fails else Succeeded)
    assert "push-secret" not in terminal.model_dump_json()
    assert "[REDACTED]" in terminal.model_dump_json()


class ToolCallingExecutor:
    async def execute(
        self, task: ExecutionTask, secret=None, *, tool_caller: FlowToolCaller
    ) -> ExecutionResult:
        result = await tool_caller.call_tool(
            "flow.open_pr",
            {"title": "Implement", "body": "Done"},
            vcs_outcome=VcsOutcome(
                backend=VcsKind.GIT,
                branch="flow/3",
                commit_sha="abc",
                remote_sha="abc",
            ),
        )
        assert isinstance(result, OpenedDelivery) and result.pr_number == 17
        return InitialTaskResult(session_id="session-1", last_message="done", exit_code=0)


class ToolConnection:
    def __init__(self) -> None:
        self.result_ready = asyncio.Event()
        self.tool_ready = asyncio.Event()
        self.sent: list[str] = []
        self.step = 0

    async def send(self, data: str) -> None:
        self.sent.append(data)
        message = parse_worker_message(data)
        if isinstance(message, (Succeeded, Failed)):
            self.result_ready.set()
        if isinstance(message, FlowToolRequest):
            self.tool_ready.set()

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
            await self.tool_ready.wait()
            request = parse_worker_message(self.sent[-1])
            assert isinstance(request, FlowToolRequest)
            return FlowToolResponse(
                call_id=request.call_id, result=OpenedDelivery(pr_number=17, url="https://github.com/owner/repo/pull/17", head_sha="abc", delivery_version=1)
            ).model_dump_json()
        if self.step == 4:
            await self.result_ready.wait()
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
async def test_client_multiplexes_tool_call_while_executor_is_running(tmp_path) -> None:
    executor = ToolCallingExecutor()
    client = WorkerClient(session=WorkerSession(tmp_path),
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="ws://serve/internal/workers/connect",
        executor=executor,
    )
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

    async def execute(self, task: ExecutionTask, secret=None, *, tool_caller=None) -> ExecutionResult:
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
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_heartbeat_failure_preserves_the_running_executor_for_reconnect(tmp_path) -> None:
    from app.worker.state.session import WorkerSession
    executor = CancellableExecutor()
    client = WorkerClient(
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="ws://unused",
        executor=executor,
        session=WorkerSession(tmp_path),
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
        from app.contracts.protocol import ResumeAssignment

        heartbeat = asyncio.Event()
        class ResumedConnection:
            first = True
            async def send(self, data):
                if isinstance(parse_worker_message(data), Heartbeat):
                    heartbeat.set()
            async def recv(self):
                if self.first:
                    self.first = False
                    return ResumeAssignment(job_id=5, assignment_token='assignment-1',
                                            connection_epoch='epoch-2', heartbeat_interval=0.01).model_dump_json()
                await asyncio.Event().wait()
        resumed = asyncio.create_task(client.run_connection(ResumedConnection()))
        try:
            await asyncio.wait_for(heartbeat.wait(), 1)
            assert client.connected  # Recovery succeeded even though the executor is still busy.
            assert client._execution is not None and not client._execution.done()
        finally:
            resumed.cancel()
            await asyncio.gather(resumed, return_exceptions=True)
    finally:
        await client._cancel_execution()
    assert executor.cancelled.is_set() is True


@pytest.mark.parametrize("fails", [False, True])
async def test_completed_disconnected_executor_is_journaled_before_recovery(tmp_path, fails):
    from app.worker.state.session import WorkerSession
    from tests.test_worker_session import _assignment

    # Execution finishes after the driver lost its connection; registration must carry its result.
    class CompletedExecutor:
        async def execute(self, task, secret=None, *, tool_caller=None):
            if fails:
                raise RuntimeError("push-secret was echoed")
            return InitialTaskResult(session_id="session", last_message="push-secret", exit_code=0)

    journal = WorkerSession(tmp_path)
    assignment = _assignment()
    journal.record_received(assignment)
    journal.record_started()
    executor = CompletedExecutor()
    client = WorkerClient(worker_id="worker-a", worker_token="secret", serve_ws_url="ws://test",
                          executor=executor, session=journal)
    client.session.assignment = assignment
    execution = asyncio.create_task(executor.execute(assignment.task, assignment.secret))
    client._execution = execution
    await asyncio.gather(execution, return_exceptions=True)
    recovery = client._recovery_descriptor()
    assert recovery.state == ("failed" if fails else "succeeded")
    assert recovery.result_digest == journal.load().result_digest
    assert journal.load().terminal is not None
    assert "push-secret" not in journal.path.read_text()
    assert "[REDACTED]" in journal.path.read_text()


@pytest.mark.parametrize("rejected", [False, True])
async def test_mcp_shutdown_consumes_late_tool_response_before_delivering_result(tmp_path, monkeypatch, rejected):
    import httpx

    from app.domain.models import VcsExecutionContext
    from app.worker.transport.mcp import WorkerMCP

    # Codex exits while open_pr waits on control. HTTP shutdown cancels the waiter,
    # but the original socket still needs to consume control's reply and deliver the job result.
    closed = asyncio.Event()
    outcome = VcsOutcome(backend=VcsKind.GIT, branch="flow/3", commit_sha="abc", remote_sha="abc")

    async def verify(context):
        return outcome

    monkeypatch.setattr("app.worker.transport.mcp.verify_pushed_outcome", verify)

    class LateConnection(ToolConnection):
        async def recv(self):
            if self.step == 2:
                await closed.wait()
                raw = await super().recv()
                if rejected:
                    request = parse_worker_message(self.sent[-1])
                    return FlowToolResponse(call_id=request.call_id, error="upstream rejected",
                                            error_kind=FlowToolErrorKind.UPSTREAM).model_dump_json()
                return raw
            return await super().recv()

    connection = LateConnection()

    class Executor:
        async def execute(self, task, secret=None, *, tool_caller):
            context = VcsExecutionContext(tmp_path, "flow/3", "origin", None, None, None, None, None)
            async with httpx.AsyncClient(trust_env=False) as http:
                async with WorkerMCP(tool_caller, context) as mcp:
                    request = asyncio.create_task(http.post(mcp.url, headers={
                        "Authorization": f"Bearer {mcp.token.get_secret_value()}",
                    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                        "name": "open_pr", "arguments": {"title": "Implement", "body": "Done"},
                    }}))
                    await connection.tool_ready.wait()
                await asyncio.gather(request, return_exceptions=True)
            closed.set()
            return InitialTaskResult(session_id="session-1", last_message="Codex timed out", exit_code=124)

    session = WorkerSession(tmp_path)
    client = WorkerClient(worker_id="worker-1", worker_token="token", serve_ws_url="unused",
                          executor=Executor(), session=session)
    with pytest.raises(ConnectionError, match="script complete"):
        await asyncio.wait_for(client.run_connection(connection), 5)
    messages = [parse_worker_message(raw) for raw in connection.sent]
    assert sum(isinstance(message, FlowToolRequest) for message in messages) == 1
    terminals = [message for message in messages if isinstance(message, Succeeded)]
    assert len(terminals) == 1 and terminals[0].result.exit_code == 124
    assert session.load().acknowledged
