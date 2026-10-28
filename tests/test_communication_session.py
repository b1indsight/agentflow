import asyncio
from typing import Literal

import pytest

from app.communication.session import (
    Responder,
    Session,
    SessionBusy,
    SessionClosed,
    SessionOverflow,
)
from app.contracts.protocol import Message


class Packet(Message):
    """Test-only symmetric protocol; the production v15 direction lists stay unchanged."""

    kind: Literal["query", "reply", "event"] = "event"
    request_id: str = "q"
    epoch: str = "epoch"
    value: str = ""


class Socket:
    def __init__(self):
        self.incoming: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.sent: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.reading = False
        self.writing = False
        self.write_started = asyncio.Event()
        self.release_write = asyncio.Event()
        self.release_write.set()

    async def close(self, code=1000):
        pass

    async def send_text(self, raw):
        assert not self.writing, "concurrent transport writer"
        self.writing = True
        self.write_started.set()
        try:
            await self.release_write.wait()
            await self.sent.put(raw)
        finally:
            self.writing = False

    async def receive_text(self):
        assert not self.reading, "concurrent transport reader"
        self.reading = True
        try:
            value = await self.incoming.get()
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            self.reading = False


def reply_key(message: Packet):
    return (message.request_id, message.epoch) if message.kind == "reply" else None


def make_session(socket, responder=None):
    session = Session[Packet, Packet](
        socket,
        parse=Packet.model_validate_json,
        reply_key=reply_key,
        responder=responder,
        epoch="epoch",
    )
    session.start()
    return session


async def respond(query: Packet) -> Packet:
    return query.model_copy(update={"kind": "reply"})


async def test_duplex_queries_bypass_fifo_and_consumer_cancellation():
    left_socket, right_socket = Socket(), Socket()
    left_socket.sent = right_socket.incoming
    right_socket.sent = left_socket.incoming
    responder = Responder(
        matches=lambda m: m.kind == "query",
        handle=respond,
        busy=lambda m: m.model_copy(update={"kind": "reply", "value": "busy"}),
    )
    left, right = make_session(left_socket, responder), make_session(right_socket, responder)
    try:
        waiting = asyncio.create_task(left.receive())
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        # No business consumer runs until both replies have bypassed queued commands/events.
        for session in (left, right):
            await session.send(Packet(value="one"))
            await session.send(Packet(value="two"))
        replies = await asyncio.gather(
            *(
                s.request(Packet(kind="query", value=str(i)), key=("q", "epoch"), timeout=1)
                for i, s in enumerate((left, right))
            )
        )
        assert [reply.value for reply in replies] == ["0", "1"]
        for session in (left, right):
            assert [(await session.receive()).value, (await session.receive()).value] == [
                "one",
                "two",
            ]
    finally:
        await asyncio.gather(left.close(), right.close())
    assert not left_socket.reading and not right_socket.reading
    with pytest.raises(SessionClosed):
        await left.receive()


@pytest.mark.parametrize("failure", ["disconnect", "count", "bytes"])
async def test_read_failure_fails_pending_immediately_but_drains_fifo(failure):
    socket = Socket()
    session = make_session(socket)
    query = asyncio.create_task(
        session.request(Packet(kind="query"), key=("q", "epoch"), timeout=1)
    )
    await socket.sent.get()
    raw = Packet(value="中" * 3_000_000 if failure == "bytes" else "event").model_dump_json()
    count = 1 if failure == "disconnect" else 2 if failure == "bytes" else 33
    try:
        for _ in range(count):
            await socket.incoming.put(raw)
        if failure == "disconnect":
            await socket.incoming.put(ConnectionError("lost"))
        with pytest.raises(SessionClosed):
            await asyncio.wait_for(query, 2)
        buffered = 32 if failure == "count" else 1
        for _ in range(buffered):
            assert await session.receive() == Packet.model_validate_json(raw)
        with pytest.raises(ConnectionError if failure == "disconnect" else SessionOverflow):
            await session.receive()
    finally:
        await session.close()


async def test_serialized_send_checks_guard_at_actual_write_and_close_joins_without_canceling_caller():
    socket = Socket()
    socket.release_write.clear()
    session = make_session(socket)
    first = asyncio.create_task(session.send(Packet(value="first")))
    await socket.write_started.wait()
    current = True
    second = asyncio.create_task(
        session.send(Packet(value="cancelled"), is_current=lambda: current)
    )
    # Give a mistakenly concurrent writer its transport turn while the first is blocked.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    current = False
    socket.release_write.set()
    assert await first
    assert not await second
    assert Packet.model_validate_json(await socket.sent.get()).value == "first"
    assert socket.sent.empty()

    socket.release_write.clear()
    socket.write_started.clear()
    writing = asyncio.create_task(session.send(Packet()))
    await socket.write_started.wait()
    waiting = asyncio.create_task(session.send(Packet()))
    await socket.incoming.put(Packet(value="discard on close").model_dump_json())
    await session.close()
    for task in (writing, waiting):
        with pytest.raises(SessionClosed):
            await task
        assert not task.cancelled()
    assert not socket.writing and not socket.reading
    with pytest.raises(SessionClosed):
        await session.receive()


@pytest.mark.parametrize("phase", ["lock", "write", "reply"])
@pytest.mark.parametrize("failure", ["timeout", "cancel", "disconnect"])
async def test_request_cleanup_at_each_await(phase, failure):
    socket = Socket()
    session = make_session(socket)
    blocker = None
    if phase in ("lock", "write"):
        socket.release_write.clear()
    if phase == "lock":
        blocker = asyncio.create_task(session.send(Packet()))
        await socket.write_started.wait()
    query = asyncio.create_task(
        session.request(
            Packet(kind="query"), key=("q", "epoch"), timeout=0.02 if failure == "timeout" else 2
        )
    )
    try:
        if phase == "reply":
            await socket.sent.get()
        elif phase == "write":
            await socket.write_started.wait()
        else:
            await asyncio.sleep(0)
        if failure == "cancel":
            query.cancel()
        elif failure == "disconnect":
            await socket.incoming.put(ConnectionError("lost"))
        error = {
            "timeout": TimeoutError,
            "cancel": asyncio.CancelledError,
            "disconnect": SessionClosed,
        }[failure]
        with pytest.raises(error):
            await asyncio.wait_for(query, 1)
        if failure != "disconnect" and phase != "write":
            # Cancellation of a lock waiter or completed write must leave reusable capacity.
            socket.release_write.set()
            if blocker is not None:
                await blocker
                await socket.sent.get()
            again = asyncio.create_task(
                session.request(
                    Packet(kind="query", request_id="next"), key=("next", "epoch"), timeout=1
                )
            )
            await socket.sent.get()
            await socket.incoming.put(Packet(kind="reply", request_id="next").model_dump_json())
            assert (await again).request_id == "next"
        else:
            with pytest.raises(SessionClosed):
                await session.request(Packet(kind="query"), key=("next", "epoch"), timeout=1)
    finally:
        await session.close()
        if blocker is not None:
            await asyncio.gather(blocker, return_exceptions=True)


@pytest.mark.parametrize("failure", ["close", "disconnect"])
async def test_early_reply_still_requires_successful_send_and_disconnect_fails_request(failure):
    socket = Socket()
    socket.release_write.clear()
    session = make_session(socket)
    query = asyncio.create_task(
        session.request(Packet(kind="query"), key=("q", "epoch"), timeout=1)
    )
    try:
        await socket.write_started.wait()
        await socket.incoming.put(Packet(kind="reply").model_dump_json())
        await socket.incoming.put(Packet(value="barrier").model_dump_json())
        await session.receive()
        assert not query.done()
        if failure == "close":
            await session.close()
        else:
            await socket.incoming.put(ConnectionError("lost"))
        with pytest.raises(SessionClosed):
            await asyncio.wait_for(query, 0.1)
    finally:
        await session.close()


@pytest.mark.parametrize("epoch", ["epoch", "old"])
async def test_reply_isolation_and_four_request_limit(epoch):
    old_socket, socket = Socket(), Socket()
    old, current = make_session(old_socket), make_session(socket)
    queries = [
        asyncio.create_task(
            current.request(
                Packet(kind="query", request_id=str(i)), key=(str(i), "epoch"), timeout=2
            )
        )
        for i in range(4)
    ]
    try:
        for _ in queries:
            await socket.sent.get()
        with pytest.raises(SessionBusy):
            await current.request(
                Packet(kind="query", request_id="fifth"), key=("fifth", "epoch"), timeout=1
            )
        await old_socket.incoming.put(
            Packet(
                kind="reply", request_id="0", epoch=epoch, value="old-connection"
            ).model_dump_json()
        )
        await socket.incoming.put(
            Packet(kind="reply", request_id="0", epoch="old", value="old-epoch").model_dump_json()
        )
        for s, transport in ((old, old_socket), (current, socket)):
            await transport.incoming.put(Packet(value="barrier").model_dump_json())
            await s.receive()
        assert all(not query.done() for query in queries)
        for i in range(4):
            await socket.incoming.put(Packet(kind="reply", request_id=str(i)).model_dump_json())
        replies = await asyncio.gather(*queries)
        assert [r.request_id for r in replies] == ["0", "1", "2", "3"]
        assert all(reply.epoch == "epoch" and reply.value == "" for reply in replies)
    finally:
        await asyncio.gather(old.close(), current.close())
        await asyncio.gather(*queries, return_exceptions=True)


@pytest.mark.parametrize("overflow", [False, True])
async def test_busy_sender_is_bounded_and_never_blocks_reply_reader(overflow):
    socket = Socket()
    release = asyncio.Event()
    active = 0

    async def handle(query):
        nonlocal active
        active += 1
        await release.wait()
        return await respond(query)

    responder = Responder(
        matches=lambda m: m.kind == "query",
        handle=handle,
        busy=lambda m: m.model_copy(update={"kind": "reply", "value": "busy"}),
    )
    session = make_session(socket, responder)
    query = asyncio.create_task(
        session.request(Packet(kind="query", request_id="local"), key=("local", "epoch"), timeout=2)
    )
    await socket.sent.get()
    socket.release_write.clear()
    socket.write_started.clear()
    try:
        for i in range(4):
            await socket.incoming.put(Packet(kind="query", request_id=str(i)).model_dump_json())
        await socket.incoming.put(Packet(value="barrier").model_dump_json())
        await session.receive()
        for i in range(4):
            await socket.incoming.put(
                Packet(kind="query", request_id=f"busy-{i}").model_dump_json()
            )
        await socket.write_started.wait()
        await socket.incoming.put(Packet(kind="reply", request_id="local").model_dump_json())
        assert (await asyncio.wait_for(query, 1)).request_id == "local"
        assert active == 4
        if overflow:
            await socket.incoming.put(Packet(kind="query", request_id="overflow").model_dump_json())
            with pytest.raises(SessionOverflow):
                await asyncio.wait_for(session.receive(), 1)
        else:
            socket.release_write.set()
            replies = [
                Packet.model_validate_json(await asyncio.wait_for(socket.sent.get(), 1))
                for _ in range(4)
            ]
            assert [reply.request_id for reply in replies] == [f"busy-{i}" for i in range(4)]
            assert all(reply.value == "busy" for reply in replies)
    finally:
        await session.close()


async def test_repeated_close_cancellation_still_joins_connection_cleanup():
    # Reconnect must not reuse an owner while the old connection is still cleaning up.
    closing, release_close, handling, cleanup, release_cleanup = (asyncio.Event() for _ in range(5))
    close_calls = 0

    class ClosingSocket(Socket):
        async def close(self, code=1000):
            nonlocal close_calls
            close_calls += 1
            closing.set()
            await release_close.wait()

    async def handle(query):
        handling.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release_cleanup.wait()
        return await respond(query)

    socket = ClosingSocket()
    session = make_session(
        socket,
        Responder(
            matches=lambda m: m.kind == "query",
            handle=handle,
            busy=lambda m: m.model_copy(update={"kind": "reply"}),
        ),
    )
    await socket.incoming.put(Packet(kind="query").model_dump_json())
    await handling.wait()
    query = asyncio.create_task(
        session.request(Packet(kind="query"), key=("q", "epoch"), timeout=1)
    )
    await socket.sent.get()
    close = asyncio.create_task(session.close())
    try:
        await closing.wait()
        await cleanup.wait()
        with pytest.raises(SessionClosed):
            await asyncio.wait_for(query, 1)
        for _ in range(2):
            close.cancel()
            await asyncio.sleep(0)
            assert not close.done()
        release_close.set()
        await asyncio.sleep(0)
        assert not close.done()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(close, 1)
        await session.close()
        assert close_calls == 1 and not socket.reading and not socket.writing
    finally:
        release_close.set()
        release_cleanup.set()
        await session.close()
        await asyncio.gather(close, query, return_exceptions=True)
