import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.contracts.conversation import HistoryPage
from app.contracts.protocol import (
    ConversationQuery,
    ConversationResult,
    Registered,
    parse_worker_message,
)
from app.worker.state.session import WorkerSession
from app.worker.transport.client import WorkerClient
from tests.test_conversation_history import SESSION


class Connection:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def recv(self):
        return await self.incoming.get()

    async def send(self, value):
        await self.outgoing.put(value)


@pytest.mark.asyncio
async def test_query_does_not_block_execution_messages(tmp_path) -> None:
    connection = Connection()
    started, release = threading.Event(), threading.Event()

    class Session(WorkerSession):
        def read(self, query):
            started.set()
            assert release.wait(timeout=5)
            return HistoryPage(session_id=query.session_id)

    client = WorkerClient(worker_id="worker-1", worker_token="token", serve_ws_url="unused",
                          executor=SimpleNamespace(), session=Session(tmp_path))
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
    client = WorkerClient(worker_id="worker", worker_token="token", serve_ws_url="unused",
                          executor=SimpleNamespace(), session=WorkerSession(tmp_path))
    request = ConversationQuery(request_id="bad-cursor", connection_epoch="epoch",
                                session_id=SESSION, cursor="invalid-base64")
    await client._answer_history(connection, request)
    result = parse_worker_message(await connection.outgoing.get())
    assert isinstance(result, ConversationResult)
    assert result.request_id == "bad-cursor" and result.error == "cursor_invalid"
