"""Single-reader worker connection with immediate conversation-response dispatch."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from typing import Protocol

from fastapi import WebSocketDisconnect

from app.contracts.protocol import parse_worker_message
from app.controler.workers.conversation import ConversationBroker


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

    def __init__(self, socket: WorkerSocket, conversations: ConversationBroker, worker_id: str, epoch: str) -> None:
        self.socket = socket
        self.conversations = conversations
        self._messages: deque[tuple[str, int]] = deque()
        self._bytes = 0
        self._ready = asyncio.Event()
        self._error: Exception | None = None
        self.conversations.connect(worker_id, epoch, self)
        self._reader = asyncio.create_task(self._receive())

    async def send_text(self, data: str) -> None:
        await self.socket.send_text(data)

    async def close(self, code: int = 1000) -> None:
        try:
            await self.socket.close(code)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Join the receiver when the gateway exits or a new connection takes over."""
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)
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
