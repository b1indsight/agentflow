import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.contracts.conversation import ConversationFailure, HistoryPage, HistoryQuery
from app.contracts.protocol import (
    PROTOCOL_VERSION,
    ConversationQuery,
    ConversationResult,
    parse_server_message,
)
from app.control.conversation import ConversationBroker, ConversationService
from app.control.worker_connection import WorkerConnection
from app.domain.models import JobKind
from tests.test_conversation_history import SESSION


def history_connection() -> Mock:
    connection = Mock(spec=WorkerConnection, sent=asyncio.Queue())
    connection.send_text = AsyncMock(side_effect=lambda raw: connection.sent.put_nowait(parse_server_message(raw)))
    return connection


@pytest.mark.asyncio
async def test_query_correlates_response_and_rejects_other_connection() -> None:
    broker = ConversationBroker()
    connection = history_connection()
    broker.connect("worker", "epoch", connection)
    query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    request = await connection.sent.get()
    assert isinstance(request, ConversationQuery)
    response = ConversationResult(request_id=request.request_id, connection_epoch="epoch",
                                  page=HistoryPage(session_id=SESSION))
    assert broker.accept(connection, response)
    assert (await query).session_id == SESSION
    assert not broker.pending


@pytest.mark.asyncio
async def test_disconnect_and_timeout_cleanup() -> None:
    broker = ConversationBroker()
    connection = history_connection()
    broker.connect("worker", "epoch", connection)
    query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    await connection.sent.get()
    broker.disconnect(connection)
    with pytest.raises(ConversationFailure, match="worker_unavailable"):
        await query
    assert not broker.pending
    broker.connect("worker", "next", connection)
    broker.timeout = 0.001
    with pytest.raises(ConversationFailure, match="query_timeout"):
        await broker.query("worker", HistoryQuery(session_id=SESSION))
    assert not broker.pending


@pytest.mark.asyncio
async def test_cancellation_and_invalid_response() -> None:
    broker = ConversationBroker()
    connection = history_connection()
    broker.connect("worker", "epoch", connection)
    query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    await connection.sent.get()
    query.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query
    assert not broker.pending
@pytest.mark.asyncio
async def test_replacement_connection_and_busy_limit() -> None:
    broker = ConversationBroker()
    old, new = history_connection(), history_connection()
    broker.connect("worker", "old", old)
    first = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    old_request = await old.sent.get()
    broker.connect("worker", "new", new)
    with pytest.raises(ConversationFailure, match="worker_unavailable"):
        await first
    queries = [asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION))) for _ in range(4)]
    for _ in queries:
        await new.sent.get()
    assert broker.accept(old, ConversationResult(
        request_id=old_request.request_id, connection_epoch="old", page=HistoryPage(session_id=SESSION),
    ))
    assert all(not query.done() for query in queries)
    try:
        with pytest.raises(ConversationFailure, match="query_busy"):
            await broker.query("worker", HistoryQuery(session_id=SESSION))
        broker.disconnect(old)
        assert "worker" in broker.connections
    finally:
        for query in queries:
            query.cancel()
        await asyncio.gather(*queries, return_exceptions=True)


@pytest.mark.asyncio
async def test_service_resolves_existing_database_metadata_and_released_binding(store, monkeypatch) -> None:
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
    query = AsyncMock(return_value=HistoryPage(session_id=SESSION))
    monkeypatch.setattr(broker, "query", query)
    await service.read(flow.id, None, 100)
    args = query.call_args.args
    assert args[0] == "worker-1"
    assert args[1].session_id == SESSION
