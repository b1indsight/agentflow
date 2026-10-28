import asyncio
from contextlib import asynccontextmanager

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
    Register,
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
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.state.execution import ExecutionState
from app.worker.transport.client import WorkerClient
from app.worker.transport.mcp import FlowToolCaller, WorkerMCP
from tests.test_worker_session import _assignment


class RecordingExecutor:
    async def execute(self, task: ExecutionTask, secret=None, *, tool_caller=None) -> ExecutionResult:
        assert isinstance(task, InitialTask)
        assert task.repo == "owner/repo"
        return InitialTaskResult(session_id="session-1", last_message="done", exit_code=0)

    async def reclaim_workspace(self) -> None:
        raise AssertionError("no stop was requested")


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
    state = ExecutionState(tmp_path)
    client = WorkerClient(read_history=state.read,
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="ws://serve/internal/workers/connect",
        execution=WorkerExecution(worker_id="worker-1", executor=RecordingExecutor(), state=state),
    )

    with pytest.raises(ConnectionError, match="script complete"):
        await client.run_connection(connection)

    messages = [parse_worker_message(raw) for raw in connection.sent]
    assert isinstance(messages[1], Started)
    assert isinstance(messages[2], Succeeded)
    assert messages[2].assignment_token == "assignment-1"


@pytest.mark.parametrize("failure", ["send", "cancel", "journal"])
async def test_assignment_never_launches_before_started_is_sent_and_persisted(tmp_path, failure):
    # A disconnected Started send or failed journal write must not start untracked work.
    class Journal(ExecutionState):
        def record_started(self):
            assert any(isinstance(parse_worker_message(raw), Started) for raw in connection.sent)
            if failure == "journal":
                raise OSError("journal unavailable")
            return super().record_started()

    class Connection(ScriptedConnection):
        async def send(self, raw):
            if isinstance(parse_worker_message(raw), Started):
                assert state.load().state == "received"
                if failure == "send":
                    raise ConnectionError("send failed")
                if failure == "cancel":
                    raise asyncio.CancelledError
            await super().send(raw)

    class Executor(RecordingExecutor):
        invocations = 0

        async def execute(self, *args, **kwargs):
            self.invocations += 1
            return await super().execute(*args, **kwargs)

    state, executor, connection = Journal(tmp_path), Executor(), Connection()
    owner = WorkerExecution(worker_id="worker-1", executor=executor, state=state)
    client = WorkerClient(worker_id="worker-1", worker_token="test", serve_ws_url="unused",
                          execution=owner, read_history=state.read)
    error = asyncio.CancelledError if failure == "cancel" else OSError
    with pytest.raises(error):
        await client.run_connection(connection)
    await owner.shutdown()
    assert state.load().state == "received"
    assert executor.invocations == 0


@pytest.mark.parametrize("journal_unavailable", [False, True])
async def test_completion_between_register_and_resume_is_persisted_without_reexecution(tmp_path, journal_unavailable):
    # Register can report a live executor that finishes before control replies with ResumeAssignment.
    finish, collected = asyncio.Event(), asyncio.Event()

    class Journal(ExecutionState):
        blocked = journal_unavailable

        def record_terminal(self, terminal):
            collected.set()
            if self.blocked:
                raise OSError("journal unavailable")
            return super().record_terminal(terminal)

    class Executor(RecordingExecutor):
        invocations = 0

        async def execute(self, *args, **kwargs):
            self.invocations += 1
            await finish.wait()
            return await super().execute(*args, **kwargs)

    class Connection(ScriptedConnection):
        async def send(self, raw):
            message = parse_worker_message(raw)
            if isinstance(message, Register) and not collected.is_set():
                assert message.recovery.state == "started" and message.recovery.executor_active
                finish.set()
                await collected.wait()
            if isinstance(message, Succeeded):
                assert state.load().terminal.result == message.result
            await super().send(raw)

    state, executor = Journal(tmp_path), Executor()
    owner = WorkerExecution(worker_id="worker-1", executor=executor, state=state)
    owner.receive(_assignment())
    owner.start()

    async def reconnect():
        connection = Connection()
        connection.messages[:2] = [ResumeAssignment(
            job_id=5, assignment_token="assignment-1", connection_epoch="epoch-2", heartbeat_interval=60,
        ).model_dump_json()]
        connection.messages[1] = ResultAccepted(job_id=5, assignment_token="assignment-1",
            connection_epoch="epoch-2", state=JobState.DONE).model_dump_json()
        client = WorkerClient(worker_id="worker-1", worker_token="test", serve_ws_url="unused",
                              execution=owner, read_history=state.read)
        await asyncio.wait_for(client.run_connection(connection), 2)

    try:
        if journal_unavailable:
            with pytest.raises(OSError, match="journal unavailable"):
                await reconnect()
            assert owner.lease() is None
            assert state.load().state == "started"
            state.blocked = False
        with pytest.raises(ConnectionError, match="script complete"):
            await reconnect()
        assert executor.invocations == 1
        assert state.load().acknowledged and state.load().terminal is not None
    finally:
        finish.set()
        state.blocked = False
        await owner.shutdown()


@pytest.mark.parametrize("fails", [False, True])
async def test_worker_redacts_echoed_push_capability_before_journal_and_wire(tmp_path, fails):
    class EchoingExecutor:
        async def execute(self, task, secret=None, *, tool_caller=None):
            if fails:
                raise RuntimeError("command echoed push-secret")
            return InitialTaskResult(session_id="session", last_message="push-secret", exit_code=0)

    class InspectJournal(ExecutionState):
        def record_terminal(self, terminal):
            assert "push-secret" not in terminal.model_dump_json()
            return super().record_terminal(terminal)

    connection = ScriptedConnection()
    assignment = parse_server_message(connection.messages[1])
    assert isinstance(assignment, Assignment)
    assignment.secret = _assignment().secret
    connection.messages[1] = assignment.model_dump_json()
    state = InspectJournal(tmp_path)
    client = WorkerClient(worker_id="worker-1", worker_token="worker-secret",
                           serve_ws_url="ws://serve/internal/workers/connect",
                           execution=WorkerExecution(worker_id="worker-1", executor=EchoingExecutor(), state=state),
                           read_history=state.read)
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

    async def reclaim_workspace(self) -> None:
        raise AssertionError("no stop was requested")


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
        self.invocations = 0

    async def execute(self, task: ExecutionTask, secret=None, *, tool_caller=None) -> ExecutionResult:
        self.invocations += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        raise AssertionError("unreachable")

    async def reclaim_workspace(self) -> None:
        raise AssertionError("no stop was requested")


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
async def test_replacing_client_preserves_running_executor_and_renews_lease(tmp_path, monkeypatch) -> None:
    # Replacing the communication object must exercise run()'s finally, not only its socket loop.
    executor = CancellableExecutor()
    session = ExecutionState(tmp_path)
    owner = WorkerExecution(worker_id="worker-1", executor=executor, state=session)
    client = WorkerClient(
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="ws://unused",
        execution=owner,
        read_history=session.read,
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

    disconnected = asyncio.Event()

    @asynccontextmanager
    async def connect(*args, **kwargs):
        try:
            yield BrokenHeartbeatConnection(assignment)
        finally:
            disconnected.set()

    monkeypatch.setattr("app.worker.transport.client.connect", connect)
    execution = asyncio.create_task(client.run())
    await executor.started.wait()
    try:
        await asyncio.wait_for(disconnected.wait(), 1)
        client.stop()
        await execution
        assert executor.cancelled.is_set() is False
        client = WorkerClient(
            worker_id="worker-1", worker_token="worker-secret", serve_ws_url="ws://unused",
            execution=owner, read_history=session.read,
        )
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
            assert executor.invocations == 1
            assert not executor.cancelled.is_set()
        finally:
            resumed.cancel()
            await asyncio.gather(resumed, return_exceptions=True)
    finally:
        await owner.shutdown()
    assert executor.cancelled.is_set() is True


@pytest.mark.asyncio
async def test_heartbeat_failure_interrupts_waiting_for_tool_reply(tmp_path) -> None:
    # A stalled control-side tool reply must not prevent a failed heartbeat from reconnecting.
    assignment = _assignment()
    state = ExecutionState(tmp_path)
    owner = WorkerExecution(worker_id="worker-1", executor=ToolCallingExecutor(), state=state)
    client = WorkerClient(
        worker_id="worker-1", worker_token="worker-secret", serve_ws_url="ws://unused",
        execution=owner, read_history=state.read,
    )
    connection = BrokenHeartbeatConnection(assignment)
    try:
        with pytest.raises(ConnectionError, match="connection lost"):
            await asyncio.wait_for(client.run_connection(connection), timeout=1)
        assert owner.lease() == assignment
        pending = await asyncio.wait_for(owner.next_tool_call(), 1)
        assert pending.request.name == "flow.open_pr"
        assert not pending.future.done()
    finally:
        await owner.shutdown()


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
                assert state.assignment is None and state.load().acknowledged
            return message.model_dump_json()

    state = ExecutionState(tmp_path)
    owner = WorkerExecution(worker_id="worker-1", executor=Executor(), state=state)
    client = WorkerClient(
        worker_id="worker-1",
        worker_token="test",
        serve_ws_url="ws://unused",
        execution=owner,
        read_history=state.read,
    )
    try:
        with pytest.raises(ConnectionError, match="controller stopped"):
            await asyncio.wait_for(client.run_connection(Disconnect()), 2)
        cancel_tool.set()
        await asyncio.wait_for(cancelled.wait(), 2)
        if finish_before_reconnect:
            await owner.wait_terminal("epoch-1")
        recovered = Reconnect()
        with pytest.raises(ConnectionError, match="script complete"):
            await asyncio.wait_for(client.run_connection(recovered), 2)
        calls = [message for message in recovered.sent if isinstance(message, FlowToolRequest)]
        assert [message.job_id for message in calls] == ([6] if finish_before_reconnect else [5, 6])
        assert len(invocations) == 2
        assert state.load().job_id == 6 and state.load().acknowledged
        assert state.assignment is None
    finally:
        cancel_tool.set()
        await owner.shutdown()


@pytest.mark.parametrize("sent_before_disconnect", [False, True])
async def test_replacing_client_replays_original_tool_call(tmp_path, sent_before_disconnect):
    # Both a ready-but-unsent call and a lost reply must retain the original caller across clients.
    assignment = _assignment()
    session = ExecutionState(tmp_path)
    executor = ToolCallingExecutor()
    owner = WorkerExecution(worker_id="worker-1", executor=executor, state=session)
    owner.receive(assignment)
    owner.start()
    client = WorkerClient(worker_id="worker-1", worker_token="test", serve_ws_url="ws://unused",
                          execution=owner, read_history=session.read)
    pending = await asyncio.wait_for(owner.next_tool_call(), 1)

    class ReconnectedTool(ToolConnection):
        def __init__(self, epoch):
            super().__init__()
            self.epoch = epoch

        async def recv(self):
            if self.step == 0:
                self.step = 2
                return ResumeAssignment(job_id=assignment.job_id,
                                        assignment_token=assignment.assignment_token,
                                        connection_epoch=self.epoch, heartbeat_interval=60).model_dump_json()
            response = parse_server_message(await super().recv())
            if isinstance(response, ResultAccepted):
                response = response.model_copy(update={"connection_epoch": self.epoch})
            return response.model_dump_json()

    class Disconnect(ReconnectedTool):
        async def recv(self):
            if self.step == 0:
                return await super().recv()
            if sent_before_disconnect:
                await self.tool_ready.wait()
            raise ConnectionError("controller stopped")

    try:
        with pytest.raises(ConnectionError, match="controller stopped"):
            await asyncio.wait_for(client.run_connection(Disconnect("epoch-1")), 1)
        assert not pending.future.done()
        replacement = WorkerClient(worker_id="worker-1", worker_token="test", serve_ws_url="ws://unused",
                                   execution=owner, read_history=session.read)
        recovered = ReconnectedTool("epoch-2")
        with pytest.raises(ConnectionError, match="script complete"):
            await asyncio.wait_for(replacement.run_connection(recovered), 1)
        calls = [parse_worker_message(raw) for raw in recovered.sent]
        requests = [message for message in calls if isinstance(message, FlowToolRequest)]
        assert len(requests) == 1 and requests[0].call_id == pending.request.call_id
        assert requests[0].connection_epoch == "epoch-2"
        assert pending.future.result() == PreparedDelivery(head_sha="abc", delivery_version=1)
        assert session.load().acknowledged
    finally:
        await owner.shutdown()


@pytest.mark.parametrize("fails", [False, True])
async def test_completed_disconnected_executor_is_journaled_before_recovery(tmp_path, fails):
    # No communication object exists when execution finishes. Reading the journal must suffice.
    class CompletedExecutor:
        async def execute(self, task, secret=None, *, tool_caller=None):
            if fails:
                raise RuntimeError("push-secret was echoed")
            return InitialTaskResult(session_id="session", last_message="push-secret", exit_code=0)

    written = asyncio.Event()

    class Journal(ExecutionState):
        def record_terminal(self, terminal):
            record = super().record_terminal(terminal)
            written.set()
            return record

    journal = Journal(tmp_path)
    assignment = _assignment()
    executor = CompletedExecutor()
    owner = WorkerExecution(worker_id="worker-a", executor=executor, state=journal)
    owner.receive(assignment)
    owner.start()
    await asyncio.wait_for(written.wait(), 1)
    assert journal.load().terminal is not None
    recovery = owner.recovery()
    assert recovery.state == ("failed" if fails else "succeeded")
    assert recovery.result_digest == journal.load().result_digest
    assert journal.load().terminal is not None
    assert "push-secret" not in journal.path.read_text()
    assert "[REDACTED]" in journal.path.read_text()


@pytest.mark.parametrize("subsequent_call", [False, True])
async def test_tool_cancelled_after_borrow_is_not_sent_or_blocking_completion(tmp_path, monkeypatch, subsequent_call):
    # MCP may cancel after the queue observer returns, before the socket driver resumes sending.
    caller = None
    outcome = VcsOutcome(backend=VcsKind.GIT, branch="flow/3", commit_sha="abc", remote_sha="abc")

    class Executor(RecordingExecutor):
        async def execute(self, task, secret=None, *, tool_caller):
            nonlocal caller
            caller = asyncio.create_task(tool_caller.call_tool(
                "flow.open_pr", {}, call_id="cancel-before-send", vcs_outcome=outcome,
            ))
            with pytest.raises(asyncio.CancelledError):
                await caller
            if subsequent_call:
                return await ToolCallingExecutor().execute(task, secret, tool_caller=tool_caller)
            return await super().execute(task, secret, tool_caller=tool_caller)

    class CancelAfterBorrow(WorkerExecution):
        async def next_tool_call(self):
            pending = await super().next_tool_call()
            if pending.request.call_id == "cancel-before-send":
                assert caller is not None
                caller.cancel()
            return pending

    state = ExecutionState(tmp_path)
    owner = CancelAfterBorrow(worker_id="worker-1", executor=Executor(), state=state)
    client = WorkerClient(worker_id="worker-1", worker_token="test", serve_ws_url="unused",
                          execution=owner, read_history=state.read)
    connection = ToolConnection() if subsequent_call else ScriptedConnection()
    original_send = connection.send

    async def send(raw):
        message = parse_worker_message(raw)
        if isinstance(message, FlowToolRequest):
            assert message.call_id != "cancel-before-send", "cancelled request reached the socket"
        await original_send(raw)

    monkeypatch.setattr(connection, "send", send)
    try:
        with pytest.raises(ConnectionError, match="script complete"):
            await asyncio.wait_for(client.run_connection(connection), 2)
        messages = [parse_worker_message(raw) for raw in connection.sent]
        assert sum(isinstance(message, FlowToolRequest) for message in messages) == int(subsequent_call)
        assert sum(isinstance(message, Succeeded) for message in messages) == 1
        assert state.load().acknowledged
    finally:
        await owner.shutdown()


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

    session = ExecutionState(tmp_path)
    client = WorkerClient(worker_id="worker-1", worker_token="token", serve_ws_url="unused",
                          execution=WorkerExecution(worker_id="worker-1", executor=Executor(), state=session),
                          read_history=session.read)
    with pytest.raises(ConnectionError, match="script complete"):
        await asyncio.wait_for(client.run_connection(connection), 5)
    messages = [parse_worker_message(raw) for raw in connection.sent]
    assert sum(isinstance(message, FlowToolRequest) for message in messages) == 1
    terminals = [message for message in messages if isinstance(message, Succeeded)]
    assert len(terminals) == 1 and terminals[0].result.exit_code == 124
    assert session.load().acknowledged
