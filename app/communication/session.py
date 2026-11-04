"""Single-reader, bidirectional communication for one peer and connection epoch.

Send registration before ``start()`` when the protocol requires it. One business
driver calls ``next_event()``; replies and bounded query handlers bypass its FIFO.
Always join ``close()`` before replacing a session. Closing owns only communication,
never the execution, durable receipts, or business acknowledgements carried by it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from app.contracts.protocol import Message, ToolCall, ToolResult
from app.contracts.validation import PayloadRejected, RejectedMessage

ReplyKey = tuple[str, str]
type Ready = Callable[[], Awaitable[None]]
_MAX_WATCHES = 4
_MAX_PENDING = 4
_MAX_HANDLERS = 4
_MAX_BUSY = 4
_MAX_MESSAGES = 32
_MAX_ITEMS = 32
_MAX_ACKS = 32
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
class Responder[Outgoing: Message]:
    """One query classifier and its business-owned success/busy response builders."""

    matches: Callable[[ToolCall | ToolResult], bool]
    handle: Callable[[ToolCall | ToolResult, str], Awaitable[Outgoing]]
    busy: Callable[[ToolCall | ToolResult, str], Outgoing]


class Session[Incoming: Message, Outgoing: Message]:
    def __init__(
        self,
        transport: Transport,
        *,
        parse: Callable[[str], Incoming],
        make_ack: Callable[[str], Outgoing],
        reply_key: Callable[[ToolCall | ToolResult, str], ReplyKey | None] | None = None,
        responder: Responder[Outgoing] | None = None,
        epoch: str | None = None,
    ) -> None:
        self._transport = transport
        self._parse = parse
        self._make_ack = make_ack
        self._reply_key = reply_key
        self._responder = responder
        self._epoch: str | None = None
        if epoch is not None:
            self.bind_epoch(epoch)
        self._reader: asyncio.Task[None] | None = None
        self._inbox: deque[tuple[Incoming | Ready, int, int]] = deque()
        self._reject: Callable[[RejectedMessage], Ready] | None = None
        self._inbox_bytes = 0
        self._inbox_items = 0
        self._ready = asyncio.Event()
        self._receiving = False
        self._watches: dict[str, asyncio.Task[Ready]] = {}
        self._done: deque[tuple[str, asyncio.Task[Ready]]] = deque()
        self._woken = False
        self._last_was_message = False
        self._handling = False
        self._periodic: asyncio.Task[None] | None = None
        self._fault: Exception | None = None
        self._driver: asyncio.Task[object] | None = None
        self._scope: asyncio.Timeout | None = None
        self._read_error: Exception | None = None
        self._pending: dict[ReplyKey, asyncio.Future[tuple[ToolCall | ToolResult, Incoming]]] = {}
        self._request_sends: set[asyncio.Task[bool]] = set()
        self._send_lock = asyncio.Lock()
        self._write: asyncio.Task[bool] | None = None
        self._write_error: SessionClosed | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._busy: deque[Outgoing | Ready] = deque()
        self._busy_sender: asyncio.Task[None] | None = None
        self._acks: deque[str] = deque()
        self._ack_ready = asyncio.Event()
        self._ack_sender: asyncio.Task[None] | None = None
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
        self._ack_sender = asyncio.create_task(self._send_acks())

    def register_control(self, message: Incoming) -> None:
        """Record receipt only; the authenticated first Register calls this before start()."""
        if message.control not in {"register", "registered", "heartbeat"}:
            return
        if len(self._acks) >= _MAX_ACKS:
            raise SessionOverflow("control acknowledgement buffer exceeded")
        self._acks.append(message.message_id)
        self._ack_ready.set()

    @asynccontextmanager
    async def driving(self) -> AsyncIterator[None]:
        """Fence a single driver; its deadline also covers awaited business callbacks."""
        if self._driver is not None or self._handling:
            raise RuntimeError("nested or concurrent session driver")
        self._driver = asyncio.current_task()
        try:
            async with asyncio.timeout(None) as scope:
                self._scope = scope
                yield
        finally:
            self._scope = None
            self._driver = None

    @contextmanager
    def handling(self) -> Iterator[None]:
        """Prevent a callback from waiting on a response queued behind itself."""
        if self._handling:
            raise RuntimeError("nested session handling")
        self._handling = True
        try:
            yield
        finally:
            self._handling = False

    def set_deadline(self, at: float | None) -> None:
        if self._scope is None or asyncio.current_task() is not self._driver:
            raise RuntimeError("only the session driver can set its deadline")
        self._scope.reschedule(at)

    def wake(self) -> None:
        self._woken = True
        self._ready.set()

    def reject_with(self, callback: Callable[[RejectedMessage], Ready] | None) -> None:
        """Install owner validation and waiter completion, returning an async response.

        Independent queries authorize synchronously before the next read can fail their
        waiters. Other messages keep their arrival position in the business FIFO.
        """
        if self._driver is not None or self._reject is not None:
            raise RuntimeError("rejection callback already configured")
        self._reject = callback

    def reject_queries(self, message: RejectedMessage) -> None:
        """Fail malformed replies only after the owner authorizes their headers and epoch."""
        assert message.connection_epoch is not None
        selected: set[asyncio.Future[tuple[ToolCall | ToolResult, Incoming]]] = set()
        for item in message.payload:
            key = self._reply_key(item, message.connection_epoch) if self._reply_key else None
            if key is not None:
                future = self._pending.get(key)
                if future is None or future.done():
                    raise ValueError("invalid reply has no pending query")
                selected.add(future)
        for future in selected:
            future.set_exception(PayloadRejected(message))

    def watch[T](
        self,
        name: str,
        source: Callable[[], Awaitable[T]],
        callback: Callable[[T], Awaitable[None]],
    ) -> None:
        """Observe once without owning the source's execution or running business in a task."""
        if self._closed:
            raise SessionClosed("session closed")
        if name in self._watches:
            raise RuntimeError("observer already registered")
        if len(self._watches) >= _MAX_WATCHES:
            raise SessionOverflow("observer limit exceeded")

        async def observe() -> Ready:
            value = await source()

            async def deliver() -> None:
                await callback(value)

            return deliver

        def completed(task: asyncio.Task[Ready]) -> None:
            if not task.cancelled():
                task.exception()
            if self._watches.get(name) is task:
                self._done.append((name, task))
                self._ready.set()

        task = asyncio.create_task(observe())
        self._watches[name] = task
        task.add_done_callback(completed)

    async def unwatch(self, *names: str) -> None:
        """Invalidate before joining; no stale completion survives cancellation or rearming."""
        tasks = [task for name in names if (task := self._watches.pop(name, None)) is not None]
        self._done = deque((name, task) for name, task in self._done if name not in names)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def periodic(self, interval: float, build: Callable[[], Outgoing | None]) -> None:
        """Send communication heartbeats independently of slow business handlers."""
        if self._periodic is not None or interval <= 0:
            raise ValueError("periodic sender already configured or invalid interval")

        async def run() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    message = build()
                    if message is not None:
                        await self.send(message)
            except Exception as error:  # noqa: BLE001 — relay the original fault to the driver.
                self._fault = error
                self._ready.set()

        self._periodic = asyncio.create_task(run())

    async def next_event(self, *, until: float | None = None) -> Incoming | Ready | None:
        """Deliver FIFO messages fairly with local readiness, or wake at an absolute deadline.

        Reader failure drains only buffered network inputs. Execution observers stay with
        their owner for the next connection; they cannot delay reporting the failed reader.
        """
        if self._handling:
            raise RuntimeError("nested session consumption")
        if self._reader is None:
            raise RuntimeError("session reader not started")
        if self._receiving or (
            self._driver is not None and asyncio.current_task() is not self._driver
        ):
            raise RuntimeError("concurrent session consumers")
        self._receiving = True
        try:
            while True:
                if self._closed:
                    raise self._write_error or SessionClosed("session closed")
                if self._fault is not None:
                    raise self._fault
                if self._read_error is not None and not self._inbox:
                    raise self._read_error
                if self._read_error is None:
                    if until is not None and asyncio.get_running_loop().time() >= until:
                        return None
                    if self._done and (not self._inbox or self._last_was_message):
                        name, task = self._done.popleft()
                        if self._watches.get(name) is not task:
                            continue
                        del self._watches[name]
                        self._last_was_message = False
                        return task.result()
                if self._inbox:
                    message, size, count = self._inbox.popleft()
                    self._inbox_bytes -= size
                    self._inbox_items -= count
                    self._last_was_message = True
                    return message
                if self._woken:
                    self._woken = False
                    return None
                self._ready.clear()
                try:
                    async with asyncio.timeout_at(until):
                        await self._ready.wait()
                except TimeoutError:
                    return None
        finally:
            self._receiving = False

    async def send(
        self, message: Outgoing, *, is_current: Callable[[], bool] | None = None
    ) -> bool:
        """Return only after the actual write, or False if its last-moment guard declined.

        A cancelled lock waiter is harmless. A cancelled/failed write has an unknown
        delivery outcome, so it seals the connection rather than allowing more writes.
        """
        async with self._send_lock:
            if self._closed:
                raise self._write_error or SessionClosed("session closed")
            write = asyncio.create_task(self._write_message(message, is_current))
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

    async def _write_message(
        self, message: Outgoing, is_current: Callable[[], bool] | None
    ) -> bool:
        if self._closed:
            raise SessionClosed("session closed")
        # No await between this check and transport entry: covers lock AND task scheduling.
        if is_current is not None and not is_current():
            return False
        ack_id = None
        if self._acks and message.control != "register":
            ack_id = self._acks[0]
            message = message.model_copy(update={"ack": True, "ack_id": ack_id})
        await self._transport.send_text(message.model_dump_json())
        if ack_id is not None and not self._closed:
            self._acks.popleft()
            if not self._acks:
                self._ack_ready.clear()
        return True

    async def request(
        self, message: Outgoing, *, call: ToolCall, key: ReplyKey, timeout: float
    ) -> tuple[ToolCall | ToolResult, Incoming]:
        """Return the selected reply and its original frame within one total time budget.

        The business owner can validate the actual header without counting unrelated items.
        Correlation still consumes only the selected item, not its containing frame.
        """
        if self._closed or self._read_error is not None:
            raise SessionClosed("session unavailable") from self._read_error
        if self._reader is None or self._reply_key is None or self._epoch is None:
            raise RuntimeError("requests require a started, epoch-bound reply reader")
        if (
            key != (call.message_id, self._epoch)
            or call not in message.payload
            or message.connection_epoch != self._epoch
            or key in self._pending
        ):
            raise ValueError("request key is stale or already pending")
        if len(self._pending) >= _MAX_PENDING:
            raise SessionBusy("session request limit exceeded")
        future: asyncio.Future[tuple[ToolCall | ToolResult, Incoming]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[key] = future
        sending = asyncio.create_task(self.send(message))
        self._request_sends.add(sending)
        try:
            async with asyncio.timeout(timeout):
                # A failed reader must unblock a query even while send is waiting for the lock.
                waiting: set[
                    asyncio.Task[bool] | asyncio.Future[tuple[ToolCall | ToolResult, Incoming]]
                ] = {
                    sending,
                    future,
                }
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
                try:
                    message = self._parse(raw)
                except PayloadRejected as rejection:
                    self._queue_rejection(rejection.message, len(raw.encode()))
                    continue
                # Validate capacity for the entire frame before ACK registration or bypass effects.
                size = len(raw.encode())
                queries = (
                    sum(self._responder.matches(item) for item in message.payload)
                    if self._responder is not None
                    else 0
                )
                busy_needed = max(0, queries - (_MAX_HANDLERS - len(self._handlers)))
                if (
                    size + self._inbox_bytes > _MAX_BYTES
                    or len(self._inbox) >= _MAX_MESSAGES
                    or self._inbox_items + len(message.payload) > _MAX_ITEMS
                    or len(self._busy) + busy_needed > _MAX_BUSY
                ):
                    raise SessionOverflow("execution message buffer exceeded")
                self.register_control(message)
                if message.control is None and not message.payload:
                    continue
                if message.control is None:
                    assert message.connection_epoch is not None
                    remaining = [
                        item for item in message.payload if not self.dispatch_item(item, message)
                    ]
                    if not remaining:
                        continue
                    message = message.model_copy(update={"payload": remaining})
                self._inbox.append((message, size, len(message.payload)))
                self._inbox_bytes += size
                self._inbox_items += len(message.payload)
                self._ready.set()
        except Exception as exc:  # noqa: BLE001 — next_event re-raises after the FIFO drains.
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

    def _queue_rejection(self, message: RejectedMessage, size: int) -> None:
        count = len(message.payload)
        if (
            size + self._inbox_bytes > _MAX_BYTES
            or len(self._inbox) >= _MAX_MESSAGES
            or self._inbox_items + count > _MAX_ITEMS
        ):
            raise SessionOverflow("execution message buffer exceeded")
        if self._reject is None:
            raise PayloadRejected(message)
        prepare = self._reject

        async def reject() -> None:
            await prepare(message)()

        epoch = message.connection_epoch
        independent = (
            message.control is None
            and epoch is not None
            and all(
                (self._responder is not None and self._responder.matches(item))
                or (self._reply_key is not None and self._reply_key(item, epoch) is not None)
                for item in message.payload
            )
        )
        if independent:
            if len(self._handlers) >= _MAX_HANDLERS and len(self._busy) >= _MAX_BUSY:
                raise SessionOverflow("query busy response buffer exceeded")
            respond = prepare(message)

            async def run() -> None:
                try:
                    await respond()
                except Exception as error:  # noqa: BLE001 — relay rejection faults to the driver.
                    self._fault = error
                    self._ready.set()

            if len(self._handlers) < _MAX_HANDLERS:
                task = asyncio.create_task(run())
                self._handlers.add(task)
                task.add_done_callback(self._handlers.discard)
            else:
                self._busy.append(run)
                if self._busy_sender is None or self._busy_sender.done():
                    self._busy_sender = asyncio.create_task(self._send_busy())
            return
        # Network rejection belongs at its arrival position, including EOF drain.
        self._inbox.append((reject, size, count))
        self._inbox_bytes += size
        self._inbox_items += count
        self._ready.set()

    def dispatch_item(self, item: ToolCall | ToolResult, frame: Incoming) -> bool:
        """Consume one temporary reply/query, never the rest of its containing frame.

        The reader invokes this only without new control. A control owner invokes it after
        authorizing control, so a mixed Registered/history cannot bypass its handshake.
        """
        epoch = frame.connection_epoch
        assert epoch is not None
        key = self._reply_key(item, epoch) if self._reply_key is not None else None
        if key is not None:
            future = self._pending.get(key)
            if future is not None and not future.done():
                future.set_result((item, frame))
            return True
        if self._responder is None or not self._responder.matches(item):
            return False
        if len(self._handlers) < _MAX_HANDLERS:
            task = asyncio.create_task(self._respond(item, epoch))
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)
        else:
            if len(self._busy) >= _MAX_BUSY:
                raise SessionOverflow("query busy response buffer exceeded")
            self._busy.append(self._responder.busy(item, epoch))
            if self._busy_sender is None or self._busy_sender.done():
                self._busy_sender = asyncio.create_task(self._send_busy())
        return True

    async def _respond(self, item: ToolCall | ToolResult, epoch: str) -> None:
        assert self._responder is not None
        try:
            await self.send(await self._responder.handle(item, epoch))
        except Exception:
            logger.warning("session query response failed", exc_info=True)

    async def _send_busy(self) -> None:
        try:
            while self._busy:
                item = self._busy[0]
                if callable(item):
                    await item()
                else:
                    await self.send(item)
                self._busy.popleft()
        except SessionClosed:
            pass  # send has already sealed the session and failed its pending requests.

    async def _send_acks(self) -> None:
        try:
            while True:
                await self._ack_ready.wait()
                if self._acks:
                    # Another queued writer may piggyback this ACK before our lock is acquired.
                    await self.send(
                        self._make_ack(self._acks[0]), is_current=lambda: bool(self._acks)
                    )
        except SessionClosed:
            pass  # A failed write already seals communication; receipt ACKs never retry.

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
            self._inbox_items = 0
            self._busy.clear()
            self._acks.clear()
            self._ready.set()
            tasks: list[asyncio.Task[None] | asyncio.Task[bool] | asyncio.Task[Ready]] = [
                *self._handlers,
                *self._request_sends,
                *self._watches.values(),
            ]
            self._watches.clear()
            self._done.clear()
            tasks.extend(
                t
                for t in (
                    self._reader,
                    self._busy_sender,
                    self._ack_sender,
                    self._write,
                    self._periodic,
                )
                if t is not None
            )
            # A send failure may initiate close inside one of these very tasks.
            tasks = [task for task in tasks if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            self._close_task = asyncio.create_task(self._finish_close(code, tasks))
        return self._close_task

    async def _finish_close(
        self,
        code: int,
        tasks: list[asyncio.Task[None] | asyncio.Task[bool] | asyncio.Task[Ready]],
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
