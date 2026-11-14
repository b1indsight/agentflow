"""Single-reader, bidirectional communication for one peer and connection epoch.

Send registration before ``start()`` when the protocol requires it. One business
driver calls ``next_event()``; all payloads retain their place in its bounded FIFO.
Always join ``close()`` before replacing a session. Closing owns only communication,
never the execution, durable receipts, or business acknowledgements carried by it.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Protocol

from app.contracts.protocol import Message
from app.contracts.validation import PayloadRejected, RejectedMessage

type Ready = Callable[[], Awaitable[None]]
_MAX_WATCHES = 4
_MAX_MESSAGES = 32
_MAX_ITEMS = 32
_MAX_ACKS = 32
_MAX_BYTES = 16 * 1024 * 1024


class Transport(Protocol):
    async def send_text(self, data: str) -> None: ...

    async def receive_text(self) -> str: ...

    async def close(self, code: int = 1000) -> None: ...


class SessionClosed(ConnectionError):
    """The connection cannot serve this operation; retry belongs to its caller."""


class SessionOverflow(RuntimeError):
    """A peer exceeded bounded inbound buffering; the connection must end."""


class Session[Incoming: Message, Outgoing: Message]:
    def __init__(
        self,
        transport: Transport,
        *,
        parse: Callable[[str], Incoming],
        make_ack: Callable[[str], Outgoing],
        epoch: str | None = None,
    ) -> None:
        self._transport = transport
        self._parse = parse
        self._make_ack = make_ack
        self._epoch: str | None = None
        if epoch is not None:
            self.bind_epoch(epoch)
        self._reader: asyncio.Task[None] | None = None
        self._inbox: deque[tuple[Incoming | Ready, int, int]] = deque()
        self._reject: Callable[[RejectedMessage], Ready] | None = None
        self._ready = asyncio.Event()
        self._space_available = asyncio.Event()
        self._receiving = False
        self._watches: dict[str, asyncio.Task[Ready]] = {}
        self._retiring_watches: set[asyncio.Task[Ready]] = set()
        self._done: deque[tuple[str, asyncio.Task[Ready]]] = deque()
        self._woken = False
        self._last_was_message = False
        self._handling = False
        self._periodic: asyncio.Task[None] | None = None
        self._fault: Exception | None = None
        self._driver: asyncio.Task[object] | None = None
        self._scope: asyncio.Timeout | None = None
        self._read_error: Exception | None = None
        self._send_lock = asyncio.Lock()
        self._write: asyncio.Task[bool] | None = None
        self._write_error: SessionClosed | None = None
        self._acks: deque[str] = deque()
        self._ack_ready = asyncio.Event()
        self._ack_sender: asyncio.Task[None] | None = None
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._on_close: Callable[[], None] | None = None

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
        """Install owner validation; every rejection retains its network FIFO position."""
        if self._driver is not None or self._reject is not None:
            raise RuntimeError("rejection callback already configured")
        self._reject = callback

    def close_with(self, callback: Callable[[], None]) -> None:
        """Notify connection-local receivers immediately, even while the driver is busy."""
        if self._on_close is not None or self._driver is not None or self._closed:
            raise RuntimeError("close callback cannot be configured")
        self._on_close = callback

    def check_request(self) -> None:
        """Admit an external waiter without letting the serial driver wait on itself."""
        if asyncio.current_task() is self._driver:
            raise RuntimeError("session driver cannot wait for a queued reply")
        if (
            self._closed
            or self._read_error is not None
            or self._reader is None
            or self._epoch is None
        ):
            raise SessionClosed("session unavailable")

    def watch[T](
        self,
        name: str,
        source: Callable[[], Awaitable[T]],
        callback: Callable[[T], Awaitable[None]],
    ) -> None:
        """Await a source and queue its completion; only the serial driver runs the callback."""
        if self._closed:
            raise SessionClosed("session closed")
        if name in self._watches:
            raise RuntimeError("observer already registered")
        if len(self._watches) + len(self._retiring_watches) >= _MAX_WATCHES:
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
        self._retiring_watches.update(tasks)
        for task in tasks:
            task.add_done_callback(self._retiring_watches.discard)
            task.cancel()
        # A cancelled waiter cannot interrupt cleanup or hide it from a concurrent close.
        await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))

    def periodic(self, interval: float, build: Callable[[], Outgoing | list[Outgoing] | None]) -> None:
        """Send communication heartbeats independently of slow business handlers."""
        if self._periodic is not None or interval <= 0:
            raise ValueError("periodic sender already configured or invalid interval")

        async def run() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    message = build()
                    if isinstance(message, list):
                        for item in message:
                            await self.send(item)
                    elif message is not None:
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
                    message, _, _ = self._inbox.popleft()
                    self._space_available.set()
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

    async def _read(self) -> None:
        try:
            while True:
                raw = await self._transport.receive_text()
                try:
                    message = self._parse(raw)
                except PayloadRejected as rejection:
                    await self._wait_for_capacity(len(raw.encode()), len(rejection.message.payload))
                    self._queue_rejection(rejection.message, len(raw.encode()))
                    continue
                # Validate the entire message before recording a receipt or delivering payloads.
                size = len(raw.encode())
                await self._wait_for_capacity(size, len(message.payload))
                self.register_control(message)
                if message.control is None and not message.payload:
                    continue
                self._inbox.append((message, size, len(message.payload)))
                self._ready.set()
        except Exception as exc:  # noqa: BLE001 — next_event re-raises after the FIFO drains.
            self._read_error = exc
        finally:
            self._ready.set()

    async def _wait_for_capacity(self, size: int, count: int) -> None:
        """Bound memory while applying transport backpressure to valid multi-job bursts.

        A single oversized message still fails. A full FIFO pauses the reader, so the
        queue limit does not become a limit on concurrent jobs or registration pages.
        """
        if size > _MAX_BYTES or count > _MAX_ITEMS:
            raise SessionOverflow("execution message buffer exceeded")
        while (
            size + sum(buffered_size for _, buffered_size, _ in self._inbox) > _MAX_BYTES
            or len(self._inbox) >= _MAX_MESSAGES
            or sum(items for _, _, items in self._inbox) + count > _MAX_ITEMS
        ):
            self._space_available.clear()
            await self._space_available.wait()

    def _queue_rejection(self, message: RejectedMessage, size: int) -> None:
        count = len(message.payload)
        if self._reject is None:
            raise PayloadRejected(message)
        prepare = self._reject

        async def reject() -> None:
            await prepare(message)()

        # Network rejection belongs at its arrival position, including EOF drain.
        self._inbox.append((reject, size, count))
        self._ready.set()

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

    def _begin_close(self, code: int) -> asyncio.Task[None]:
        if self._close_task is None:
            self._closed = True
            self._inbox.clear()
            self._acks.clear()
            self._ready.set()
            tasks: list[asyncio.Task[None] | asyncio.Task[bool] | asyncio.Task[Ready]] = [
                *self._watches.values(),
            ]
            self._watches.clear()
            self._done.clear()
            tasks.extend(
                t
                for t in (
                    self._reader,
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
            # These observers are already cancelled; a second cancel would abort cleanup.
            tasks.extend(self._retiring_watches)
            self._close_task = asyncio.create_task(self._finish_close(code, tasks))
            if self._on_close is not None:
                self._on_close()
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
