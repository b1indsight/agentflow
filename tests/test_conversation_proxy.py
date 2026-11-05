import asyncio
from unittest.mock import AsyncMock

import pytest

from app.communication.gateway import Gateway
from app.communication.session import Session
from app.contracts.conversation import (
    ConversationContent,
    ConversationFailure,
    HistoryItem,
    HistoryPage,
    HistoryQuery,
)
from app.contracts.protocol import (
    PROTOCOL_VERSION,
    ConversationQuery,
    ConversationResult,
    ExecuteResult,
    ServerMessageUnion,
    WorkerMessage,
    WorkerMessageUnion,
    message_header,
    parse_server_message,
    parse_worker_message,
    result_header,
)
from app.controler.workers.conversation import (
    ConversationBroker,
    ConversationService,
)
from app.controler.workers.transport import control_ack
from app.domain.models import JobKind
from tests.test_communication_session import Socket
from tests.test_conversation_history import SESSION


@pytest.fixture
async def history_connection():
    serving = []

    async def create(peers, worker="worker", epoch="epoch", *, socket=None, before_message=None):
        if socket is None:
            socket = Socket()
        socket.delivered = asyncio.Queue()
        opened = asyncio.Event()
        session = Session[WorkerMessageUnion, ServerMessageUnion](
            socket,
            parse=parse_worker_message,
            epoch=epoch,
            make_ack=control_ack,
        )
        session.start()

        class Callbacks:
            def bind(self, routes):
                self.routes = routes
                routes.configure(
                    opened=self.opened,
                    message=self.message,
                    unmatched_reply=self.unmatched,
                    validation_error=self.error,
                )

            async def opened(self):
                opened.set()

            async def message(self, message):
                if message.connection_epoch != epoch:
                    raise ConnectionError("wrong epoch")
                if before_message is not None:
                    await before_message(message)

            async def unmatched(self, item, message):
                if not isinstance(item, ConversationResult):
                    socket.delivered.put_nowait(item)

            async def error(self, item, message):
                self.routes.reject_reply(item, "invalid response")

        serving.append(asyncio.create_task(peers.serve(worker, session, Callbacks(), replace=True)))
        await asyncio.wait_for(opened.wait(), 1)
        registration = peers.get(worker)
        assert registration is not None
        return registration, socket

    yield create
    for task in serving:
        task.cancel()
    await asyncio.gather(*serving, return_exceptions=True)


async def answer(socket, page=None, error=None):
    message = parse_server_message(await asyncio.wait_for(socket.sent.get(), 1))
    request = message.payload[0]
    assert isinstance(request, ConversationQuery)
    response = ConversationResult(
        **result_header(request.message_id),
        method="worker.history",
        page=page if page is not None or error is not None else HistoryPage(session_id=SESSION),
        error=error,
    )
    await socket.incoming.put(
        WorkerMessage(
            **message_header(message.connection_epoch), control=None, payload=[response]
        ).model_dump_json()
    )


@pytest.mark.asyncio
async def test_query_correlates_response(history_connection) -> None:
    peers = Gateway[WorkerMessageUnion, ServerMessageUnion]()
    broker = ConversationBroker(peers)
    _, socket = await history_connection(peers)
    query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    await answer(socket)
    assert (await query).session_id == SESSION
    assert not broker._inflight


@pytest.mark.parametrize("phase", ["lock", "write", "reply"])
@pytest.mark.parametrize("failure", ["disconnect", "timeout", "cancel"])
async def test_disconnect_timeout_and_cancellation_cleanup(
    history_connection, failure, phase
) -> None:
    # A query's budget includes acquiring the send lock and the actual transport write.
    # Abandoning an uncertain write seals the connection; a queued writer must never leak out.
    peers = Gateway[WorkerMessageUnion, ServerMessageUnion]()
    broker = ConversationBroker(peers)
    registration, socket = await history_connection(peers)
    blocker = None
    if phase != "reply":
        socket.release_write.clear()
    if phase == "lock":
        blocker = asyncio.create_task(registration.session.send(control_ack("a" * 32)))
        await socket.write_started.wait()
    if failure == "timeout":
        broker.timeout = 0.02
    query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    if phase == "reply":
        await socket.sent.get()
    elif phase == "write":
        await socket.write_started.wait()
    else:
        await asyncio.sleep(0)
    if failure == "disconnect":
        await peers.unregister(registration)
    elif failure == "cancel":
        query.cancel()
    code = "query_timeout" if failure == "timeout" else "worker_unavailable"
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else ConversationFailure,
        match=None if failure == "cancel" else code,
    ):
        await asyncio.wait_for(query, 1)
    assert not broker._inflight
    if phase != "reply":
        assert socket.sent.empty()
        socket.release_write.set()
        if blocker is not None:
            await asyncio.gather(blocker, return_exceptions=True)
            if failure != "disconnect":
                await socket.sent.get()  # Only the pre-existing ACK writer may finish.
        assert socket.sent.empty()
    if failure == "disconnect" or phase == "write":
        assert registration.session.closed
        _, socket = await history_connection(peers, epoch="new")
    broker.timeout = 1
    again = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    await answer(socket)
    assert (await again).session_id == SESSION


@pytest.mark.parametrize("write_complete", [False, True])
async def test_reply_then_eof_requires_a_completed_write(history_connection, write_complete):
    # EOF must drain a received reply, but a reply cannot make a blocked write successful.
    class ReplyingSocket(Socket):
        async def send_text(self, raw):
            request = parse_server_message(raw).payload[0]
            response = ConversationResult(
                **result_header(request.message_id),
                method="worker.history",
                page=HistoryPage(session_id=SESSION),
            )
            self.incoming.put_nowait(
                WorkerMessage(
                    **message_header("epoch"), control=None, payload=[response]
                ).model_dump_json()
            )
            self.incoming.put_nowait(ConnectionError("EOF"))
            if not write_complete:
                await asyncio.Event().wait()

    peers = Gateway()
    broker = ConversationBroker(peers)
    await history_connection(peers, socket=ReplyingSocket())
    if write_complete:
        assert (
            await asyncio.wait_for(broker.query("worker", HistoryQuery(session_id=SESSION)), 1)
        ).session_id == SESSION
    else:
        with pytest.raises(ConversationFailure, match="worker_unavailable"):
            await asyncio.wait_for(broker.query("worker", HistoryQuery(session_id=SESSION)), 1)
    assert not broker._inflight


async def test_explicit_close_fails_waiter_while_serial_driver_is_busy(history_connection):
    entered, release = asyncio.Event(), asyncio.Event()

    async def block(message):
        entered.set()
        await release.wait()

    peers = Gateway()
    broker = ConversationBroker(peers)
    registration, socket = await history_connection(peers, before_message=block)
    query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    try:
        await answer(socket)
        await entered.wait()
        await peers.unregister(registration)
        with pytest.raises(ConversationFailure, match="worker_unavailable"):
            await asyncio.wait_for(query, 1)
        assert not broker._inflight
    finally:
        release.set()
        query.cancel()
        await asyncio.gather(query, return_exceptions=True)


@pytest.mark.asyncio
async def test_replacement_connection_and_busy_limit(history_connection) -> None:
    peers = Gateway[WorkerMessageUnion, ServerMessageUnion]()
    broker = ConversationBroker(peers)
    old, old_socket = await history_connection(peers, epoch="old")
    first = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    await old_socket.sent.get()
    new, socket = await history_connection(peers, epoch="new")
    with pytest.raises(ConversationFailure, match="worker_unavailable"):
        await first
    queries = [
        asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
        for _ in range(4)
    ]
    try:
        # Let the requests occupy their four slots without replying yet.
        await asyncio.sleep(0)
        with pytest.raises(ConversationFailure, match="query_busy"):
            await broker.query("worker", HistoryQuery(session_id=SESSION))
        await peers.unregister(old)
        assert peers.get("worker") is new
        for _ in queries:
            await answer(socket)
        assert all(page.session_id == SESSION for page in await asyncio.gather(*queries))
    finally:
        for query in queries:
            query.cancel()
        await asyncio.gather(*queries, return_exceptions=True)


async def test_global_admission_spans_sessions_and_is_reusable(history_connection):
    peers = Gateway()
    broker = ConversationBroker(peers)
    sockets = [(await history_connection(peers, worker=str(i)))[1] for i in range(17)]
    queries = [
        asyncio.create_task(broker.query(str(i), HistoryQuery(session_id=SESSION)))
        for i in range(16)
        for _ in range(4)
    ]
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
        assert not broker._inflight
    finally:
        for query in queries:
            query.cancel()
        await asyncio.gather(*queries, return_exceptions=True)


@pytest.mark.parametrize(
    "defect", ["session", "limit", "bytes", "has_more", "item_id", "cursor", "error"]
)
async def test_worker_pages_are_validated_after_correlation(history_connection, defect):
    from app.contracts.conversation import ConversationContent, HistoryItem

    peers = Gateway()
    broker = ConversationBroker(peers)
    _, socket = await history_connection(peers)
    query = asyncio.create_task(
        broker.query(
            "worker", HistoryQuery(session_id=SESSION, limit=100 if defect == "bytes" else 1)
        )
    )
    page = HistoryPage(session_id=SESSION)
    if defect == "session":
        page.session_id = "019eac3b-a60c-7a93-a442-3718f660fc8d"
    elif defect in ("limit", "bytes", "item_id"):
        page.items = [
            HistoryItem(
                id=f"{SESSION}:0" if defect != "item_id" else "other:0",
                role="assistant",
                kind="message",
                content=ConversationContent(text="x" * 8192 if defect == "bytes" else "hi"),
            )
        ]
        if defect == "limit":
            page.items *= 2
        elif defect == "bytes":
            page.items *= 65
    elif defect == "has_more":
        page.has_more = True
    elif defect == "cursor":
        page.next_cursor = "not-a-cursor"
    await answer(
        socket,
        page=page if defect != "error" else None,
        error="history_unreadable" if defect == "error" else None,
    )
    with pytest.raises(
        ConversationFailure, match="history_unreadable" if defect == "error" else "invalid_response"
    ):
        await query
    assert not broker._inflight


@pytest.mark.asyncio
async def test_service_resolves_existing_database_metadata_and_released_binding(
    store, monkeypatch
) -> None:
    flow = store.flows.create_flow("owner/repo", 8, installation_id=42)
    broker = ConversationBroker(Gateway())
    service = ConversationService(
        broker,
        database=store.database,
    )
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


async def test_history_size_ignores_other_items_and_preserves_their_delivery(history_connection):
    # A large terminal sharing a valid history response must neither invalidate the page
    # nor disappear when the pending query consumes its own item.
    peers = Gateway()
    broker = ConversationBroker(peers)
    _, socket = await history_connection(peers)
    query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    message = parse_server_message(await socket.sent.get())
    request = message.payload[0]
    response = ConversationResult(
        **result_header(request.message_id),
        method="worker.history",
        page=HistoryPage(session_id=SESSION),
    )
    terminal = ExecuteResult(
        **result_header("1" * 32),
        method="worker.execute",
        job_id=5,
        assignment_token="token",
        error="x" * (600 * 1024),
    )
    await socket.incoming.put(
        WorkerMessage(
            **message_header("epoch"), control=None, payload=[response, terminal]
        ).model_dump_json()
    )
    assert (await asyncio.wait_for(query, 1)).session_id == SESSION
    assert await asyncio.wait_for(socket.delivered.get(), 1) == terminal


@pytest.mark.parametrize("ack_header", [False, True])
async def test_history_size_includes_shared_header(history_connection, ack_header):
    peers = Gateway()
    broker = ConversationBroker(peers)
    _, socket = await history_connection(peers)
    query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    message = parse_server_message(await socket.sent.get())
    response = ConversationResult(
        **result_header(message.payload[0].message_id),
        method="worker.history",
        page=HistoryPage(
            session_id=SESSION,
            items=[
                HistoryItem(
                    id=f"{SESSION}:{i}",
                    role="assistant",
                    kind="message",
                    content=ConversationContent(text=""),
                )
                for i in range(64)
            ],
        ),
    )
    assert response.page is not None
    delivery = WorkerMessage(**message_header("epoch"), control=None, payload=[response])
    # A receipt ACK increases the actual shared header; a reconstructed empty header
    # would accept this over-budget page even though its selected item remains valid.
    measured = delivery if ack_header else response
    remaining = 512 * 1024 - 1 - len(measured.model_dump_json().encode())
    for item in response.page.items:
        size = min(8192, remaining)
        item.content.text = "x" * size
        remaining -= size
    assert remaining == 0 and len(response.model_dump_json().encode()) < 512 * 1024
    if ack_header:
        assert len(delivery.model_dump_json().encode()) < 512 * 1024
        delivery = delivery.model_copy(update={"ack": True, "ack_id": "1" * 32})
    assert len(delivery.model_dump_json().encode()) > 512 * 1024
    await socket.incoming.put(delivery.model_dump_json())
    with pytest.raises(ConversationFailure, match="invalid_response"):
        await query


@pytest.mark.parametrize("wrong_epoch", [False, True])
async def test_same_method_wrong_reply_or_epoch_cannot_finish_query(
    history_connection, wrong_epoch
):
    peers = Gateway()
    broker = ConversationBroker(peers)
    _, socket = await history_connection(peers)
    query = asyncio.create_task(broker.query("worker", HistoryQuery(session_id=SESSION)))
    message = parse_server_message(await socket.sent.get())
    request = message.payload[0]
    right = ConversationResult(
        **result_header(request.message_id),
        method="worker.history",
        page=HistoryPage(session_id=SESSION),
    )
    wrong = ConversationResult(
        **result_header(message.message_id), method="worker.history", error="query_busy"
    )
    if wrong_epoch:
        await socket.incoming.put(
            WorkerMessage(**message_header("old"), control=None, payload=[right]).model_dump_json()
        )
        with pytest.raises(ConversationFailure, match="worker_unavailable"):
            await query
        return
    marker = ExecuteResult(
        **result_header("1" * 32),
        method="worker.execute",
        job_id=5,
        assignment_token="token",
        error="marker",
    )
    await socket.incoming.put(
        WorkerMessage(
            **message_header("epoch"), control=None, payload=[wrong, marker]
        ).model_dump_json()
    )
    assert await socket.delivered.get() == marker
    assert not query.done()
    await socket.incoming.put(
        WorkerMessage(**message_header("epoch"), control=None, payload=[right]).model_dump_json()
    )
    assert (await query).session_id == SESSION
