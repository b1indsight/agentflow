"""The shared driver serializes business while owning only communication observers."""

import asyncio

import pytest

from app.communication.dispatch import Dispatcher
from app.communication.gateway import Gateway
from tests.test_communication_session import (
    Call,
    Packet,
    Reply,
    Socket,
    event,
    make_session,
    packet,
    query,
    reply,
)


async def test_ready_fairness_rearm_and_read_failure_drain():
    socket = Socket()
    session = make_session(socket)
    seen = []

    async def source():
        return "ready"

    async def consume(value):
        seen.append(value)

    try:
        for _ in range(50):
            session.watch("tool", source, consume)
            await asyncio.sleep(0)
            await session.unwatch("tool")
        session.watch("tool", source, consume)
        for value in ("first", "second"):
            await socket.incoming.put(packet(event(value)).model_dump_json())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert (await session.next_event()).payload[0].value == "first"
        ready = await session.next_event()
        assert callable(ready)
        await ready()
        assert seen == ["ready"]
        assert (await session.next_event()).payload[0].value == "second"
        session.watch("tool", source, consume)
        await socket.incoming.put(packet(event("last")).model_dump_json())
        await socket.incoming.put(ConnectionError("EOF"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert (await session.next_event()).payload[0].value == "last"
        with pytest.raises(ConnectionError, match="EOF"):
            await session.next_event()
        assert seen == ["ready"]
    finally:
        await session.close()


async def test_gateway_deadline_covers_callback_and_cleanup():
    # A slow business callback must not defeat the stop deadline or nest its own receive.
    socket = Socket()
    session = make_session(socket)
    gateway = Gateway[Packet, Packet]()
    seen = []

    class Callbacks:
        def bind(self, routes):
            routes.register("test.event", Call, self.on_event)
            routes.configure(
                opened=self.opened,
                frame=self.frame,
                result=self.on_error,
                validation_error=self.on_error,
                closed=self.closed,
            )

        async def opened(self):
            seen.append("opened")

        async def frame(self, message):
            seen.append("frame")

        async def on_error(self, error, message):
            pytest.fail("unexpected validation error")

        async def on_event(self, item, message):
            with pytest.raises(RuntimeError, match="nested"):
                await session.next_event()
            with pytest.raises(RuntimeError):
                async with session.driving():
                    pass
            session.set_deadline(asyncio.get_running_loop().time())
            await asyncio.Event().wait()

        def closed(self):
            seen.append("closed")

    await socket.incoming.put(packet(event()).model_dump_json())
    with pytest.raises(TimeoutError):
        await gateway.serve("peer", session, Callbacks(), replace=False)
    assert seen == ["opened", "frame", "closed"]
    assert session.closed and gateway.get("peer") is None


def test_registration_is_exact_frozen_and_direction_local():
    async def callback(item, message):
        pass

    routes = Dispatcher[Packet]()
    for name in ("*", "test.*", "event", "test.a.b", " Test.event", "Test.event"):
        with pytest.raises(ValueError):
            routes.register(name, Call, callback)
    routes.register("test.event", Call, callback)
    with pytest.raises(ValueError, match="duplicate"):
        routes.register("test.event", Call, callback)
    with pytest.raises(TypeError, match="request"):
        routes.register("test.query", Reply, callback)
    routes.configure(
        opened=lambda: callback(None, None),
        frame=lambda msg: callback(None, msg),
        result=callback,
        validation_error=callback,
    )
    routes.freeze()
    with pytest.raises(RuntimeError):
        routes.register("test.query", Call, callback)


@pytest.mark.parametrize("registered", [False, True])
async def test_reply_reaches_its_owner_without_a_method_route(registered):
    # Bidirectional calls can share a method; replies must not invoke its request handler.
    request = query()
    response = reply(request)
    received = []

    async def unexpected(*args):
        pytest.fail("a reply entered request routing or error handling")

    async def on_result(item, message):
        assert item.reply_to == request.message_id
        received.append(item)

    routes = Dispatcher[Packet]()
    if registered:
        routes.register(request.method, Call, unexpected)
    routes.configure(
        opened=unexpected,
        frame=unexpected,
        result=on_result,
        validation_error=unexpected,
        unrouted=unexpected,
    )
    routes.freeze()
    await routes.dispatch(response, packet(response))
    assert received == [response]
