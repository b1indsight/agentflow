import asyncio
from typing import Literal

import pytest

from app.communication.session import Session, SessionClosed, SessionOverflow
from app.contracts.protocol import (
    Message,
    ToolCall,
    ToolResult,
    call_header,
    message_header,
    result_header,
)


class Call(ToolCall):
    method: Literal["test.query", "test.event"]
    value: str = ""


class Reply(ToolResult):
    method: Literal["test.query"]
    value: str = ""


class Packet(Message[Call | Reply]):
    """Symmetric test messages use the current strict headers and item identities."""


def query(value="") -> Call:
    return Call(**call_header(), method="test.query", value=value)


def event(value="") -> Call:
    return Call(**call_header(), method="test.event", value=value)


def reply(call: Call, value: str | None = None) -> Reply:
    return Reply(
        **result_header(call.message_id),
        method="test.query",
        value=call.value if value is None else value,
    )


def packet(*items: Call | Reply, epoch="epoch", control=None) -> Packet:
    return Packet(**message_header(epoch), control=control, payload=list(items))


def ack(message_id: str) -> Packet:
    header = message_header(None)
    header["ack"] = True
    header["ack_id"] = message_id
    return Packet(**header, control=None, payload=[])


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


def make_session(socket):
    session = Session[Packet, Packet](
        socket, parse=Packet.model_validate_json, make_ack=ack, epoch="epoch"
    )
    session.start()
    return session


@pytest.mark.parametrize("control", [None, "registered", "heartbeat"])
async def test_every_payload_keeps_fifo_position_after_consumer_cancellation(control):
    socket = Socket()
    session = make_session(socket)
    waiting = asyncio.create_task(session.next_event())
    await asyncio.sleep(0)
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    call = query()
    mixed = packet(event("first"), reply(call), call, event("last"), control=control)
    try:
        await socket.incoming.put(mixed.model_dump_json())
        assert await session.next_event() == mixed
        if control:
            receipt = Packet.model_validate_json(await asyncio.wait_for(socket.sent.get(), 1))
            assert receipt.ack_id == mixed.message_id and not receipt.payload
        assert socket.sent.empty()  # No request or reply runs outside the owner driver.
        following = packet(event("next"))
        await socket.incoming.put(following.model_dump_json())
        assert await session.next_event() == following
    finally:
        await session.close()


@pytest.mark.parametrize("capacity", ["count", "bytes"])
async def test_bounded_fifo_backpressures_bursts_then_drains_before_disconnect(capacity):
    socket = Socket()
    session = make_session(socket)
    raw = packet(event("中" * 3_000_000 if capacity == "bytes" else "event")).model_dump_json()
    count = 2 if capacity == "bytes" else 33
    try:
        for _ in range(count):
            socket.incoming.put_nowait(raw)
        socket.incoming.put_nowait(ConnectionError("lost"))
        await asyncio.sleep(0)  # Reader fills the bounded FIFO before the consumer resumes.
        assert len(session._inbox) == (1 if capacity == "bytes" else 32)
        for _ in range(count):
            assert await session.next_event() == Packet.model_validate_json(raw)
        with pytest.raises(ConnectionError, match="lost"):
            await session.next_event()
    finally:
        await session.close()


async def test_single_oversized_message_is_rejected_without_delivery():
    socket = Socket()
    session = make_session(socket)
    try:
        socket.incoming.put_nowait(packet(event("中" * 6_000_000)).model_dump_json())
        with pytest.raises(SessionOverflow):
            await session.next_event()
        assert socket.sent.empty()
    finally:
        await session.close()


async def test_serialized_send_checks_guard_at_actual_write_and_close_joins_without_canceling_caller():
    socket = Socket()
    socket.release_write.clear()
    session = make_session(socket)
    first = asyncio.create_task(session.send(packet(event("first"))))
    await socket.write_started.wait()
    current = True
    second = asyncio.create_task(
        session.send(packet(event("cancelled")), is_current=lambda: current)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    current = False
    socket.release_write.set()
    assert await first
    assert not await second
    assert Packet.model_validate_json(await socket.sent.get()).payload[0].value == "first"
    assert socket.sent.empty()

    socket.release_write.clear()
    socket.write_started.clear()
    writing = asyncio.create_task(session.send(packet(event())))
    await socket.write_started.wait()
    waiting = asyncio.create_task(session.send(packet(event())))
    await socket.incoming.put(packet(event("discard on close")).model_dump_json())
    await session.close()
    for task in (writing, waiting):
        with pytest.raises(SessionClosed):
            await task
        assert not task.cancelled()
    assert not socket.writing and not socket.reading
    with pytest.raises(SessionClosed):
        await session.next_event()


async def test_repeated_close_cancellation_still_joins_connection_cleanup():
    # Reconnect must not reuse an owner while the old connection is still cleaning up.
    closing, release_close, observing, cleanup, release_cleanup = (
        asyncio.Event() for _ in range(5)
    )
    close_calls = 0

    class ClosingSocket(Socket):
        async def close(self, code=1000):
            nonlocal close_calls
            close_calls += 1
            closing.set()
            await release_close.wait()

    async def observe():
        observing.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release_cleanup.wait()

    async def consume(value):
        pytest.fail("observer should be cancelled")

    socket = ClosingSocket()
    session = make_session(socket)
    session.watch("observer", observe, consume)
    await observing.wait()
    close = asyncio.create_task(session.close())
    try:
        await closing.wait()
        await cleanup.wait()
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
        await asyncio.gather(close, return_exceptions=True)


async def test_control_ack_fifo_piggyback_is_guarded_and_does_not_mutate_caller():
    socket = Socket()
    session = make_session(socket)
    socket.release_write.clear()
    first = asyncio.create_task(session.send(packet(event("blocking"))))
    await socket.write_started.wait()
    outgoing = packet(event("business"))
    rejected = asyncio.create_task(session.send(outgoing, is_current=lambda: False))
    sent = asyncio.create_task(session.send(outgoing))
    await asyncio.sleep(0)  # Both business sends join the lock before the ACK sender.
    controls = [packet(control="heartbeat") for _ in range(2)]
    controls[0] = controls[0].model_copy(update={"ack": True, "ack_id": "a" * 32})
    try:
        for incoming in controls:
            await socket.incoming.put(incoming.model_dump_json())
            assert await session.next_event() == incoming
        socket.release_write.set()
        assert await first and not await rejected and await sent
        delivered = [
            Packet.model_validate_json(await asyncio.wait_for(socket.sent.get(), 1))
            for _ in range(3)
        ]
        assert not delivered[0].ack
        assert delivered[1].payload == outgoing.payload
        assert [message.ack_id for message in delivered[1:]] == [
            message.message_id for message in controls
        ]
        assert delivered[2].connection_epoch is None and not delivered[2].payload
        assert not outgoing.ack and outgoing.ack_id is None
        # A pure ACK must not queue, bind an epoch, or produce a receipt loop.
        await socket.incoming.put(ack(delivered[2].message_id).model_dump_json())
        await socket.incoming.put(packet(event("barrier")).model_dump_json())
        assert (await session.next_event()).payload[0].value == "barrier"
        outbound_barrier = packet(event("outbound-barrier"))
        await session.send(outbound_barrier)
        assert Packet.model_validate_json(await socket.sent.get()) == outbound_barrier
        assert socket.sent.empty()
    finally:
        await session.close()
        await asyncio.gather(first, rejected, sent, return_exceptions=True)


async def test_ack_fifo_counts_inflight_write_until_success_and_reader_keeps_progressing():
    socket = Socket()
    session = make_session(socket)
    socket.release_write.clear()
    try:
        # Draining controls keeps the inbox empty; only the ACK FIFO can overflow.
        for _ in range(32):
            incoming = packet(control="heartbeat")
            await socket.incoming.put(incoming.model_dump_json())
            assert await asyncio.wait_for(session.next_event(), 1) == incoming
        await socket.write_started.wait()
        response = packet(reply(query()))
        await socket.incoming.put(response.model_dump_json())
        assert await asyncio.wait_for(session.next_event(), 1) == response
        await socket.incoming.put(packet(control="heartbeat").model_dump_json())
        with pytest.raises(SessionOverflow):
            await asyncio.wait_for(session.next_event(), 1)
        assert socket.sent.empty()
    finally:
        await session.close()
    assert not socket.writing and not socket.reading


@pytest.mark.parametrize("control", [None, "heartbeat"])
async def test_batch_waits_for_item_capacity_before_any_reply_or_control_effect(control):
    socket = Socket()
    session = make_session(socket)
    buffered = packet(*(event(str(i)) for i in range(31)))
    following = packet(reply(query()), event("next"), control=control)
    try:
        socket.incoming.put_nowait(buffered.model_dump_json())
        socket.incoming.put_nowait(following.model_dump_json())
        await asyncio.sleep(0)
        assert socket.sent.empty()  # A not-yet-buffered control has no receipt ACK.
        assert await session.next_event() == buffered
        assert await session.next_event() == following
    finally:
        await session.close()


async def test_authenticated_initial_control_ack_and_pure_ack_do_not_bind_epoch():
    socket = Socket()
    session = Session[Packet, Packet](socket, parse=Packet.model_validate_json, make_ack=ack)
    initial = packet(epoch=None, control="register")
    session.register_control(initial)
    session.start()
    try:
        response = Packet.model_validate_json(await asyncio.wait_for(socket.sent.get(), 1))
        assert response.ack_id == initial.message_id
        assert response.payload == [] and response.connection_epoch is None
        await socket.incoming.put(ack(response.message_id).model_dump_json())
        await socket.incoming.put(packet(event("barrier")).model_dump_json())
        assert (await session.next_event()).payload[0].value == "barrier"
        outbound_barrier = packet(event("outbound-barrier"))
        await session.send(outbound_barrier)
        assert Packet.model_validate_json(await socket.sent.get()) == outbound_barrier
        assert session.epoch is None and socket.sent.empty()
    finally:
        await session.close()


async def test_control_only_messages_still_have_a_bounded_business_inbox():
    socket = Socket()
    session = make_session(socket)
    controls = [packet(control="heartbeat") for _ in range(32)]
    try:
        for control in controls:
            await socket.incoming.put(control.model_dump_json())
            # Drain receipt writes but not the owner inbox: messages, not ACKs/items,
            # must account for this backlog of unprocessed lease decisions.
            receipt = Packet.model_validate_json(await asyncio.wait_for(socket.sent.get(), 1))
            assert receipt.ack_id == control.message_id
        following = packet(control="heartbeat")
        await socket.incoming.put(following.model_dump_json())
        # Wait for the reader to process the extra message without draining its inbox.
        await socket.incoming.put(ConnectionError("reader barrier"))
        await asyncio.sleep(0)
        assert len(session._inbox) == 32 and socket.sent.empty()
        assert [await session.next_event() for _ in controls] == controls
        assert await session.next_event() == following
        with pytest.raises(ConnectionError, match="reader barrier"):
            await asyncio.wait_for(session.next_event(), 1)
    finally:
        await session.close()
