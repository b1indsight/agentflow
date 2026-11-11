import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.contracts.conversation import HistoryPage
from app.contracts.protocol import (
    AcceptResultRequest,
    AcceptResultResponse,
    AcceptStop,
    ConversationQuery,
    ConversationResult,
    ExecuteResult,
    HistoryArguments,
    InitialTaskResult,
    RecoveryDecision,
    RecoveryRejected,
    RecoveryRejectedArguments,
    Register,
    ResultAcceptance,
    ServerMessage,
    StopAccepted,
    StopExecution,
    StopFence,
    StopResult,
    WorkerMessageUnion,
    accept_result_id,
    accept_stop_id,
    call_header,
    message_header,
    parse_worker_message,
    result_header,
    stop_call_id,
    stop_receipt_id,
    terminal_id,
)
from app.worker.execution.lifecycle import WorkerExecution, WorkerProtocolError
from app.worker.state.execution import ExecutionState, ExecutionStateError
from tests.test_conversation_history import SESSION
from tests.test_worker_client import registered_message
from tests.test_worker_session import _assignment
from tests.wire import registered, server_message
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

    async def receive_message(self) -> WorkerMessageUnion:
        """Ignore transport receipts, never ACK them or drop a business-bearing message."""
        async with asyncio.timeout(5):
            while True:
                message = parse_worker_message(await self.outgoing.get())
                if message.control is not None or message.payload:
                    return message


@pytest.mark.asyncio
@pytest.mark.parametrize("registered_first", [False, True])
async def test_history_uses_fifo_with_separate_registration(tmp_path, registered_first) -> None:
    # A standalone read holds the driver; control in its own message waits behind it.
    # Protocol 20 keeps registration and history in separate messages.
    connection = Connection()
    started, release = threading.Event(), threading.Event()

    class State(ExecutionState):
        def read(self, query):
            started.set()
            assert release.wait(timeout=5)
            return HistoryPage(session_id=query.session_id)

    state = State(tmp_path, 3)
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
    if registered_first:
        await connection.incoming.put(registered(epoch="e1").model_dump_json())
        await connection.incoming.put(server_message(query, epoch="e1").model_dump_json())
    else:
        await connection.incoming.put(server_message(query, epoch="e1").model_dump_json())
        await connection.incoming.put(registered(epoch="e1").model_dump_json())
    running = asyncio.create_task(run_worker(client, connection))
    try:
        await connection.outgoing.get()  # Registration request.
        assert await asyncio.to_thread(started.wait, 5)
        assert client._connected is registered_first
        release.set()
        message = await connection.receive_message()
        assert len(message.payload) == 1 and message.connection_epoch == "e1"
        result = message.payload[0]
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
    state = ExecutionState(tmp_path, 3)
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
        await connection.outgoing.get()  # Register must be the first outbound message.
        await connection.incoming.put(server_message(request, epoch="epoch").model_dump_json())
        message = await connection.receive_message()
        assert message.connection_epoch == "epoch" and len(message.payload) == 1
        result = message.payload[0]
        assert isinstance(result, ConversationResult)
        assert result.reply_to == request.message_id and result.error == "cursor_invalid"
        assert not client._connected  # A query cannot confirm registration or the epoch.
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("operation", ["stop", "reject"])
async def test_handshake_stop_allows_history_but_rejection_retains_started_evidence(tmp_path, operation):
    # Stops join asynchronously, so another workspace's history can proceed during cleanup.
    # A rejection is not a stop receipt: started evidence must remain reconcilable.
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
    state = ExecutionState(tmp_path, 3)
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
        register = await connection.receive_message()
        assert isinstance(register, Register)
        assert len(register.recoveries) == 1 and register.inventory_complete
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
        await connection.incoming.put(server_message(first, epoch="new").model_dump_json())
        if operation == "reject":
            with pytest.raises(ExecutionStateError, match="retain execution evidence"):
                await asyncio.wait_for(running, 1)
            assert owner.lease() == work and not cleaning.is_set()
            assert state.load().state == "started" and not state.load().acknowledged
            assert not client._connected
            return
        await asyncio.wait_for(cleaning.wait(), 1)
        query = ConversationQuery(
            **call_header(), method="worker.history", arguments=HistoryArguments(session_id=SESSION)
        )
        await connection.incoming.put(server_message(query, epoch="new").model_dump_json())
        message = await connection.receive_message()
        assert message.connection_epoch == "new" and len(message.payload) == 1
        result = message.payload[0]
        assert isinstance(result, ConversationResult) and result.page is not None
        assert result.reply_to == query.message_id
        assert not release.is_set()
        assert not client._connected
        release.set()
        stopped = await connection.receive_message()
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
        await connection.incoming.put(server_message(accepted, epoch="new").model_dump_json())
        assert not client._connected
        await connection.incoming.put(registered(epoch="new").model_dump_json())
        async with asyncio.timeout(1):
            while not client._connected:
                await asyncio.sleep(0)
        journal = state.load()
        assert journal is not None and journal.acknowledged
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await owner.shutdown()


@pytest.mark.parametrize("conflict", [False, True])
async def test_recovery_decision_precedes_terminal_replay_and_fences_ack_epoch(tmp_path, conflict):
    # Registration carries only evidence. Neither receipt ACK nor history permits replay.
    work = _assignment()
    state = ExecutionState(tmp_path, 3)
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
        register = await connection.receive_message()
        assert isinstance(register, Register) and register.payload == []
        assert register.inventory_complete and len(register.recoveries) == 1
        terminal = state.replay()
        receipt_header = message_header(None)
        receipt_header["ack"] = True
        receipt_header["ack_id"] = register.message_id
        await connection.incoming.put(
            ServerMessage(**receipt_header, control=None, payload=[]).model_dump_json()
        )
        barrier = ConversationQuery(
            **call_header(), method="worker.history", arguments=HistoryArguments(session_id=SESSION)
        )
        await connection.incoming.put(server_message(barrier, epoch="query-only").model_dump_json())
        response = await connection.receive_message()
        assert len(response.payload) == 1 and isinstance(response.payload[0], ConversationResult)
        assert response.payload[0].reply_to == barrier.message_id
        journal = state.load()
        assert journal is not None and not journal.acknowledged and not client._connected
        await connection.incoming.put(registered_message("new", recovery=RecoveryDecision(
            job_id=work.job_id, assignment_token=work.assignment_token, action="replay",
        )).model_dump_json())
        replay = await connection.receive_message()
        result, acceptance = replay.payload
        assert isinstance(result, ExecuteResult) and result.terminal() == terminal
        assert isinstance(acceptance, AcceptResultRequest)
        assert acceptance.arguments.result_id == terminal_id(terminal)
        assert acceptance.message_id == accept_result_id(terminal_id(terminal))
        accepted = AcceptResultResponse(
            **result_header(acceptance.message_id),
            method="server.accept_result",
            result=ResultAcceptance(
                job_id=work.job_id, assignment_token=work.assignment_token, state="done"
            ),
        )
        await connection.incoming.put(
            server_message(accepted, epoch="conflict" if conflict else "new").model_dump_json()
        )
        if conflict:
            with pytest.raises(WorkerProtocolError, match="epoch"):
                await asyncio.wait_for(running, 1)
        else:
            async with asyncio.timeout(1):
                while not state.load().acknowledged:
                    await asyncio.sleep(0)
        journal = state.load()
        assert journal is not None and journal.acknowledged is not conflict
        assert (state.assignment is None) is not conflict
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
