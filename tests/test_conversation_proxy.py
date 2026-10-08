import asyncio

import pytest

from app.contracts.conversation import ConversationFailure, ConversationPage
from app.contracts.protocol import ConversationQuery, ConversationResult, parse_server_message
from app.control.conversation import ConversationBroker


class Socket:
    def __init__(self):
        self.sent = asyncio.Queue()

    async def send_text(self, raw):
        await self.sent.put(parse_server_message(raw))


@pytest.mark.asyncio
async def test_query_correlates_response_and_rejects_other_connection() -> None:
    broker = ConversationBroker()
    socket, wrong = Socket(), Socket()
    broker.connect("worker", "epoch", socket)
    query = asyncio.create_task(broker.query("worker", 42, [], None, 100))
    request = await socket.sent.get()
    assert isinstance(request, ConversationQuery)
    response = ConversationResult(request_id=request.request_id, connection_epoch="epoch",
                                  page=ConversationPage(flow_id=42))
    assert broker.accept(wrong, response)
    await asyncio.sleep(0)
    assert not query.done()
    assert broker.accept(socket, response.model_copy(update={"connection_epoch": "old"}))
    await asyncio.sleep(0)
    assert not query.done()
    assert broker.accept(socket, response)
    assert (await query).flow_id == 42
    assert not broker.pending


@pytest.mark.asyncio
async def test_disconnect_and_timeout_cleanup() -> None:
    broker = ConversationBroker()
    socket = Socket()
    broker.connect("worker", "epoch", socket)
    query = asyncio.create_task(broker.query("worker", 42, [], None, 100))
    await socket.sent.get()
    broker.disconnect(socket)
    with pytest.raises(ConversationFailure, match="worker_unavailable"):
        await query
    assert not broker.pending
    broker.connect("worker", "next", socket)
    broker.timeout = 0.001
    with pytest.raises(ConversationFailure, match="query_timeout"):
        await broker.query("worker", 42, [], None, 100)
    assert not broker.pending


@pytest.mark.asyncio
async def test_cancellation_and_invalid_response() -> None:
    broker = ConversationBroker()
    socket = Socket()
    broker.connect("worker", "epoch", socket)
    query = asyncio.create_task(broker.query("worker", 42, [], None, 100))
    await socket.sent.get()
    query.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query
    assert not broker.pending
    query = asyncio.create_task(broker.query("worker", 42, [], None, 100))
    request = await socket.sent.get()
    broker.accept(socket, ConversationResult(request_id=request.request_id, connection_epoch="epoch",
                                            page=ConversationPage(flow_id=99)))
    with pytest.raises(ConversationFailure, match="invalid_response"):
        await query


@pytest.mark.asyncio
async def test_replacement_connection_and_busy_limit() -> None:
    broker = ConversationBroker()
    old, new = Socket(), Socket()
    broker.connect("worker", "old", old)
    first = asyncio.create_task(broker.query("worker", 42, [], None, 100))
    await old.sent.get()
    broker.connect("worker", "new", new)
    with pytest.raises(ConversationFailure, match="worker_unavailable"):
        await first
    queries = [asyncio.create_task(broker.query("worker", 42, [], None, 100)) for _ in range(4)]
    for _ in queries:
        await new.sent.get()
    try:
        with pytest.raises(ConversationFailure, match="query_busy"):
            await broker.query("worker", 42, [], None, 100)
        broker.disconnect(old)
        assert "worker" in broker.connections
    finally:
        for query in queries:
            query.cancel()
        await asyncio.gather(*queries, return_exceptions=True)


@pytest.mark.asyncio
async def test_service_resolves_existing_database_metadata_and_released_binding(store, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from app.contracts.protocol import PROTOCOL_VERSION
    from app.control.conversation import ConversationService
    from app.domain.models import JobKind
    from tests.test_conversation_history import SESSION

    flow = store.create_flow("owner/repo", 8, installation_id=42)
    broker = ConversationBroker()
    service = ConversationService(store, broker)
    assert (await service.read(flow.id, None, 100)).unavailable_reason == "no_worker"
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    store.worker_connected("worker-1", PROTOCOL_VERSION, "epoch")
    store.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.assign_job("worker-1", "assignment", "epoch") is not None
    assert (await service.read(flow.id, None, 100)).unavailable_reason == "no_session"
    attempt.codex_session_id = SESSION
    store.update_attempt(attempt)
    assert store.release_flow_execution(flow.id)
    query = AsyncMock(return_value=ConversationPage(flow_id=flow.id))
    monkeypatch.setattr(broker, "query", query)
    await service.read(flow.id, None, 100)
    args = query.call_args.args
    assert args[0] == "worker-1"
    assert args[2][0].session_id == SESSION
