import asyncio

import pytest

from app.communication.gateway import Gateway
from app.communication.session import SessionClosed
from tests.test_communication_session import Packet, Socket, make_session


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


async def test_superseded_candidate_stays_rejected_after_newest_connection_exits():
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
        await gateway.unregister(newest)
        a_socket.release_close.set()
        with pytest.raises(SessionClosed):
            await asyncio.wait_for(replacing, 1)
        await gateway.unregister(first)
        assert gateway.get("peer") is None
    finally:
        a_socket.release_close.set()
        await asyncio.gather(replacing, return_exceptions=True)
        await gateway.close()
    assert a_socket.codes == b_socket.codes == [1012]


async def test_unregister_keeps_predecessor_barrier_until_close_joins():
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
        old_socket.release_close.set()
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


async def test_process_close_joins_hidden_sessions_despite_cancellation_and_errors():
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
        # Let the other closes finish: checking done() at socket entry can miss an early return.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), 0.02)
        for _ in range(2):
            closing.cancel()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(closing), 0.02)
        a_socket.release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        with pytest.raises(SessionClosed):
            await replacing
        with pytest.raises(ExceptionGroup, match="gateway close failed") as errors:
            await gateway.close()
        assert [str(error) for error in errors.value.exceptions] == ["close failed"]
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
