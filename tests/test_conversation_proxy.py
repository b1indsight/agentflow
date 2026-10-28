import asyncio
from unittest.mock import AsyncMock

import pytest

from app.communication.session import Session
from app.contracts.conversation import ConversationFailure, HistoryPage, HistoryQuery
from app.contracts.protocol import (
    PROTOCOL_VERSION,
    ConversationQuery,
    ConversationResult,
    ServerMessage,
    WorkerMessage,
    parse_server_message,
    parse_worker_message,
)
from app.controler.workers.conversation import (
    ConversationBroker,
    ConversationService,
    conversation_result_key,
)
from app.domain.models import JobKind
from tests.test_communication_session import Socket
from tests.test_conversation_history import SESSION


@pytest.fixture
async def history_connection():
    sessions = []

    def create(broker, worker="worker", epoch="epoch"):
        socket = Socket()
        session = Session[WorkerMessage, ServerMessage](socket, parse=parse_worker_message,
                    reply_key=conversation_result_key, epoch=epoch)
        session.start()
        broker.attach(worker, session)
        sessions.append(session)
        return session, socket

    yield create
    await asyncio.gather(*(session.close() for session in sessions))


async def answer(socket, page=None, error=None):
    request = parse_server_message(await asyncio.wait_for(socket.sent.get(), 1))
    assert isinstance(request, ConversationQuery)
    await socket.incoming.put(ConversationResult(
        request_id=request.request_id, connection_epoch=request.connection_epoch,
        page=page if page is not None or error is not None else HistoryPage(session_id=SESSION),
        error=error,
    ).model_dump_json())


@pytest.mark.asyncio
async def test_query_correlates_response(history_connection) -> None:
    broker = ConversationBroker()
    _, socket = history_connection(broker)
    query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    await answer(socket)
    assert (await query).session_id == SESSION
    assert broker._inflight == 0


@pytest.mark.parametrize("failure", ["disconnect", "timeout", "cancel"])
async def test_disconnect_timeout_and_cancellation_cleanup(history_connection, failure) -> None:
    broker = ConversationBroker()
    connection, socket = history_connection(broker)
    if failure == "timeout":
        broker.timeout = 0.01
    query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    await socket.sent.get()
    if failure == "disconnect":
        await connection.close()
    elif failure == "cancel":
        query.cancel()
    code = "query_timeout" if failure == "timeout" else "worker_unavailable"
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else ConversationFailure,
                       match=None if failure == "cancel" else code):
        await query
    assert broker._inflight == 0
    if failure == "disconnect":
        _, socket = history_connection(broker, epoch="new")
    broker.timeout = 1
    again = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    await answer(socket)
    assert (await again).session_id == SESSION


@pytest.mark.asyncio
async def test_replacement_connection_and_busy_limit(history_connection) -> None:
    broker = ConversationBroker()
    old, old_socket = history_connection(broker, epoch="old")
    first = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    await old_socket.sent.get()
    await old.close()
    new, socket = history_connection(broker, epoch="new")
    with pytest.raises(ConversationFailure, match="worker_unavailable"):
        await first
    queries = [asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION))) for _ in range(4)]
    try:
        # Let the requests occupy their four slots without replying yet.
        await asyncio.sleep(0)
        with pytest.raises(ConversationFailure, match="query_busy"):
            await broker.query("worker", HistoryQuery(session_id=SESSION))
        broker.detach("worker", old)
        assert broker.connections["worker"] is new
        for _ in queries:
            await answer(socket)
        assert all(page.session_id == SESSION for page in await asyncio.gather(*queries))
    finally:
        for query in queries:
            query.cancel()
        await asyncio.gather(*queries, return_exceptions=True)


async def test_global_admission_spans_sessions_and_is_reusable(history_connection):
    broker = ConversationBroker()
    sockets = [history_connection(broker, worker=str(i))[1] for i in range(17)]
    queries = [asyncio.create_task(broker.query(str(i), HistoryQuery(session_id=SESSION)))
               for i in range(16) for _ in range(4)]
    try:
        await asyncio.sleep(0)
        with pytest.raises(ConversationFailure, match="query_busy"):
            await broker.query("16", HistoryQuery(session_id=SESSION))
        assert sockets[16].sent.empty()
        for socket in sockets[:16]:
            for _ in range(4):
                await answer(socket)
        await asyncio.gather(*queries)
        last = asyncio.create_task(broker.query("16", HistoryQuery(session_id=SESSION)))
        await answer(sockets[16])
        assert (await last).session_id == SESSION
        assert broker._inflight == 0
    finally:
        for query in queries:
            query.cancel()
        await asyncio.gather(*queries, return_exceptions=True)


@pytest.mark.parametrize("defect", ["session", "limit", "bytes", "has_more", "item_id", "cursor", "error"])
async def test_worker_pages_are_validated_after_correlation(history_connection, defect):
    from app.contracts.conversation import ConversationContent, HistoryItem

    broker = ConversationBroker()
    _, socket = history_connection(broker)
    query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION, limit=100 if defect == "bytes" else 1)))
    page = HistoryPage(session_id=SESSION)
    if defect == "session":
        page.session_id = "019eac3b-a60c-7a93-a442-3718f660fc8d"
    elif defect in ("limit", "bytes", "item_id"):
        page.items = [HistoryItem(id=f"{SESSION}:0" if defect != "item_id" else "other:0", role="assistant",
                                 kind="message", content=ConversationContent(text="x" * 8192 if defect == "bytes" else "hi"))]
        if defect == "limit":
            page.items *= 2
        elif defect == "bytes":
            page.items *= 65
    elif defect == "has_more":
        page.has_more = True
    elif defect == "cursor":
        page.next_cursor = "not-a-cursor"
    await answer(socket, page=page if defect != "error" else None,
                 error="history_unreadable" if defect == "error" else None)
    with pytest.raises(ConversationFailure, match="history_unreadable" if defect == "error" else "invalid_response"):
        await query
    assert broker._inflight == 0


@pytest.mark.asyncio
async def test_service_resolves_existing_database_metadata_and_released_binding(store, monkeypatch) -> None:
    flow = store.flows.create_flow("owner/repo", 8, installation_id=42)
    broker = ConversationBroker()
    service = ConversationService(broker, database=store.database, )
    assert (await service.read(flow.id, None, 100)).unavailable_reason == "no_worker"
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    store.workers.worker_connected("worker-1", PROTOCOL_VERSION, "epoch")
    store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("worker-1", "assignment", "epoch") is not None
    assert (await service.read(flow.id, None, 100)).unavailable_reason == "no_session"
    attempt.codex_session_id = SESSION
    store.flows.update_attempt(attempt)
    assert store.workers.release_flow_execution(flow.id)
    query = AsyncMock(return_value=HistoryPage(session_id=SESSION))
    monkeypatch.setattr(broker, "query", query)
    await service.read(flow.id, None, 100)
    args = query.call_args.args
    assert args[0] == "worker-1"
    assert args[1].session_id == SESSION
