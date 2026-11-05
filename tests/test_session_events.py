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


@pytest.mark.parametrize("reverse", [False, True])
async def test_gateway_deadline_covers_callback_and_cleanup(reverse):
    # A slow business callback must not defeat the stop deadline or nest its own receive.
    # Independently mounted providers share the same driver regardless of bind order.
    socket = Socket()
    session = make_session(socket)
    gateway = Gateway[Packet, Packet]()
    seen = []

    class Callbacks:
        def bind(self, routes):
            routes.configure(
                opened=self.opened,
                message=self.message,
                unmatched_reply=self.on_error,
                validation_error=self.on_error,
                closed=self.closed,
            )

        async def opened(self):
            seen.append("opened")

        async def message(self, message):
            seen.append("message")

        async def on_error(self, error, message):
            pytest.fail("unexpected validation error")

        def closed(self):
            seen.append("closed")

    class Events:
        def bind(self, routes):
            self.routes = routes
            routes.register("test.event", Call, self.on_event)
            routes.before_wait(self.prepare)

        async def prepare(self):
            with pytest.raises(RuntimeError, match="frozen"):
                self.routes.before_wait(self.prepare)
            with pytest.raises(RuntimeError, match="nested"):
                await session.next_event()
            seen.append("prepare")

        async def on_event(self, item, message):
            with pytest.raises(RuntimeError, match="nested"):
                await session.next_event()
            with pytest.raises(RuntimeError):
                async with session.driving():
                    pass
            session.set_deadline(asyncio.get_running_loop().time())
            await asyncio.Event().wait()

    await socket.incoming.put(packet(event()).model_dump_json())
    callbacks = (Callbacks(), Events())
    with pytest.raises(TimeoutError):
        await gateway.serve(
            "peer", session, *(reversed(callbacks) if reverse else callbacks), replace=False
        )
    assert seen == ["opened", "prepare", "message", "closed"]
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
        message=lambda message: callback(None, message),
        unmatched_reply=callback,
        validation_error=callback,
    )
    routes.freeze()
    with pytest.raises(RuntimeError):
        routes.register("test.query", Call, callback)


@pytest.mark.parametrize("registered", [False, True])
async def test_reply_hook_runs_after_correlation_before_original_caller(registered):
    # Two simultaneous calls of the same method must reach different callers. A hook
    # may reject a correlated reply without either Future receiving an unvalidated result.
    # A same-method request route must not intercept either reply.
    first, second = query("first"), query("second")
    seen = []
    routes = Dispatcher[Packet]()

    async def unexpected(*args):
        pytest.fail("unexpected request or lifecycle dispatch")

    async def unmatched(item, message):
        seen.append("unmatched")

    async def hook(item, message):
        seen.append("hook:" + item.value)
        if item.value == "invalid":
            raise ValueError("invalid reply")

    async def deliver_first(item, message):
        seen.append("first:" + item.value)
        routes.forget_reply(first.message_id)

    async def deliver_second(item, message):
        seen.append("second:" + item.value)
        routes.forget_reply(second.message_id)

    if registered:
        routes.register(first.method, Call, unexpected)
    routes.configure(
        opened=unexpected,
        message=unexpected,
        unmatched_reply=unmatched,
        validation_error=unexpected,
        unrouted=unexpected,
    )
    routes.freeze()
    routes.expect_reply(first, Reply, deliver_first, on_reply=hook)
    routes.expect_reply(second, Reply, deliver_second, on_reply=hook)

    unrelated = reply(query())
    await routes.dispatch(unrelated, packet(unrelated))
    invalid = reply(first, "invalid")
    with pytest.raises(ValueError, match="invalid reply"):
        await routes.dispatch(invalid, packet(invalid))
    for request in (second, first, first):
        response = reply(request)
        await routes.dispatch(response, packet(response))
    assert seen == [
        "unmatched",
        "hook:invalid",
        "hook:second",
        "second:second",
        "hook:first",
        "first:first",
        "unmatched",
    ]
