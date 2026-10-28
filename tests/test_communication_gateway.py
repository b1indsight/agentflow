import asyncio

import pytest

from app.communication.gateway import Gateway
from app.communication.session import Responder, SessionClosed
from tests.test_communication_session import Packet, Socket, make_session, respond


class ClosingSocket(Socket):
    def __init__(self, *, blocked=False, error=None):
        super().__init__()
        self.closing = asyncio.Event()
        self.release_close = asyncio.Event()
        if not blocked:
            self.release_close.set()
        self.codes = []
        self.error = error

    async def close(self, code=1000):
        self.codes.append(code)
        self.closing.set()
        await self.release_close.wait()
        if self.error is not None:
            raise self.error


async def test_same_gateway_routes_bidirectional_sessions_and_rejects_overlap():
    left_socket, right_socket = ClosingSocket(), ClosingSocket()
    left_socket.sent = right_socket.incoming
    right_socket.sent = left_socket.incoming
    responder = Responder(
        matches=lambda message: message.kind == "query",
        handle=respond,
        busy=lambda message: message.model_copy(update={"kind": "reply", "value": "busy"}),
    )
    left, right = make_session(left_socket, responder), make_session(right_socket, responder)
    worker, server = Gateway[Packet, Packet](), Gateway[Packet, Packet]()
    worker_registration = await worker.register("server", left, replace=False)
    server_registration = await server.register("worker", right, replace=True)
    candidate = make_session(ClosingSocket())
    try:
        assert worker.get("server") is left
        assert server.get("worker") is right
        responses = await asyncio.gather(
            *(
                session.request(Packet(kind="query", value=value), key=("q", "epoch"), timeout=1)
                for session, value in ((left, "from-worker"), (right, "from-server"))
            )
        )
        assert [response.value for response in responses] == ["from-worker", "from-server"]
        with pytest.raises(RuntimeError, match="already registered"):
            await worker.register("server", candidate, replace=False)
        with pytest.raises(ValueError, match="session already registered"):
            await worker.register("other", left, replace=True)
        await worker.unregister(server_registration)  # Foreign handles cannot close another peer.
        assert not right.closed
        worker.seal()
        assert worker.get("server") is None
        with pytest.raises(SessionClosed):
            await worker.register("server", candidate, replace=False)
    finally:
        await asyncio.gather(worker.close(), server.close(), candidate.close())
        await worker.unregister(worker_registration)
        await server.unregister(server_registration)
    assert left_socket.codes == right_socket.codes == [1000]


@pytest.mark.parametrize("newest_exits", [False, True])
async def test_overlapping_replacements_never_republish_a_superseded_candidate(newest_exits):
    # B waits on A's slow close while C overtakes B; even C exiting cannot revive B.
    gateway = Gateway[Packet, Packet]()
    a_socket, b_socket, c_socket = ClosingSocket(blocked=True), ClosingSocket(), ClosingSocket()
    a, b, c = (make_session(socket) for socket in (a_socket, b_socket, c_socket))
    first = await gateway.register("peer", a, replace=True)
    replacing = asyncio.create_task(gateway.register("peer", b, replace=True))
    try:
        await asyncio.wait_for(a_socket.closing.wait(), 1)
        assert gateway.get("peer") is None
        newest = await asyncio.wait_for(gateway.register("peer", c, replace=True), 1)
        assert gateway.get("peer") is c
        if newest_exits:
            await gateway.unregister(newest)
        a_socket.release_close.set()
        with pytest.raises(SessionClosed):
            await asyncio.wait_for(replacing, 1)
        await gateway.unregister(first)
        assert gateway.get("peer") is (None if newest_exits else c)
        if not newest_exits:
            await c.send(Packet(value="still-current"))
            assert Packet.model_validate_json(await c_socket.sent.get()).value == "still-current"
        await gateway.unregister(newest)
    finally:
        a_socket.release_close.set()
        await asyncio.gather(replacing, return_exceptions=True)
        await gateway.close()
    assert a_socket.codes == b_socket.codes == [1012]


async def test_unregister_keeps_predecessor_barrier_and_joins_despite_repeated_cancellation():
    gateway = Gateway[Packet, Packet]()
    old_socket = ClosingSocket(blocked=True)
    old, new = make_session(old_socket), make_session(ClosingSocket())
    registration = await gateway.register("peer", old, replace=False)
    retiring = asyncio.create_task(gateway.unregister(registration))
    replacement = None
    try:
        await asyncio.wait_for(old_socket.closing.wait(), 1)
        assert gateway.get("peer") is None
        with pytest.raises(RuntimeError):
            await gateway.register("peer", new, replace=False)
        entered = asyncio.Event()

        async def replace():
            entered.set()
            return await gateway.register("peer", new, replace=True)

        replacement = asyncio.create_task(replace())
        await entered.wait()
        assert gateway.get("peer") is None
        for _ in range(2):
            retiring.cancel()
            await asyncio.sleep(0)
            assert not retiring.done()
        old_socket.release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await retiring
        newest = await asyncio.wait_for(replacement, 1)
        assert gateway.get("peer") is new
        await gateway.unregister(registration)
        assert not new.closed
        await gateway.unregister(newest)
    finally:
        old_socket.release_close.set()
        await asyncio.gather(
            retiring, *([replacement] if replacement else []), return_exceptions=True
        )
        await gateway.close()
        await new.close()
    assert old_socket.codes == [1000]


@pytest.mark.parametrize("cancel", [False, True])
async def test_process_close_joins_hidden_sessions_and_collects_errors(cancel):
    gateway = Gateway[Packet, Packet]()
    a_socket = ClosingSocket(blocked=True)
    a, b = make_session(a_socket), make_session(ClosingSocket())
    broken_socket = ClosingSocket(error=ValueError("close failed"))
    broken = make_session(broken_socket)
    await gateway.register("peer", a, replace=True)
    await gateway.register("other", broken, replace=True)
    replacing = asyncio.create_task(gateway.register("peer", b, replace=True))
    await asyncio.wait_for(a_socket.closing.wait(), 1)
    closing = asyncio.create_task(gateway.close(1012))
    try:
        await asyncio.wait_for(broken_socket.closing.wait(), 1)
        assert gateway.get("peer") is gateway.get("other") is None
        assert not closing.done()  # A is hidden by B but still owned and must be joined.
        if cancel:
            for _ in range(2):
                closing.cancel()
                await asyncio.sleep(0)
                assert not closing.done()
        a_socket.release_close.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError) as caught:
                await closing
            assert isinstance(caught.value.__context__, ExceptionGroup)
        else:
            with pytest.raises(ExceptionGroup, match="gateway close failed") as errors:
                await closing
            assert [str(error) for error in errors.value.exceptions] == ["close failed"]
        with pytest.raises(SessionClosed):
            await replacing
        with pytest.raises(ExceptionGroup):
            await gateway.close()
        assert a.closed and b.closed and broken.closed
        assert broken_socket.codes == [1012]
    finally:
        a_socket.release_close.set()
        await asyncio.gather(replacing, closing, return_exceptions=True)


@pytest.mark.parametrize("failure", ["cancel", "close-error"])
async def test_failed_registration_closes_candidate_without_restoring_old_route(failure):
    gateway = Gateway[Packet, Packet]()
    old_socket = ClosingSocket(
        blocked=True, error=ValueError("old close failed") if failure == "close-error" else None
    )
    old, candidate = make_session(old_socket), make_session(ClosingSocket())
    await gateway.register("peer", old, replace=True)
    registering = asyncio.create_task(gateway.register("peer", candidate, replace=True))
    try:
        await asyncio.wait_for(old_socket.closing.wait(), 1)
        if failure == "cancel":
            registering.cancel()
        old_socket.release_close.set()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError):
            await registering
        assert candidate.closed
        assert gateway.get("peer") is None
        replacement = make_session(ClosingSocket())
        newest = await gateway.register("peer", replacement, replace=True)
        assert gateway.get("peer") is replacement
        await gateway.unregister(newest)
    finally:
        old_socket.release_close.set()
        await asyncio.gather(registering, gateway.close(), return_exceptions=True)
