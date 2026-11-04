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
from app.contracts.protocol import (
    Message,
    ToolCall,
    ToolResult,
    call_header,
    frame_header,
    result_header,
)


class Call(ToolCall):
    method: Literal["test.query", "test.event"]
    value: str = ""


class Reply(ToolResult):
    method: Literal["test.query"]
    value: str = ""


class Packet(Message[Call | Reply]):
    """Symmetric test frames use the current strict headers and item identities."""


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
    return Packet(**frame_header(epoch), control=control, payload=list(items))


def ack(message_id: str) -> Packet:
    header = frame_header(None)
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


def reply_key(item: ToolCall | ToolResult, epoch: str):
    return (item.reply_to, epoch) if isinstance(item, Reply) else None


def is_query(item: ToolCall | ToolResult) -> bool:
    return isinstance(item, Call) and item.method == "test.query"


def make_session(socket, responder=None):
    session = Session[Packet, Packet](
        socket,
        parse=Packet.model_validate_json,
        make_ack=ack,
        reply_key=reply_key,
        responder=responder,
        epoch="epoch",
    )
    session.start()
    return session


async def request(session: Session[Packet, Packet], call: Call, timeout=1) -> Reply:
    result, _frame = await session.request(
        packet(call),
        call=call,
        key=(call.message_id, "epoch"),
        timeout=timeout,
    )
    assert isinstance(result, Reply)
    return result


async def respond(item: ToolCall | ToolResult, epoch: str) -> Packet:
    assert isinstance(item, Call)
    return packet(reply(item), epoch=epoch)


def busy(item: ToolCall | ToolResult, epoch: str) -> Packet:
    assert isinstance(item, Call)
    return packet(reply(item, "busy"), epoch=epoch)


async def test_duplex_queries_bypass_fifo_and_consumer_cancellation():
    left_socket, right_socket = Socket(), Socket()
    left_socket.sent = right_socket.incoming
    right_socket.sent = left_socket.incoming
    responder = Responder(matches=is_query, handle=respond, busy=busy)
    left, right = make_session(left_socket, responder), make_session(right_socket, responder)
    try:
        waiting = asyncio.create_task(left.next_event())
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        # No business consumer runs until both replies bypass the queued commands.
        for session in (left, right):
            await session.send(packet(event("one")))
            await session.send(packet(event("two")))
        replies = await asyncio.gather(
            *(request(s, query(str(i))) for i, s in enumerate((left, right)))
        )
        assert [result.value for result in replies] == ["0", "1"]
        for session in (left, right):
            assert [
                (await session.next_event()).payload[0].value,
                (await session.next_event()).payload[0].value,
            ] == ["one", "two"]
    finally:
        await asyncio.gather(left.close(), right.close())
    assert not left_socket.reading and not right_socket.reading
    with pytest.raises(SessionClosed):
        await left.next_event()


@pytest.mark.parametrize("failure", ["disconnect", "count", "bytes"])
async def test_read_failure_fails_pending_immediately_but_drains_fifo(failure):
    socket = Socket()
    session = make_session(socket)
    pending = asyncio.create_task(request(session, query()))
    await socket.sent.get()
    raw = packet(event("中" * 3_000_000 if failure == "bytes" else "event")).model_dump_json()
    count = 1 if failure == "disconnect" else 2 if failure == "bytes" else 33
    try:
        for _ in range(count):
            await socket.incoming.put(raw)
        if failure == "disconnect":
            await socket.incoming.put(ConnectionError("lost"))
        with pytest.raises(SessionClosed):
            await asyncio.wait_for(pending, 2)
        buffered = 32 if failure == "count" else 1
        for _ in range(buffered):
            assert await session.next_event() == Packet.model_validate_json(raw)
        with pytest.raises(ConnectionError if failure == "disconnect" else SessionOverflow):
            await session.next_event()
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


@pytest.mark.parametrize("phase", ["lock", "write", "reply"])
@pytest.mark.parametrize("failure", ["timeout", "cancel", "disconnect"])
async def test_request_cleanup_at_each_await(phase, failure):
    socket = Socket()
    session = make_session(socket)
    blocker = None
    if phase in ("lock", "write"):
        socket.release_write.clear()
    if phase == "lock":
        blocker = asyncio.create_task(session.send(packet(event())))
        await socket.write_started.wait()
    call = query()
    pending = asyncio.create_task(
        request(session, call, timeout=0.02 if failure == "timeout" else 2)
    )
    try:
        if phase == "reply":
            await socket.sent.get()
        elif phase == "write":
            await socket.write_started.wait()
        else:
            await asyncio.sleep(0)
        if failure == "cancel":
            pending.cancel()
        elif failure == "disconnect":
            await socket.incoming.put(ConnectionError("lost"))
        error = {
            "timeout": TimeoutError,
            "cancel": asyncio.CancelledError,
            "disconnect": SessionClosed,
        }[failure]
        with pytest.raises(error):
            await asyncio.wait_for(pending, 1)
        if failure != "disconnect" and phase != "write":
            # A late response must not consume the replacement request's slot or FIFO.
            socket.release_write.set()
            if blocker is not None:
                await blocker
                await socket.sent.get()
            next_call = query("next")
            again = asyncio.create_task(request(session, next_call))
            await socket.sent.get()
            await socket.incoming.put(packet(reply(call), event("barrier")).model_dump_json())
            assert (await session.next_event()).payload[0].value == "barrier"
            assert not again.done()
            await socket.incoming.put(packet(reply(next_call)).model_dump_json())
            assert (await again).reply_to == next_call.message_id
        else:
            with pytest.raises(SessionClosed):
                await request(session, query())
    finally:
        await session.close()
        if blocker is not None:
            await asyncio.gather(blocker, return_exceptions=True)


@pytest.mark.parametrize("failure", ["close", "disconnect"])
async def test_early_reply_still_requires_successful_send_and_disconnect_fails_request(failure):
    socket = Socket()
    socket.release_write.clear()
    session = make_session(socket)
    call = query()
    pending = asyncio.create_task(request(session, call))
    try:
        await socket.write_started.wait()
        await socket.incoming.put(packet(reply(call), event("barrier")).model_dump_json())
        await session.next_event()
        assert not pending.done()
        if failure == "close":
            await session.close()
        else:
            await socket.incoming.put(ConnectionError("lost"))
        with pytest.raises(SessionClosed):
            await asyncio.wait_for(pending, 0.1)
    finally:
        await session.close()


@pytest.mark.parametrize("epoch", ["epoch", "old"])
async def test_reply_isolation_and_four_request_limit(epoch):
    old_socket, socket = Socket(), Socket()
    old, current = make_session(old_socket), make_session(socket)
    calls = [query(str(i)) for i in range(4)]
    queries = [asyncio.create_task(request(current, call, timeout=2)) for call in calls]
    try:
        for _ in queries:
            await socket.sent.get()
        with pytest.raises(SessionBusy):
            await request(current, query("fifth"))
        await old_socket.incoming.put(
            packet(reply(calls[0], "old-connection"), epoch=epoch).model_dump_json()
        )
        await socket.incoming.put(
            packet(reply(calls[0], "old-epoch"), epoch="old").model_dump_json()
        )
        for s, transport in ((old, old_socket), (current, socket)):
            await transport.incoming.put(packet(event("barrier")).model_dump_json())
            await s.next_event()
        assert all(not pending.done() for pending in queries)
        # Same method, reverse arrival order, one batch: only reply_to chooses a Future.
        await socket.incoming.put(
            packet(*(reply(call) for call in reversed(calls))).model_dump_json()
        )
        replies = await asyncio.gather(*queries)
        assert [r.reply_to for r in replies] == [call.message_id for call in calls]
        assert [r.value for r in replies] == ["0", "1", "2", "3"]
    finally:
        await asyncio.gather(old.close(), current.close())
        await asyncio.gather(*queries, return_exceptions=True)


@pytest.mark.parametrize("overflow", [False, True])
async def test_busy_sender_is_bounded_and_never_blocks_reply_reader(overflow):
    socket = Socket()
    release = asyncio.Event()
    active = 0

    async def handle(item, epoch):
        nonlocal active
        active += 1
        await release.wait()
        return await respond(item, epoch)

    responder = Responder(matches=is_query, handle=handle, busy=busy)
    session = make_session(socket, responder)
    local = query("local")
    pending = asyncio.create_task(request(session, local, timeout=2))
    await socket.sent.get()
    socket.release_write.clear()
    socket.write_started.clear()
    try:
        await socket.incoming.put(
            packet(*(query(str(i)) for i in range(4)), event("barrier")).model_dump_json()
        )
        await session.next_event()
        calls = [query(f"busy-{i}") for i in range(4)]
        await socket.incoming.put(packet(*calls).model_dump_json())
        await socket.write_started.wait()
        await socket.incoming.put(packet(reply(local)).model_dump_json())
        assert (await asyncio.wait_for(pending, 1)).reply_to == local.message_id
        assert active == 4
        if overflow:
            await socket.incoming.put(packet(query("overflow")).model_dump_json())
            with pytest.raises(SessionOverflow):
                await asyncio.wait_for(session.next_event(), 1)
        else:
            socket.release_write.set()
            replies = [
                Packet.model_validate_json(await asyncio.wait_for(socket.sent.get(), 1)).payload[0]
                for _ in range(4)
            ]
            assert [result.reply_to for result in replies] == [call.message_id for call in calls]
            assert all(result.value == "busy" for result in replies)
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

    async def handle(item, epoch):
        handling.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release_cleanup.wait()
        return await respond(item, epoch)

    socket = ClosingSocket()
    session = make_session(
        socket,
        Responder(
            matches=is_query,
            handle=handle,
            busy=busy,
        ),
    )
    await socket.incoming.put(packet(query()).model_dump_json())
    await handling.wait()
    pending = asyncio.create_task(request(session, query()))
    await socket.sent.get()
    close = asyncio.create_task(session.close())
    try:
        await closing.wait()
        await cleanup.wait()
        with pytest.raises(SessionClosed):
            await asyncio.wait_for(pending, 1)
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
        await asyncio.gather(close, pending, return_exceptions=True)


async def test_mixed_payload_bypasses_only_matching_items_and_keeps_business_order():
    socket = Socket()
    session = make_session(
        socket,
        Responder(
            matches=is_query,
            handle=respond,
            busy=busy,
        ),
    )
    local, remote = query("local"), query("remote")
    pending = asyncio.create_task(request(session, local))
    await socket.sent.get()
    try:
        mixed = packet(event("first"), reply(local), remote, event("second"))
        mixed = mixed.model_copy(update={"ack": True, "ack_id": "a" * 32})
        await socket.incoming.put(mixed.model_dump_json())
        assert (await pending).reply_to == local.message_id
        business = await session.next_event()
        assert [item.value for item in business.payload] == ["first", "second"]
        assert business.message_id == mixed.message_id
        answer = Packet.model_validate_json(await asyncio.wait_for(socket.sent.get(), 1))
        assert len(answer.payload) == 1
        result = answer.payload[0]
        assert isinstance(result, Reply) and result.reply_to == remote.message_id
        assert result.value == "remote"
        assert not answer.ack  # Unknown receipt ACKs and business do not generate ACKs.
        await socket.incoming.put(packet(event("next frame")).model_dump_json())
        assert (await session.next_event()).payload[0].value == "next frame"
        assert socket.sent.empty()
    finally:
        await session.close()


async def test_request_selects_one_call_from_batch_not_its_frame_or_neighbor():
    socket = Socket()
    session = make_session(socket)
    selected, neighbor = query("selected"), query("neighbor")
    batch = packet(neighbor, event("business"), selected)
    pending = asyncio.create_task(
        session.request(
            batch,
            call=selected,
            key=(selected.message_id, "epoch"),
            timeout=1,
        )
    )
    try:
        assert Packet.model_validate_json(await socket.sent.get()) == batch
        frame_reply = Reply(**result_header(batch.message_id), method="test.query", value="frame")
        await socket.incoming.put(
            packet(reply(neighbor), frame_reply, event("barrier")).model_dump_json()
        )
        assert (await session.next_event()).payload[0].value == "barrier"
        assert not pending.done()
        response = reply(selected)
        delivery = packet(response)
        await socket.incoming.put(delivery.model_dump_json())
        assert await pending == (response, delivery)
    finally:
        await session.close()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("control", ["registered", "heartbeat"])
async def test_control_payload_waits_for_owner_before_query_dispatch(control):
    socket = Socket()
    handled = []

    async def handle(item, epoch):
        handled.append(item.message_id)
        return await respond(item, epoch)

    session = make_session(
        socket,
        Responder(
            matches=is_query,
            handle=handle,
            busy=busy,
        ),
    )
    call = query()
    incoming = packet(call, event("business"), control=control)
    try:
        await socket.incoming.put(incoming.model_dump_json())
        received = await session.next_event()
        assert received == incoming and handled == []
        # Receipt ACK may precede authorization, but cannot run the history callback.
        receipt = Packet.model_validate_json(await asyncio.wait_for(socket.sent.get(), 1))
        assert receipt.ack_id == incoming.message_id and receipt.payload == []
        assert handled == []
        assert session.dispatch_item(received.payload[0], received)
        assert not session.dispatch_item(received.payload[1], received)
        answer = Packet.model_validate_json(await asyncio.wait_for(socket.sent.get(), 1))
        assert answer.payload[0].reply_to == call.message_id
        assert handled == [call.message_id]
    finally:
        await session.close()


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
        assert [frame.ack_id for frame in delivered[1:]] == [frame.message_id for frame in controls]
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
    call = query()
    pending = asyncio.create_task(request(session, call))
    await socket.sent.get()
    socket.release_write.clear()
    socket.write_started.clear()
    try:
        # Draining controls keeps the inbox empty; only the ACK FIFO can overflow.
        for _ in range(32):
            incoming = packet(control="heartbeat")
            await socket.incoming.put(incoming.model_dump_json())
            assert await asyncio.wait_for(session.next_event(), 1) == incoming
        await socket.write_started.wait()
        await socket.incoming.put(packet(reply(call)).model_dump_json())
        assert (await asyncio.wait_for(pending, 1)).reply_to == call.message_id
        await socket.incoming.put(packet(control="heartbeat").model_dump_json())
        with pytest.raises(SessionOverflow):
            await asyncio.wait_for(session.next_event(), 1)
        assert socket.sent.empty()
    finally:
        await session.close()
        await asyncio.gather(pending, return_exceptions=True)
    assert not socket.writing and not socket.reading


@pytest.mark.parametrize("control", [None, "heartbeat"])
async def test_batch_item_capacity_is_preflighted_before_any_reply_or_control_effect(control):
    socket = Socket()
    session = make_session(socket)
    call = query()
    pending = asyncio.create_task(request(session, call))
    await socket.sent.get()
    buffered = packet(*(event(str(i)) for i in range(31)))
    try:
        await socket.incoming.put(buffered.model_dump_json())
        # Two items overflow 32 even though there are only two queued frames and one
        # item could bypass. Resolving the pending result before preflight is unsafe.
        await socket.incoming.put(
            packet(reply(call), event("overflow"), control=control).model_dump_json()
        )
        with pytest.raises(SessionClosed):
            await asyncio.wait_for(pending, 1)
        assert await session.next_event() == buffered
        with pytest.raises(SessionOverflow):
            await session.next_event()
        assert socket.sent.empty()  # The rejected control must not be acknowledged.
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


async def test_control_only_frames_still_have_a_bounded_business_inbox():
    socket = Socket()
    session = make_session(socket)
    controls = [packet(control="heartbeat") for _ in range(32)]
    try:
        for control in controls:
            await socket.incoming.put(control.model_dump_json())
            # Drain receipt writes but not the owner inbox: frames, not ACKs/items,
            # must account for this backlog of unprocessed lease decisions.
            receipt = Packet.model_validate_json(await asyncio.wait_for(socket.sent.get(), 1))
            assert receipt.ack_id == control.message_id
        await socket.incoming.put(packet(control="heartbeat").model_dump_json())
        # Wait for the reader to process the extra frame without draining its inbox.
        await socket.incoming.put(ConnectionError("reader barrier"))
        await asyncio.sleep(0)
        assert [await session.next_event() for _ in controls] == controls
        with pytest.raises(SessionOverflow):
            await asyncio.wait_for(session.next_event(), 1)
    finally:
        await session.close()
