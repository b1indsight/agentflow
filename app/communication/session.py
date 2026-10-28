"""Single-reader, bidirectional communication for one peer and connection epoch.

Send registration before ``start()`` when the protocol requires it. One business
consumer calls ``receive()``; replies and bounded query handlers bypass its FIFO.
Always join ``close()`` before replacing a session. Closing owns only communication,
never the execution, durable receipts, or business acknowledgements carried by it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.contracts.protocol import Message

ReplyKey = tuple[str, str]
_MAX_PENDING = 4
_MAX_HANDLERS = 4
_MAX_BUSY = 4
_MAX_MESSAGES = 32
_MAX_BYTES = 16 * 1024 * 1024
logger = logging.getLogger(__name__)


class Transport(Protocol):
    async def send_text(self, data: str) -> None: ...

    async def receive_text(self) -> str: ...

    async def close(self, code: int = 1000) -> None: ...


class SessionClosed(ConnectionError):
    """The connection cannot serve this operation; retry belongs to its caller."""


class SessionBusy(RuntimeError):
    """All connection-local temporary request slots are occupied."""


class SessionOverflow(RuntimeError):
    """A peer exceeded bounded inbound buffering; the connection must end."""


@dataclass(frozen=True)
class Responder[Incoming: Message, Outgoing: Message]:
    """One query classifier and its business-owned success/busy response builders."""

    matches: Callable[[Incoming], bool]
    handle: Callable[[Incoming], Awaitable[Outgoing]]
    busy: Callable[[Incoming], Outgoing]


class Session[Incoming: Message, Outgoing: Message]:
    def __init__(
        self,
        transport: Transport,
        *,
        parse: Callable[[str], Incoming],
        reply_key: Callable[[Incoming], ReplyKey | None] | None = None,
        responder: Responder[Incoming, Outgoing] | None = None,
        epoch: str | None = None,
    ) -> None:
        self._transport = transport
        self._parse = parse
        self._reply_key = reply_key
        self._responder = responder
        self._epoch: str | None = None
        if epoch is not None:
            self.bind_epoch(epoch)
        self._reader: asyncio.Task[None] | None = None
        self._inbox: deque[tuple[Incoming, int]] = deque()
        self._inbox_bytes = 0
        self._ready = asyncio.Event()
        self._receiving = False
        self._read_error: Exception | None = None
        self._pending: dict[ReplyKey, asyncio.Future[Incoming]] = {}
        self._request_sends: set[asyncio.Task[bool]] = set()
        self._send_lock = asyncio.Lock()
        self._write: asyncio.Task[bool] | None = None
        self._write_error: SessionClosed | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._busy: deque[Outgoing] = deque()
        self._busy_sender: asyncio.Task[None] | None = None
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def epoch(self) -> str | None:
        return self._epoch

    @property
    def closed(self) -> bool:
        return self._closed

    def bind_epoch(self, epoch: str) -> None:
        """Confirm once from an authorized protocol message, not an incidental query."""
        if not epoch or (self._epoch is not None and self._epoch != epoch):
            raise ValueError("connection epoch cannot change")
        self._epoch = epoch

    def start(self) -> None:
        if self._closed:
            raise SessionClosed("session closed")
        if self._reader is not None:
            raise RuntimeError("session reader already started")
        self._reader = asyncio.create_task(self._read())

    async def receive(self) -> Incoming:
        """Drain parsed business messages in order; cancellation leaves the reader alone."""
        if self._reader is None:
            raise RuntimeError("session reader not started")
        if self._receiving:
            raise RuntimeError("concurrent session consumers")
        self._receiving = True
        try:
            while True:
                if self._closed:
                    raise SessionClosed("session closed")
                if self._inbox:
                    message, size = self._inbox.popleft()
                    self._inbox_bytes -= size
                    return message
                if self._read_error is not None:
                    raise self._read_error
                self._ready.clear()
                await self._ready.wait()
        finally:
            self._receiving = False

    async def send(
        self, message: Outgoing, *, is_current: Callable[[], bool] | None = None
    ) -> bool:
        """Return only after the actual write, or False if its last-moment guard declined.

        A cancelled lock waiter is harmless. A cancelled/failed write has an unknown
        delivery outcome, so it seals the connection rather than allowing more writes.
        """
        raw = message.model_dump_json()
        async with self._send_lock:
            if self._closed:
                raise self._write_error or SessionClosed("session closed")
            write = asyncio.create_task(self._write_message(raw, is_current))
            self._write = write
            try:
                return await asyncio.shield(write)
            except asyncio.CancelledError:
                caller = asyncio.current_task()
                cancelled = caller is not None and caller.cancelling()
                already_closed = self._closed
                # Cancellation can race the shield's completion callback after a
                # successful transport return; that write no longer has an unknown outcome.
                if not write.done() or write.cancelled() or write.exception() is not None:
                    self._begin_close(1011)
                if cancelled or not already_closed:
                    raise
                raise SessionClosed("session closed during write") from None
            except Exception as exc:
                error = SessionClosed(f"session write failed: {exc}")
                error.__cause__ = exc
                self._write_error = error
                self._begin_close(1011)
                raise error from exc
            finally:
                self._write = None

    async def _write_message(self, raw: str, is_current: Callable[[], bool] | None) -> bool:
        if self._closed:
            raise SessionClosed("session closed")
        # No await between this check and transport entry: covers lock AND task scheduling.
        if is_current is not None and not is_current():
            return False
        await self._transport.send_text(raw)
        return True

    async def request(self, message: Outgoing, *, key: ReplyKey, timeout: float) -> Incoming:
        """Correlate a fresh temporary key within a total lock/write/reply time budget."""
        if self._closed or self._read_error is not None:
            raise SessionClosed("session unavailable") from self._read_error
        if self._reader is None or self._reply_key is None or self._epoch is None:
            raise RuntimeError("requests require a started, epoch-bound reply reader")
        if key[1] != self._epoch or key in self._pending:
            raise ValueError("request key is stale or already pending")
        if len(self._pending) >= _MAX_PENDING:
            raise SessionBusy("session request limit exceeded")
        future: asyncio.Future[Incoming] = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        sending = asyncio.create_task(self.send(message))
        self._request_sends.add(sending)
        try:
            async with asyncio.timeout(timeout):
                # A failed reader must unblock a query even while send is waiting for the lock.
                waiting: set[asyncio.Task[bool] | asyncio.Future[Incoming]] = {sending, future}
                await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if future.done():
                    response = future.result()
                    await sending  # An early reply is not proof our write completed.
                    return response
                await sending
                return await future
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                raise
            raise SessionClosed("session closed during request") from None
        finally:
            try:
                sending.cancel()
                await asyncio.gather(sending, return_exceptions=True)
            finally:
                self._request_sends.discard(sending)
                if self._pending.get(key) is future:
                    del self._pending[key]
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()

    async def _read(self) -> None:
        try:
            while True:
                raw = await self._transport.receive_text()
                message = self._parse(raw)
                key = self._reply_key(message) if self._reply_key is not None else None
                if key is not None:
                    future = self._pending.get(key)
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                if self._responder is not None and self._responder.matches(message):
                    if len(self._handlers) < _MAX_HANDLERS:
                        task = asyncio.create_task(self._respond(message))
                        self._handlers.add(task)
                        task.add_done_callback(self._handlers.discard)
                    else:
                        if len(self._busy) >= _MAX_BUSY:
                            raise SessionOverflow("query busy response buffer exceeded")
                        self._busy.append(self._responder.busy(message))
                        if self._busy_sender is None or self._busy_sender.done():
                            self._busy_sender = asyncio.create_task(self._send_busy())
                    continue
                size = len(raw.encode())
                if len(self._inbox) >= _MAX_MESSAGES or self._inbox_bytes + size > _MAX_BYTES:
                    raise SessionOverflow("execution message buffer exceeded")
                self._inbox.append((message, size))
                self._inbox_bytes += size
                self._ready.set()
        except Exception as exc:  # noqa: BLE001 — receive re-raises after buffered messages drain.
            self._read_error = exc
            error = SessionClosed("session reader failed")
            error.__cause__ = exc
            self._fail_pending(error)
            # An early reply may already have resolved its Future while write is
            # still blocked. That observer must fail now too, not at its deadline.
            for sending in self._request_sends:
                sending.cancel()
        finally:
            self._ready.set()

    async def _respond(self, message: Incoming) -> None:
        assert self._responder is not None
        try:
            await self.send(await self._responder.handle(message))
        except Exception:
            logger.warning("session query response failed", exc_info=True)

    async def _send_busy(self) -> None:
        try:
            while self._busy:
                await self.send(self._busy[0])
                self._busy.popleft()
        except SessionClosed:
            pass  # send has already sealed the session and failed its pending requests.

    def _fail_pending(self, error: SessionClosed) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def _begin_close(self, code: int) -> asyncio.Task[None]:
        if self._close_task is None:
            self._closed = True
            self._fail_pending(self._write_error or SessionClosed("session closed"))
            self._inbox.clear()
            self._inbox_bytes = 0
            self._busy.clear()
            self._ready.set()
            tasks: list[asyncio.Task[None] | asyncio.Task[bool]] = [
                *self._handlers,
                *self._request_sends,
            ]
            tasks.extend(t for t in (self._reader, self._busy_sender, self._write) if t is not None)
            # A send failure may initiate close inside one of these very tasks.
            tasks = [task for task in tasks if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            self._close_task = asyncio.create_task(self._finish_close(code, tasks))
        return self._close_task

    async def _finish_close(
        self,
        code: int,
        tasks: list[asyncio.Task[None] | asyncio.Task[bool]],
    ) -> None:
        try:
            await self._transport.close(code)
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self, code: int = 1000) -> None:
        """Seal immediately, then finish joining communication even if the caller is cancelled."""
        task = self._begin_close(code)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        try:
            task.result()
        finally:
            if cancelled:
                raise asyncio.CancelledError
