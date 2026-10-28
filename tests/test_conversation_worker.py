import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.contracts.conversation import HistoryPage
from app.contracts.protocol import (
    ConversationQuery,
    ConversationResult,
    ExecutionStopped,
    InitialTaskResult,
    RecoveryRejected,
    Registered,
    ResultAccepted,
    StopAccepted,
    StopExecution,
    parse_worker_message,
)
from app.domain.models import JobState
from app.worker.execution.lifecycle import WorkerExecution, WorkerProtocolError
from app.worker.state.execution import ExecutionState
from app.worker.transport.client import WorkerClient
from tests.test_conversation_history import SESSION
from tests.test_worker_session import _assignment


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


@pytest.mark.asyncio
async def test_query_does_not_block_execution_messages(tmp_path) -> None:
    connection = Connection()
    started, release = threading.Event(), threading.Event()

    class State(ExecutionState):
        def read(self, query):
            started.set()
            assert release.wait(timeout=5)
            return HistoryPage(session_id=query.session_id)

    state = State(tmp_path)
    client = WorkerClient(worker_id="worker-1", worker_token="token", serve_ws_url="unused",
                          execution=WorkerExecution(worker_id="worker-1", executor=SimpleNamespace(), state=state),
                          read_history=state.read)
    query = ConversationQuery(request_id="q1", connection_epoch="e1", session_id=SESSION)
    await connection.incoming.put(query.model_dump_json())
    registered = Registered(connection_epoch="e1", heartbeat_interval=10).model_dump_json()
    await connection.incoming.put(registered)
    running = asyncio.create_task(client.run_connection(connection))
    try:
        await connection.outgoing.get()  # Registration request.
        assert await asyncio.to_thread(started.wait, 5)
        assert client.connected
        release.set()
        result = parse_worker_message(await asyncio.wait_for(connection.outgoing.get(), 5))
        assert isinstance(result, ConversationResult)
        assert result.request_id == "q1"
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
    client = WorkerClient(worker_id="worker", worker_token="token", serve_ws_url="unused",
                          execution=WorkerExecution(worker_id="worker", executor=SimpleNamespace(), state=state),
                          read_history=state.read)
    request = ConversationQuery(request_id="bad-cursor", connection_epoch="epoch",
                                session_id=SESSION, cursor="invalid-base64")
    running = asyncio.create_task(client.run_connection(connection))
    try:
        await connection.outgoing.get()  # Register must be the first outbound frame.
        await connection.incoming.put(request.model_dump_json())
        result = parse_worker_message(await asyncio.wait_for(connection.outgoing.get(), 1))
        assert isinstance(result, ConversationResult)
        assert result.request_id == "bad-cursor" and result.error == "cursor_invalid"
        assert not client.connected  # A query cannot confirm registration or the epoch.
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
    client = WorkerClient(worker_id="worker", worker_token="test", serve_ws_url="unused",
                          execution=owner, read_history=lambda query: HistoryPage(session_id=query.session_id))
    request = StopExecution(job_id=work.job_id, flow_id=work.flow_id, assignment_token=work.assignment_token,
                            request_id="stop", connection_epoch="new")
    running = asyncio.create_task(client.run_connection(connection))
    try:
        await started.wait()
        await connection.outgoing.get()
        first = request if operation == "stop" else RecoveryRejected(job_id=work.job_id, reason="expired")
        await connection.incoming.put(first.model_dump_json())
        await asyncio.wait_for(cleaning.wait(), 1)
        await connection.incoming.put(ConversationQuery(request_id="history", connection_epoch="query-only",
                                                        session_id=SESSION).model_dump_json())
        result = parse_worker_message(await asyncio.wait_for(connection.outgoing.get(), 1))
        assert isinstance(result, ConversationResult) and result.page is not None
        assert result.connection_epoch == "query-only"
        assert not client.connected and not release.is_set()
        release.set()
        if operation == "stop":
            receipt = parse_worker_message(await asyncio.wait_for(connection.outgoing.get(), 1))
            assert isinstance(receipt, ExecutionStopped) and not state.load().acknowledged
            await connection.incoming.put(StopAccepted(**request.model_dump(exclude={"type"})).model_dump_json())
        await connection.incoming.put(Registered(connection_epoch="new", heartbeat_interval=60).model_dump_json())
        async with asyncio.timeout(1):
            while not client.connected:
                await asyncio.sleep(0)
        if operation == "stop":
            assert state.load().acknowledged
        else:
            assert state.assignment is None
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await owner.shutdown()


@pytest.mark.parametrize("conflict", [False, True])
async def test_recovery_result_ack_before_registered_confirms_epoch(tmp_path, conflict):
    # An already-applied result is acknowledged directly, with no ReplayResult exchange.
    work = _assignment()
    state = ExecutionState(tmp_path)
    state.record_received(work)
    state.record_started()
    state.complete(InitialTaskResult(session_id=SESSION, last_message="done", exit_code=0))
    owner = WorkerExecution(worker_id="worker", executor=SimpleNamespace(), state=state)
    client = WorkerClient(worker_id="worker", worker_token="test", serve_ws_url="unused",
                          execution=owner, read_history=state.read)
    connection = Connection()
    await connection.incoming.put(ResultAccepted(job_id=work.job_id, assignment_token=work.assignment_token,
        state=JobState.DONE, connection_epoch="new").model_dump_json())
    await connection.incoming.put(Registered(connection_epoch="conflict" if conflict else "new",
                                             heartbeat_interval=60).model_dump_json())
    running = asyncio.create_task(client.run_connection(connection))
    try:
        if conflict:
            with pytest.raises(WorkerProtocolError, match="epoch cannot change"):
                await asyncio.wait_for(running, 1)
        else:
            async with asyncio.timeout(1):
                while not client.connected:
                    await asyncio.sleep(0)
        assert state.load().acknowledged and state.assignment is None
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
