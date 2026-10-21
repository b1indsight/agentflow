import asyncio
from unittest.mock import Mock

import pytest
from fastapi import WebSocketDisconnect

from app.contracts.conversation import ConversationFailure, HistoryPage, HistoryQuery
from app.contracts.protocol import (
    ConversationQuery,
    ConversationResult,
    Heartbeat,
    parse_server_message,
)
from app.controler.workers.connection import ProtocolViolation, WorkerConnection
from app.controler.workers.conversation import ConversationBroker
from tests.test_conversation_history import SESSION
from tests.worker_fakes import UnpausedMaintenance


class Socket:
    def __init__(self):
        self.headers: dict[str, str] = {}
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()
        self.reading = False

    async def accept(self):
        pass

    async def close(self, code=1000):
        await self.incoming.put(WebSocketDisconnect(code))

    async def send_text(self, raw):
        await self.sent.put(parse_server_message(raw))

    async def receive_text(self):
        assert not self.reading, "concurrent transport receiver"
        self.reading = True
        try:
            value = await self.incoming.get()
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            self.reading = False


@pytest.mark.asyncio
async def test_dispatch_bypasses_execution_buffer_and_survives_consumer_cancellation() -> None:
    broker, socket = ConversationBroker(), Socket()
    connection = WorkerConnection(socket, broker, "worker", "epoch", maintenance=UnpausedMaintenance(Mock()))
    try:
        # Idle polling cancels its own wait; it must not cancel the transport reader.
        waiting = asyncio.create_task(connection.receive_text())
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
        request = await socket.sent.get()
        assert isinstance(request, ConversationQuery)
        execution = [Heartbeat(job_id=job, assignment_token="token", connection_epoch="epoch").model_dump_json()
                     for job in (1, 2)]
        for raw in execution:
            await socket.incoming.put(raw)
        await socket.incoming.put(ConversationResult(request_id=request.request_id, connection_epoch="epoch",
                                                    page=HistoryPage(session_id=SESSION)).model_dump_json())
        assert (await asyncio.wait_for(query, 1)).session_id == SESSION
        assert [await connection.receive_text(), await connection.receive_text()] == execution
    finally:
        await connection.stop()
    assert not socket.reading
    assert not broker.connections
    with pytest.raises(WebSocketDisconnect):
        await connection.receive_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["disconnect", "overflow"])
async def test_receiver_failure_immediately_fails_query_while_control_is_busy(failure) -> None:
    broker, socket = ConversationBroker(), Socket()
    connection = WorkerConnection(socket, broker, "worker", "epoch", maintenance=UnpausedMaintenance(Mock()))
    query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    await socket.sent.get()
    raw = Heartbeat(job_id=1, assignment_token="token", connection_epoch="epoch").model_dump_json()
    try:
        if failure == "disconnect":
            await socket.incoming.put(WebSocketDisconnect())
        else:
            # A peer flooding execution messages during a slow GitHub call must not
            # grow memory without limit or stall conversation routing via backpressure.
            for _ in range(33):
                await socket.incoming.put(raw)
        with pytest.raises(ConversationFailure, match="worker_unavailable"):
            await asyncio.wait_for(query, 1)
        if failure == "overflow":
            for _ in range(32):
                assert await connection.receive_text() == raw
            with pytest.raises(ProtocolViolation, match="buffer exceeded"):
                await connection.receive_text()
        assert not broker.pending
    finally:
        await connection.stop()
