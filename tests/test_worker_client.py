import asyncio

import httpx
import pytest

from app.contracts.protocol import (
    Assignment,
    ExecutionResult,
    ExecutionTask,
    Failed,
    FlowToolRequest,
    FlowToolResponse,
    Heartbeat,
    InitialTask,
    InitialTaskResult,
    Registered,
    ReplayResult,
    ResultAccepted,
    ResumeAssignment,
    Started,
    Succeeded,
    parse_server_message,
    parse_worker_message,
)
from app.domain.delivery_models import PreparedDelivery
from app.domain.models import JobState, VcsExecutionContext, VcsKind, VcsOutcome
from app.worker.state.session import WorkerSession
from app.worker.transport.client import WorkerClient
from app.worker.transport.mcp import FlowToolCaller, WorkerMCP
from tests.test_worker_session import _assignment


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
        assert result == PreparedDelivery(head_sha="abc", delivery_version=1)
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
                call_id=request.call_id, result=PreparedDelivery(head_sha="abc", delivery_version=1)
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
    def __init__(self, assignment: Assignment) -> None:
        self.messages = iter([
            Registered(heartbeat_interval=0.01, connection_epoch=assignment.connection_epoch).model_dump_json(),
            assignment.model_dump_json(),
        ])

    async def send(self, data: str) -> None:
        if isinstance(parse_worker_message(data), Heartbeat):
            raise ConnectionError("connection lost")

    async def recv(self) -> str:
        message = next(self.messages, None)
        if message is not None:
            return message
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_heartbeat_failure_preserves_the_running_executor_for_reconnect(tmp_path) -> None:
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

    execution = asyncio.create_task(client.run_connection(BrokenHeartbeatConnection(assignment)))
    await executor.started.wait()
    try:
        with pytest.raises(ConnectionError, match="connection lost"):
            await execution
        assert executor.cancelled.is_set() is False
        heartbeat = asyncio.Event()
        class ResumedConnection:
            first = True
            async def send(self, data):
                message = parse_worker_message(data)
                if isinstance(message, Heartbeat):
                    assert message.job_id == assignment.job_id
                    assert message.assignment_token == assignment.assignment_token
                    assert message.connection_epoch == "epoch-2"
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


@pytest.mark.asyncio
async def test_heartbeat_failure_interrupts_waiting_for_tool_reply(tmp_path) -> None:
    # A stalled control-side tool reply must not prevent a failed heartbeat from reconnecting.
    assignment = _assignment()
    client = WorkerClient(
        worker_id="worker-1", worker_token="worker-secret", serve_ws_url="ws://unused",
        executor=ToolCallingExecutor(), session=WorkerSession(tmp_path),
    )
    connection = BrokenHeartbeatConnection(assignment)
    try:
        with pytest.raises(ConnectionError, match="connection lost"):
            await asyncio.wait_for(client.run_connection(connection), timeout=1)
        assert client._execution is not None and not client._execution.done()
        pending = client._tool_requests.get_nowait()
        assert pending.request.name == "flow.open_pr"
        assert not pending.future.done()
    finally:
        await client._cancel_execution()


@pytest.mark.parametrize("finish_before_reconnect", [False, True])
async def test_cancelled_disconnected_tool_does_not_leak_into_resumed_or_next_assignment(
    tmp_path, finish_before_reconnect
):
    # MCP can cancel a requeued tool during a disconnect. The live executor may
    # resume, or its terminal may replay before another assignment uses the socket.
    cancel_tool, cancelled = asyncio.Event(), asyncio.Event()
    invocations = []
    outcome = VcsOutcome(backend=VcsKind.GIT, branch="flow/3", commit_sha="abc", remote_sha="abc")

    class Executor:
        async def execute(self, task, secret=None, *, tool_caller):
            invocations.append(task)
            if len(invocations) == 1:
                call = asyncio.create_task(
                    tool_caller.call_tool(
                        "flow.open_pr",
                        {},
                        call_id="retired",
                        vcs_outcome=outcome,
                    )
                )
                try:
                    await cancel_tool.wait()
                finally:
                    call.cancel()
                    await asyncio.gather(call, return_exceptions=True)
                    cancelled.set()
                if finish_before_reconnect:
                    return InitialTaskResult(
                        session_id="session-1", last_message="timed out", exit_code=124
                    )
            return await ToolCallingExecutor().execute(task, secret, tool_caller=tool_caller)

    class Disconnect(ToolConnection):
        async def recv(self):
            if self.step == 2:
                await self.tool_ready.wait()
                raise ConnectionError("controller stopped")
            return await super().recv()

    assignment = _assignment()
    following = assignment.model_copy(
        update={"job_id": 6, "assignment_token": "next-token", "connection_epoch": "epoch-2"}
    )

    class Reconnect:
        def __init__(self):
            self.sent = []
            self.messages = asyncio.Queue()
            response = ReplayResult if finish_before_reconnect else ResumeAssignment
            self.messages.put_nowait(
                response(
                    job_id=5,
                    assignment_token="assignment-1",
                    connection_epoch="epoch-2",
                    heartbeat_interval=60,
                )
            )

        async def send(self, raw):
            message = parse_worker_message(raw)
            self.sent.append(message)
            if isinstance(message, FlowToolRequest):
                assert message.call_id != "retired"
                assert message.connection_epoch == "epoch-2"
                self.messages.put_nowait(
                    FlowToolResponse(
                        call_id=message.call_id,
                        result=PreparedDelivery(head_sha="abc", delivery_version=1),
                    )
                )
            elif isinstance(message, Succeeded):
                self.messages.put_nowait(
                    ResultAccepted(
                        job_id=message.job_id,
                        assignment_token=message.assignment_token,
                        connection_epoch="epoch-2",
                        state=JobState.DONE,
                    )
                )
                self.messages.put_nowait(
                    following if message.job_id == 5 else ConnectionError("script complete")
                )

        async def recv(self):
            message = await self.messages.get()
            if isinstance(message, Exception):
                raise message
            if isinstance(message, Assignment):
                assert client.session.assignment is None and client.session.load().acknowledged
            return message.model_dump_json()

    client = WorkerClient(
        worker_id="worker-1",
        worker_token="test",
        serve_ws_url="ws://unused",
        executor=Executor(),
        session=WorkerSession(tmp_path),
    )
    try:
        with pytest.raises(ConnectionError, match="controller stopped"):
            await asyncio.wait_for(client.run_connection(Disconnect()), 2)
        cancel_tool.set()
        await asyncio.wait_for(cancelled.wait(), 2)
        if finish_before_reconnect:
            await client._execution
        recovered = Reconnect()
        with pytest.raises(ConnectionError, match="script complete"):
            await asyncio.wait_for(client.run_connection(recovered), 2)
        calls = [message for message in recovered.sent if isinstance(message, FlowToolRequest)]
        assert [message.job_id for message in calls] == ([6] if finish_before_reconnect else [5, 6])
        assert len(invocations) == 2
        assert client.session.load().job_id == 6 and client.session.load().acknowledged
        assert client.session.assignment is None
    finally:
        cancel_tool.set()
        await client._cancel_execution()


async def test_disconnect_with_queued_tool_replays_it_after_controller_restart(tmp_path):
    # Disconnect and queue.get can complete in the same scheduling turn. The
    # pending call must survive even when no bytes of its request were sent.
    assignment = _assignment()
    session = WorkerSession(tmp_path)
    session.record_received(assignment)
    session.record_started()
    executor = ToolCallingExecutor()
    client = WorkerClient(worker_id="worker-1", worker_token="test", serve_ws_url="ws://unused",
                          executor=executor, session=session)
    client._execution = asyncio.create_task(executor.execute(assignment.task, tool_caller=client))
    await asyncio.sleep(0)

    class ReconnectedTool(ToolConnection):
        async def recv(self):
            if self.step == 0:
                self.step = 2
                return ResumeAssignment(job_id=assignment.job_id,
                                        assignment_token=assignment.assignment_token,
                                        connection_epoch="epoch-1", heartbeat_interval=60).model_dump_json()
            return await super().recv()

    class Disconnect(ReconnectedTool):
        async def recv(self):
            if self.step == 0:
                return await super().recv()
            raise ConnectionError("controller stopped")

    try:
        with pytest.raises(ConnectionError, match="controller stopped"):
            await client.run_connection(Disconnect())
        recovered = ReconnectedTool()
        with pytest.raises(ConnectionError, match="script complete"):
            await asyncio.wait_for(client.run_connection(recovered), 1)
        calls = [parse_worker_message(raw) for raw in recovered.sent]
        assert len([message for message in calls if isinstance(message, FlowToolRequest)]) == 1
        assert session.load().acknowledged
    finally:
        await client._cancel_execution()


@pytest.mark.parametrize("fails", [False, True])
async def test_completed_disconnected_executor_is_journaled_before_recovery(tmp_path, fails):
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


async def test_mcp_shutdown_consumes_late_tool_response_before_delivering_result(tmp_path, monkeypatch):
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
