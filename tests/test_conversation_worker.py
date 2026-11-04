import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.contracts.conversation import HistoryPage
from app.contracts.protocol import (
    AcceptedResult,
    AcceptResult,
    AcceptStop,
    ConversationQuery,
    ConversationResult,
    HistoryArguments,
    InitialTaskResult,
    RecoveryRejected,
    RecoveryRejectedArguments,
    Register,
    ResultAccepted,
    ServerFrame,
    StopAccepted,
    StopExecution,
    StopFence,
    StopResult,
    WorkerMessage,
    accept_result_id,
    accept_stop_id,
    call_header,
    frame_header,
    parse_worker_message,
    result_header,
    stop_call_id,
    stop_receipt_id,
    terminal_id,
)
from app.worker.execution.lifecycle import WorkerExecution, WorkerProtocolError
from app.worker.state.execution import ExecutionState
from tests.test_conversation_history import SESSION
from tests.test_worker_session import _assignment
from tests.wire import registered, server_frame
from tests.worker_fakes import run_worker, worker_runtime


class Connection:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def recv(self):
        return await self.incoming.get()

    async def send(self, value):
        await self.outgoing.put(value)

    async def close(self, code=1000):
        pass

    async def receive_frame(self) -> WorkerMessage:
        """Ignore transport receipts, never ACK them or drop a business-bearing frame."""
        async with asyncio.timeout(5):
            while True:
                frame = parse_worker_message(await self.outgoing.get())
                if frame.control is not None or frame.payload:
                    return frame


@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [False, True])
async def test_query_does_not_block_execution_messages(tmp_path, batched) -> None:
    connection = Connection()
    started, release = threading.Event(), threading.Event()

    class State(ExecutionState):
        def read(self, query):
            started.set()
            assert release.wait(timeout=5)
            return HistoryPage(session_id=query.session_id)

    state = State(tmp_path)
    client = worker_runtime(
        worker_id="worker-1",
        worker_token="token",
        serve_ws_url="unused",
        execution=WorkerExecution(worker_id="worker-1", executor=SimpleNamespace(), state=state),
        read_history=state.read,
    )
    query = ConversationQuery(
        **call_header(), method="worker.history", arguments=HistoryArguments(session_id=SESSION)
    )
    if batched:
        await connection.incoming.put(registered(query, epoch="e1").model_dump_json())
    else:
        await connection.incoming.put(server_frame(query, epoch="e1").model_dump_json())
        await connection.incoming.put(registered(epoch="e1").model_dump_json())
    running = asyncio.create_task(run_worker(client, connection))
    try:
        await connection.outgoing.get()  # Registration request.
        assert await asyncio.to_thread(started.wait, 5)
        assert client._connected
        release.set()
        frame = await connection.receive_frame()
        assert len(frame.payload) == 1 and frame.connection_epoch == "e1"
        result = frame.payload[0]
        assert isinstance(result, ConversationResult)
        assert result.reply_to == query.message_id
        assert result.page is not None
        assert result.page.session_id == SESSION
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_bad_cursor_returns_correlated_query_error(tmp_path):
    connection = Connection()
    state = ExecutionState(tmp_path)
    client = worker_runtime(
        worker_id="worker",
        worker_token="token",
        serve_ws_url="unused",
        execution=WorkerExecution(worker_id="worker", executor=SimpleNamespace(), state=state),
        read_history=state.read,
    )
    request = ConversationQuery(
        **call_header(),
        method="worker.history",
        arguments=HistoryArguments(session_id=SESSION, cursor="invalid-base64"),
    )
    running = asyncio.create_task(run_worker(client, connection))
    try:
        await connection.outgoing.get()  # Register must be the first outbound frame.
        await connection.incoming.put(server_frame(request, epoch="epoch").model_dump_json())
        frame = await connection.receive_frame()
        assert frame.connection_epoch == "epoch" and len(frame.payload) == 1
        result = frame.payload[0]
        assert isinstance(result, ConversationResult)
        assert result.reply_to == request.message_id and result.error == "cursor_invalid"
        assert not client._connected  # A query cannot confirm registration or the epoch.
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("operation", ["stop", "reject"])
async def test_query_completes_during_handshake_cleanup_before_registered(tmp_path, operation):
    # Recovery/stop may spend a long time joining a process before Registered is legal.
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Executor:
        async def execute(self, task, secret=None, *, tool_caller):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        async def reclaim_workspace(self):
            pass

    work = _assignment()
    state = ExecutionState(tmp_path)
    owner = WorkerExecution(worker_id="worker", executor=Executor(), state=state)
    owner.receive(work)
    owner.start()
    connection = Connection()
    client = worker_runtime(
        worker_id="worker",
        worker_token="test",
        serve_ws_url="unused",
        execution=owner,
        read_history=lambda query: HistoryPage(session_id=query.session_id),
    )
    fence = StopFence(
        job_id=work.job_id,
        flow_id=work.flow_id,
        assignment_token=work.assignment_token,
        request_id="stop",
    )
    request = StopExecution(
        **call_header(stop_call_id(fence)), method="worker.stop", arguments=fence
    )
    running = asyncio.create_task(run_worker(client, connection))
    try:
        await started.wait()
        register = await connection.receive_frame()
        assert isinstance(register, Register)
        first = (
            request
            if operation == "stop"
            else RecoveryRejected(
                **call_header(),
                method="worker.reject_recovery",
                arguments=RecoveryRejectedArguments(
                    job_id=work.job_id, reason="expired", register_message_id=register.message_id
                ),
            )
        )
        await connection.incoming.put(server_frame(first, epoch="new").model_dump_json())
        await asyncio.wait_for(cleaning.wait(), 1)
        query = ConversationQuery(
            **call_header(), method="worker.history", arguments=HistoryArguments(session_id=SESSION)
        )
        await connection.incoming.put(server_frame(query, epoch="query-only").model_dump_json())
        frame = await connection.receive_frame()
        assert frame.connection_epoch == "query-only" and len(frame.payload) == 1
        result = frame.payload[0]
        assert isinstance(result, ConversationResult) and result.page is not None
        assert result.reply_to == query.message_id
        assert not client._connected and not release.is_set()
        release.set()
        if operation == "stop":
            stopped = await connection.receive_frame()
            assert stopped.connection_epoch == "new" and len(stopped.payload) == 2
            receipt, acceptance = stopped.payload
            assert isinstance(receipt, StopResult) and isinstance(acceptance, AcceptStop)
            assert receipt.reply_to == request.message_id
            assert receipt.message_id == stop_receipt_id(receipt.result)
            assert acceptance.message_id == accept_stop_id(receipt.message_id)
            assert acceptance.arguments.receipt_id == receipt.message_id
            journal = state.load()
            assert journal is not None and not journal.acknowledged
            accepted = StopAccepted(
                **result_header(acceptance.message_id), method="server.accept_stop", result=fence
            )
            await connection.incoming.put(server_frame(accepted, epoch="new").model_dump_json())
        await connection.incoming.put(registered(epoch="new").model_dump_json())
        async with asyncio.timeout(1):
            while not client._connected:
                await asyncio.sleep(0)
        if operation == "stop":
            journal = state.load()
            assert journal is not None and journal.acknowledged
        else:
            assert state.assignment is None
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await owner.shutdown()


@pytest.mark.parametrize("conflict", [False, True])
async def test_recovery_result_ack_before_registered_confirms_epoch(tmp_path, conflict):
    # An already-applied result answers Register+[A], without asking for terminal replay.
    work = _assignment()
    state = ExecutionState(tmp_path)
    state.record_received(work)
    state.record_started()
    state.complete(InitialTaskResult(session_id=SESSION, last_message="done", exit_code=0))
    owner = WorkerExecution(worker_id="worker", executor=SimpleNamespace(), state=state)
    client = worker_runtime(
        worker_id="worker",
        worker_token="test",
        serve_ws_url="unused",
        execution=owner,
        read_history=state.read,
    )
    connection = Connection()
    running = asyncio.create_task(run_worker(client, connection))
    try:
        register = await connection.receive_frame()
        assert isinstance(register, Register) and len(register.payload) == 1
        acceptance = register.payload[0]
        assert isinstance(acceptance, AcceptResult)
        terminal = state.replay()
        assert acceptance.arguments.result_id == terminal_id(terminal)
        assert acceptance.message_id == accept_result_id(terminal_id(terminal))
        receipt_header = frame_header(None)
        receipt_header["ack"] = True
        receipt_header["ack_id"] = register.message_id
        await connection.incoming.put(
            ServerFrame(**receipt_header, control=None, payload=[]).model_dump_json()
        )
        barrier = ConversationQuery(
            **call_header(), method="worker.history", arguments=HistoryArguments(session_id=SESSION)
        )
        await connection.incoming.put(server_frame(barrier, epoch="query-only").model_dump_json())
        response = await connection.receive_frame()
        assert len(response.payload) == 1 and isinstance(response.payload[0], ConversationResult)
        assert response.payload[0].reply_to == barrier.message_id
        journal = state.load()
        assert journal is not None and not journal.acknowledged and not client._connected
        accepted = ResultAccepted(
            **result_header(acceptance.message_id),
            method="server.accept_result",
            result=AcceptedResult(
                job_id=work.job_id, assignment_token=work.assignment_token, state="done"
            ),
        )
        await connection.incoming.put(server_frame(accepted, epoch="new").model_dump_json())
        await connection.incoming.put(
            registered(epoch="conflict" if conflict else "new").model_dump_json()
        )
        if conflict:
            with pytest.raises(WorkerProtocolError, match="epoch"):
                await asyncio.wait_for(running, 1)
        else:
            async with asyncio.timeout(1):
                while not client._connected:
                    await asyncio.sleep(0)
        journal = state.load()
        assert journal is not None and journal.acknowledged and state.assignment is None
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
