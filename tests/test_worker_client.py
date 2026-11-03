import asyncio
from contextlib import asynccontextmanager
from unittest.mock import Mock

import httpx
import pytest

from app.communication.session import Session
from app.contracts.conversation import HistoryPage
from app.contracts.protocol import (
    AcceptedResult,
    AcceptResult,
    Assignment,
    AssignmentArguments,
    ConversationQuery,
    ConversationResult,
    ExecuteResult,
    ExecutionResult,
    ExecutionTask,
    FlowToolRequest,
    FlowToolResponse,
    Heartbeat,
    HistoryArguments,
    InitialTask,
    InitialTaskResult,
    RecoveryDecision,
    Register,
    Registered,
    ResultAccepted,
    ServerFrame,
    Started,
    WorkerPayload,
    accept_result_id,
    call_header,
    execution_call_id,
    frame_header,
    parse_server_message,
    parse_worker_message,
    result_header,
    terminal_id,
)
from app.domain.delivery_models import PreparedDelivery
from app.domain.models import VcsExecutionContext, VcsKind, VcsOutcome
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.state.execution import ExecutionState
from app.worker.transport.client import WorkerClient
from app.worker.transport.mcp import FlowToolCaller, WorkerMCP
from tests.test_conversation_history import SESSION
from tests.test_remote_e2e import QueueConnection
from tests.test_worker_session import _assignment


def assignment_frame(work: AssignmentArguments, epoch: str = "epoch-1") -> ServerFrame:
    return ServerFrame(
        **frame_header(epoch),
        control=None,
        payload=[
            Assignment(
                **call_header(execution_call_id(work.job_id, work.assignment_token)),
                method="worker.execute",
                arguments=work,
            )
        ],
    )


def registered_frame(
    epoch: str = "epoch-1",
    *,
    recovery: RecoveryDecision | None = None,
    interval: float = 60,
) -> Registered:
    return Registered(
        **frame_header(epoch),
        control="registered",
        payload=[],
        heartbeat_interval=interval,
        recovery=recovery,
    )


def accepted_frame(call: AcceptResult, epoch: str = "epoch-1") -> ServerFrame:
    return ServerFrame(
        **frame_header(epoch),
        control=None,
        payload=[
            ResultAccepted(
                **result_header(call.message_id),
                method="server.accept_result",
                result=AcceptedResult(
                    job_id=call.arguments.job_id,
                    assignment_token=call.arguments.assignment_token,
                    state="done",
                ),
            )
        ],
    )


def sent_items(raw_frames: list[str]) -> list[WorkerPayload]:
    return [item for raw in raw_frames for item in parse_worker_message(raw).payload]


class RecordingExecutor:
    async def execute(
        self, task: ExecutionTask, secret=None, *, tool_caller=None
    ) -> ExecutionResult:
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
        self.result_request: AcceptResult | None = None
        self.accepted = False
        self.epoch = "epoch-1"
        self.messages = [
            registered_frame().model_dump_json(),
            assignment_frame(_assignment()).model_dump_json(),
        ]

    async def send(self, data: str) -> None:
        self.sent.append(data)
        message = parse_worker_message(data)
        for item in message.payload:
            if isinstance(item, AcceptResult):
                self.result_request = item
                self.result_ready.set()
            if isinstance(item, FlowToolRequest):
                self.tool_ready.set()

    async def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        if not self.accepted:
            await self.result_ready.wait()
            assert self.result_request is not None
            self.accepted = True
            return accepted_frame(self.result_request, self.epoch).model_dump_json()
        raise ConnectionError("script complete")

    async def close(self, code: int = 1000) -> None:
        pass


async def test_stop_during_dial_never_registers_or_borrows_execution(monkeypatch):
    # stop() may run while connect.__aenter__ is still waiting for the server.
    dialing, release = asyncio.Event(), asyncio.Event()
    connection = ScriptedConnection()
    owner = Mock(
        spec=WorkerExecution,
        recovery=Mock(return_value=None),
        result_for_registration=Mock(return_value=None),
    )

    @asynccontextmanager
    async def connect(*args, **kwargs):
        dialing.set()
        await release.wait()
        yield connection

    monkeypatch.setattr("app.worker.transport.client.connect", connect)
    client = WorkerClient(
        worker_id="w",
        worker_token="test",
        serve_ws_url="unused",
        execution=owner,
        read_history=Mock(),
    )
    running = asyncio.create_task(client.run())
    await dialing.wait()
    client.stop()
    release.set()
    await asyncio.wait_for(running, 1)
    assert connection.sent == []
    owner.recovery.assert_not_called()
    owner.result_for_registration.assert_not_called()
    owner.shutdown.assert_not_called()


async def test_overlapping_connection_cannot_borrow_owner_or_change_online_state():
    # A rejected second socket must not reset the first connection's readiness.
    class IdleConnection(ScriptedConnection):
        async def recv(self):
            if self.messages:
                first = self.messages[0]
                self.messages.clear()
                return first
            await asyncio.Event().wait()

    owner = Mock(
        spec=WorkerExecution,
        recovery=Mock(side_effect=[None, AssertionError("second connection borrowed owner")]),
        result_for_registration=Mock(return_value=None),
    )
    client = WorkerClient(
        worker_id="w",
        worker_token="test",
        serve_ws_url="unused",
        execution=owner,
        read_history=Mock(),
    )
    first = asyncio.create_task(client.run_connection(IdleConnection()))
    try:
        async with asyncio.timeout(1):
            while not client.connected:
                await asyncio.sleep(0)
        second = ScriptedConnection()
        with pytest.raises(RuntimeError, match="peer already registered"):
            await client.run_connection(second)
        assert second.sent == []
        assert client.connected
        owner.recovery.assert_called_once_with()
        owner.result_for_registration.assert_called_once_with()
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    owner.shutdown.assert_not_called()


@pytest.mark.asyncio
async def test_client_acknowledges_and_executes_one_assignment(tmp_path) -> None:
    connection = ScriptedConnection()
    state = ExecutionState(tmp_path)
    client = WorkerClient(
        read_history=state.read,
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="ws://serve/internal/workers/connect",
        execution=WorkerExecution(worker_id="worker-1", executor=RecordingExecutor(), state=state),
    )

    with pytest.raises(ConnectionError, match="script complete"):
        await client.run_connection(connection)

    assert isinstance(parse_worker_message(connection.sent[0]), Register)
    messages = sent_items(connection.sent)
    assert len(messages) == 3
    assert isinstance(messages[0], Started)
    assert isinstance(messages[1], ExecuteResult) and messages[1].result is not None
    assert messages[1].assignment_token == "assignment-1"
    assert messages[1].reply_to == execution_call_id(5, "assignment-1")
    assert messages[1].message_id == terminal_id(messages[1].terminal())
    assert isinstance(messages[2], AcceptResult)
    assert messages[2].arguments.result_id == messages[1].message_id
    assert messages[2].message_id == accept_result_id(messages[1].message_id)


@pytest.mark.parametrize("failure", ["send", "cancel", "journal"])
async def test_assignment_never_launches_before_started_is_sent_and_persisted(tmp_path, failure):
    # A disconnected Started send or failed journal write must not start untracked work.
    class Journal(ExecutionState):
        def record_started(self):
            assert any(isinstance(item, Started) for item in sent_items(connection.sent))
            if failure == "journal":
                raise OSError("journal unavailable")
            return super().record_started()

    class Connection(ScriptedConnection):
        async def send(self, raw):
            if any(isinstance(item, Started) for item in parse_worker_message(raw).payload):
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
    client = WorkerClient(
        worker_id="worker-1",
        worker_token="test",
        serve_ws_url="unused",
        execution=owner,
        read_history=state.read,
    )
    error = asyncio.CancelledError if failure == "cancel" else OSError
    with pytest.raises(error):
        await client.run_connection(connection)
    await owner.shutdown()
    assert state.load().state == "received"
    assert executor.invocations == 0


@pytest.mark.parametrize("journal_unavailable", [False, True])
async def test_completion_between_register_and_resume_is_persisted_without_reexecution(
    tmp_path, journal_unavailable
):
    # Register can report a live executor that finishes before control's resume decision.
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
            for item in message.payload:
                if isinstance(item, ExecuteResult):
                    assert state.load().terminal.result == item.result
            await super().send(raw)

    state, executor = Journal(tmp_path), Executor()
    owner = WorkerExecution(worker_id="worker-1", executor=executor, state=state)
    owner.receive(_assignment())
    owner.start()

    async def reconnect():
        connection = Connection()
        connection.epoch = "epoch-2"
        connection.messages = [
            registered_frame(
                "epoch-2",
                recovery=RecoveryDecision(
                    action="resume",
                    job_id=5,
                    assignment_token="assignment-1",
                ),
            ).model_dump_json()
        ]
        client = WorkerClient(
            worker_id="worker-1",
            worker_token="test",
            serve_ws_url="unused",
            execution=owner,
            read_history=state.read,
        )
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
    frame = parse_server_message(connection.messages[1])
    assignment = frame.payload[0]
    assert isinstance(assignment, Assignment)
    assignment.arguments.secret = _assignment().secret
    connection.messages[1] = frame.model_dump_json()
    state = InspectJournal(tmp_path)
    client = WorkerClient(
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="ws://serve/internal/workers/connect",
        execution=WorkerExecution(worker_id="worker-1", executor=EchoingExecutor(), state=state),
        read_history=state.read,
    )
    with pytest.raises(ConnectionError, match="script complete"):
        await client.run_connection(connection)
    terminals = [item for item in sent_items(connection.sent) if isinstance(item, ExecuteResult)]
    assert len(terminals) == 1
    terminal = terminals[0]
    assert (terminal.error is not None) == fails
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


class ToolConnection(ScriptedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.step = 0

    async def recv(self) -> str:
        self.step += 1
        if self.step == 1:
            return registered_frame(self.epoch).model_dump_json()
        if self.step == 2:
            return assignment_frame(_assignment(), self.epoch).model_dump_json()
        if self.step == 3:
            await self.tool_ready.wait()
            requests = [item for item in sent_items(self.sent) if isinstance(item, FlowToolRequest)]
            assert len(requests) == 1
            request = requests[0]
            return ServerFrame(
                **frame_header(self.epoch),
                control=None,
                payload=[
                    FlowToolResponse(
                        **result_header(request.message_id),
                        method=request.method,
                        call_id=request.arguments.call_id,
                        result=PreparedDelivery(head_sha="abc", delivery_version=1),
                    )
                ],
            ).model_dump_json()
        if self.step == 4:
            await self.result_ready.wait()
            assert self.result_request is not None
            return accepted_frame(self.result_request, self.epoch).model_dump_json()
        raise ConnectionError("script complete")


class CancellableExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.invocations = 0

    async def execute(
        self, task: ExecutionTask, secret=None, *, tool_caller=None
    ) -> ExecutionResult:
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
    def __init__(self, assignment: AssignmentArguments) -> None:
        self.messages = iter(
            [
                registered_frame(interval=0.01).model_dump_json(),
                assignment_frame(assignment).model_dump_json(),
            ]
        )

    async def send(self, data: str) -> None:
        if isinstance(parse_worker_message(data), Heartbeat):
            raise ConnectionError("connection lost")

    async def recv(self) -> str:
        message = next(self.messages, None)
        if message is not None:
            return message
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self, code: int = 1000) -> None:
        pass


@pytest.mark.asyncio
async def test_replacing_client_preserves_running_executor_and_renews_lease(
    tmp_path, monkeypatch
) -> None:
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
    assignment = _assignment()

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
            worker_id="worker-1",
            worker_token="worker-secret",
            serve_ws_url="ws://unused",
            execution=owner,
            read_history=session.read,
        )
        heartbeat = asyncio.Event()

        class ResumedConnection:
            first = True

            async def close(self, code=1000):
                pass

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
                    return registered_frame(
                        "epoch-2",
                        interval=0.01,
                        recovery=RecoveryDecision(
                            action="resume",
                            job_id=5,
                            assignment_token="assignment-1",
                        ),
                    ).model_dump_json()
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
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="ws://unused",
        execution=owner,
        read_history=state.read,
    )
    connection = BrokenHeartbeatConnection(assignment)
    try:
        with pytest.raises(ConnectionError, match="connection lost"):
            await asyncio.wait_for(client.run_connection(connection), timeout=1)
        assert owner.lease() == assignment
        pending = await asyncio.wait_for(owner.next_tool_call(), 1)
        assert pending.request.method == "flow.open_pr"
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
    following = assignment.model_copy(update={"job_id": 6, "assignment_token": "next-token"})

    class Reconnect:
        def __init__(self):
            self.sent = []
            self.messages = asyncio.Queue()
            self.messages.put_nowait(
                registered_frame(
                    "epoch-2",
                    recovery=RecoveryDecision(
                        action="replay" if finish_before_reconnect else "resume",
                        job_id=5,
                        assignment_token="assignment-1",
                    ),
                )
            )

        async def close(self, code=1000):
            pass

        async def send(self, raw):
            message = parse_worker_message(raw)
            self.sent.append(raw)
            # This peer has not committed the offline result: Register+A receives replay,
            # and only the subsequent T/A batch can be accepted.
            if isinstance(message, Register):
                return
            for item in message.payload:
                if isinstance(item, Started) and item.arguments.job_id == 6:
                    assert state.assignment.job_id == 6 and state.load().state == "received"
                if isinstance(item, FlowToolRequest):
                    assert item.arguments.call_id != "retired"
                    assert message.connection_epoch == "epoch-2"
                    self.messages.put_nowait(
                        ServerFrame(
                            **frame_header("epoch-2"),
                            control=None,
                            payload=[
                                FlowToolResponse(
                                    **result_header(item.message_id),
                                    method=item.method,
                                    call_id=item.arguments.call_id,
                                    result=PreparedDelivery(head_sha="abc", delivery_version=1),
                                )
                            ],
                        )
                    )
                elif isinstance(item, AcceptResult):
                    self.messages.put_nowait(accepted_frame(item, "epoch-2"))
                    self.messages.put_nowait(
                        assignment_frame(following, "epoch-2")
                        if item.arguments.job_id == 5
                        else ConnectionError("script complete")
                    )

        async def recv(self):
            message = await self.messages.get()
            if isinstance(message, Exception):
                raise message
            return message.model_dump_json()

    class Journal(ExecutionState):
        def record_received(self, work):
            if work.job_id == 6:
                # Continuous reading may prefetch the next Assignment; application still waits for ACK.
                assert self.assignment is None and self.load().acknowledged
            return super().record_received(work)

    state = Journal(tmp_path)
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
            await owner.wait_terminal()
        recovered = Reconnect()
        with pytest.raises(ConnectionError, match="script complete"):
            await asyncio.wait_for(client.run_connection(recovered), 2)
        calls = [item for item in sent_items(recovered.sent) if isinstance(item, FlowToolRequest)]
        assert [item.arguments.job_id for item in calls] == (
            [6] if finish_before_reconnect else [5, 6]
        )
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
    client = WorkerClient(
        worker_id="worker-1",
        worker_token="test",
        serve_ws_url="ws://unused",
        execution=owner,
        read_history=session.read,
    )
    pending = await asyncio.wait_for(owner.next_tool_call(), 1)

    class ReconnectedTool(ToolConnection):
        def __init__(self, epoch):
            super().__init__()
            self.epoch = epoch

        async def recv(self):
            if self.step == 0:
                self.step = 2
                return registered_frame(
                    self.epoch,
                    recovery=RecoveryDecision(
                        action="resume",
                        job_id=assignment.job_id,
                        assignment_token=assignment.assignment_token,
                    ),
                ).model_dump_json()
            return await super().recv()

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
        replacement = WorkerClient(
            worker_id="worker-1",
            worker_token="test",
            serve_ws_url="ws://unused",
            execution=owner,
            read_history=session.read,
        )
        recovered = ReconnectedTool("epoch-2")
        with pytest.raises(ConnectionError, match="script complete"):
            await asyncio.wait_for(replacement.run_connection(recovered), 1)
        requests = [
            item for item in sent_items(recovered.sent) if isinstance(item, FlowToolRequest)
        ]
        assert requests == [pending.request]
        assert all(
            parse_worker_message(raw).connection_epoch == "epoch-2"
            for raw in recovered.sent
            if parse_worker_message(raw).payload
        )
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
async def test_tool_cancelled_after_borrow_is_not_sent_or_blocking_completion(
    tmp_path, monkeypatch, subsequent_call
):
    # MCP may cancel after the queue observer returns, before the socket driver resumes sending.
    caller = None
    outcome = VcsOutcome(backend=VcsKind.GIT, branch="flow/3", commit_sha="abc", remote_sha="abc")

    class Executor(RecordingExecutor):
        async def execute(self, task, secret=None, *, tool_caller):
            nonlocal caller
            caller = asyncio.create_task(
                tool_caller.call_tool(
                    "flow.open_pr",
                    {},
                    call_id="cancel-before-send",
                    vcs_outcome=outcome,
                )
            )
            with pytest.raises(asyncio.CancelledError):
                await caller
            if subsequent_call:
                return await ToolCallingExecutor().execute(task, secret, tool_caller=tool_caller)
            return await super().execute(task, secret, tool_caller=tool_caller)

    class CancelAfterBorrow(WorkerExecution):
        async def next_tool_call(self):
            pending = await super().next_tool_call()
            if pending.request.arguments.call_id == "cancel-before-send":
                assert caller is not None
                caller.cancel()
            return pending

    state = ExecutionState(tmp_path)
    owner = CancelAfterBorrow(worker_id="worker-1", executor=Executor(), state=state)
    client = WorkerClient(
        worker_id="worker-1",
        worker_token="test",
        serve_ws_url="unused",
        execution=owner,
        read_history=state.read,
    )
    connection = ToolConnection() if subsequent_call else ScriptedConnection()
    original_send = connection.send

    async def send(raw):
        message = parse_worker_message(raw)
        for item in message.payload:
            if isinstance(item, FlowToolRequest):
                assert item.arguments.call_id != "cancel-before-send", (
                    "cancelled request reached the socket"
                )
        await original_send(raw)

    monkeypatch.setattr(connection, "send", send)
    try:
        with pytest.raises(ConnectionError, match="script complete"):
            await asyncio.wait_for(client.run_connection(connection), 2)
        messages = sent_items(connection.sent)
        assert sum(isinstance(message, FlowToolRequest) for message in messages) == int(
            subsequent_call
        )
        assert sum(isinstance(message, ExecuteResult) for message in messages) == 1
        assert state.load().acknowledged
    finally:
        await owner.shutdown()


async def test_tool_cancelled_while_history_holds_write_lock_is_not_sent(tmp_path, monkeypatch):
    # A query response can hold the shared write lock after the owner lends a tool request.
    write_started, release, call_ready, borrowed = (asyncio.Event() for _ in range(4))
    to_server, to_worker = asyncio.Queue(), asyncio.Queue()
    caller = None

    class Connection(QueueConnection):
        async def send(self, raw):
            if any(
                isinstance(item, ConversationResult) for item in parse_worker_message(raw).payload
            ):
                write_started.set()
                await release.wait()
            await super().send(raw)

    class Executor(RecordingExecutor):
        async def execute(self, task, secret=None, *, tool_caller):
            nonlocal caller
            await call_ready.wait()
            caller = asyncio.create_task(
                tool_caller.call_tool(
                    "flow.open_pr",
                    {},
                    call_id="cancel-while-locked",
                    vcs_outcome=VcsOutcome(
                        backend=VcsKind.GIT, branch="flow/3", commit_sha="abc", remote_sha="abc"
                    ),
                )
            )
            with pytest.raises(asyncio.CancelledError):
                await caller
            return await super().execute(task, secret, tool_caller=tool_caller)

    original_send = Session.send

    async def observe_send(session, message, **kwargs):
        if any(isinstance(item, FlowToolRequest) for item in message.payload):
            borrowed.set()
        return await original_send(session, message, **kwargs)

    monkeypatch.setattr(Session, "send", observe_send)

    state = ExecutionState(tmp_path)
    owner = WorkerExecution(worker_id="worker", executor=Executor(), state=state)
    work = _assignment()
    owner.receive(work)
    owner.start()
    client = WorkerClient(
        worker_id="worker",
        worker_token="test",
        serve_ws_url="unused",
        execution=owner,
        read_history=lambda query: HistoryPage(session_id=query.session_id),
    )
    running = asyncio.create_task(client.run_connection(Connection(to_server, to_worker)))
    try:
        await to_server.get()
        await to_worker.put(
            registered_frame(
                "new",
                recovery=RecoveryDecision(
                    action="resume",
                    job_id=work.job_id,
                    assignment_token=work.assignment_token,
                ),
            ).model_dump_json()
        )
        query = ConversationQuery(
            **call_header(), method="worker.history", arguments=HistoryArguments(session_id=SESSION)
        )
        await to_worker.put(
            ServerFrame(**frame_header("new"), control=None, payload=[query]).model_dump_json()
        )
        await asyncio.wait_for(write_started.wait(), 1)
        call_ready.set()
        await asyncio.wait_for(borrowed.wait(), 1)
        assert caller is not None
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        release.set()
        frames = []
        while len(frames) < 2:
            frame = parse_worker_message(await asyncio.wait_for(to_server.get(), 1))
            if frame.control is None and frame.ack and not frame.payload:
                continue
            frames.append(frame)
        history = frames[0].payload
        assert len(history) == 1 and isinstance(history[0], ConversationResult)
        assert history[0].reply_to == query.message_id
        terminal = frames[1].payload
        assert len(terminal) == 2 and isinstance(terminal[0], ExecuteResult), (
            "cancelled tool must never reach the transport"
        )
        assert isinstance(terminal[1], AcceptResult)
        await to_worker.put(accepted_frame(terminal[1], "new").model_dump_json())
        async with asyncio.timeout(1):
            while not state.load().acknowledged:
                await asyncio.sleep(0)
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await owner.shutdown()


async def test_mcp_shutdown_consumes_late_tool_response_before_delivering_result(
    tmp_path, monkeypatch
):
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
            context = VcsExecutionContext(
                tmp_path, "flow/3", "origin", None, None, None, None, None
            )
            async with httpx.AsyncClient(trust_env=False) as http:
                async with WorkerMCP(tool_caller, context) as mcp:
                    request = asyncio.create_task(
                        http.post(
                            mcp.url,
                            headers={
                                "Authorization": f"Bearer {mcp.token.get_secret_value()}",
                            },
                            json={
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "tools/call",
                                "params": {
                                    "name": "open_pr",
                                    "arguments": {"title": "Implement", "body": "Done"},
                                },
                            },
                        )
                    )
                    await connection.tool_ready.wait()
                await asyncio.gather(request, return_exceptions=True)
            closed.set()
            return InitialTaskResult(
                session_id="session-1", last_message="Codex timed out", exit_code=124
            )

    session = ExecutionState(tmp_path)
    client = WorkerClient(
        worker_id="worker-1",
        worker_token="token",
        serve_ws_url="unused",
        execution=WorkerExecution(worker_id="worker-1", executor=Executor(), state=session),
        read_history=session.read,
    )
    with pytest.raises(ConnectionError, match="script complete"):
        await asyncio.wait_for(client.run_connection(connection), 5)
    messages = sent_items(connection.sent)
    assert sum(isinstance(message, FlowToolRequest) for message in messages) == 1
    terminals = [message for message in messages if isinstance(message, ExecuteResult)]
    assert len(terminals) == 1 and isinstance(terminals[0].result, InitialTaskResult)
    assert terminals[0].result.exit_code == 124
    assert session.load().acknowledged
