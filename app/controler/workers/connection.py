"""Single-reader worker connection with immediate conversation-response dispatch."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from typing import Protocol

from fastapi import WebSocketDisconnect
from loguru import logger
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from app.contracts.protocol import parse_worker_message
from app.contracts.updates import ReleaseAvailable, WorkerMaintenance
from app.controler.workers.conversation import ConversationBroker
from app.persistence.worker_updates import WorkerUpdateStore


class WorkerSocket(Protocol):
    @property
    def headers(self) -> Mapping[str, str]: ...

    async def accept(self) -> None: ...

    async def close(self, code: int = 1000) -> None: ...

    async def send_text(self, data: str) -> None: ...

    async def receive_text(self) -> str: ...


class ProtocolViolation(RuntimeError):
    pass


class WorkerConnection:
    """Keep reads flowing during coordinator I/O, preserving execution-message order.

    Only the receiver task reads the transport. Execution messages use a bounded
    in-memory buffer; overflow fails the connection rather than blocking history
    responses or allocating unbounded memory. Nothing is persisted or replayed on
    a replacement connection.
    """

    def __init__(self, socket: WorkerSocket, conversations: ConversationBroker, worker_id: str, epoch: str,
                 updates: WorkerUpdateStore | None = None) -> None:
        self.socket = socket
        self.conversations = conversations
        self.updates = updates
        self.worker_id, self.epoch = worker_id, epoch
        self._send_lock = asyncio.Lock()
        self._release_task: asyncio.Task[None] | None = None
        self._messages: deque[tuple[str, int]] = deque()
        self._bytes = 0
        self._ready = asyncio.Event()
        self._error: Exception | None = None
        self.conversations.connect(worker_id, epoch, self)
        self._reader = asyncio.create_task(self._receive())

    async def send_text(self, data: str) -> None:
        async with self._send_lock:
            await self.socket.send_text(data)

    def start_updates(self) -> None:
        """Only a registered opt-in peer receives extension messages."""
        if self.updates is not None:
            self._release_task = asyncio.create_task(self._notify_releases())

    async def _notify_releases(self) -> None:
        assert self.updates is not None
        previous = None
        while True:
            try:
                release = self.updates.latest()
                if release is not None and release != previous:
                    await self.send_text(ReleaseAvailable(release=release).model_dump_json())
                    previous = release
            except (DatabaseError, PoolTimeout):
                logger.warning("worker release notification delayed: database unavailable")
            await asyncio.sleep(5)

    async def close(self, code: int = 1000) -> None:
        try:
            await self.socket.close(code)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Join the receiver when the gateway exits or a new connection takes over."""
        self._reader.cancel()
        tasks = [self._reader]
        if self._release_task is not None:
            self._release_task.cancel()
            tasks.append(self._release_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        self.conversations.disconnect(self)
        self._ready.set()

    async def receive_text(self) -> str:
        while not self._messages:
            if self._reader.done():
                if self._error is not None:
                    raise self._error
                raise WebSocketDisconnect(code=1001)
            self._ready.clear()
            await self._ready.wait()
        raw, size = self._messages.popleft()
        self._bytes -= size
        return raw

    async def _receive(self) -> None:
        try:
            while True:
                raw = await self.socket.receive_text()
                message = parse_worker_message(raw)
                if self.conversations.accept(self, message):
                    continue
                if isinstance(message, WorkerMaintenance):
                    if self.updates is None:
                        raise ProtocolViolation("worker did not negotiate updates")
                    reply = self.updates.handle(self.worker_id, self.epoch, message)
                    await self.send_text(reply.model_dump_json())
                    continue
                size = len(raw.encode())
                if len(self._messages) >= 32 or self._bytes + size > 16 * 1024 * 1024:
                    raise ProtocolViolation("worker execution message buffer exceeded")
                self._messages.append((raw, size))
                self._bytes += size
                self._ready.set()
        except Exception as exc:  # noqa: BLE001 -- re-raised by the gateway's receive boundary
            self._error = exc
        finally:
            self.conversations.disconnect(self)
            self._ready.set()
