"""Bounded text transport pairs for real Session endpoints in the same event loop."""

import asyncio
from collections import deque
from dataclasses import dataclass, field

from app.communication.session import SessionClosed, SessionOverflow, Transport

_MAX_MESSAGES = 32
_MAX_BYTES = 16 * 1024 * 1024


@dataclass
class _Buffer:
    messages: deque[tuple[str, int]] = field(default_factory=deque)
    size: int = 0


@dataclass
class _Connection:
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    closed: bool = False


class _InProcessTransport:
    def __init__(self, connection: _Connection, incoming: _Buffer, outgoing: _Buffer) -> None:
        self.connection, self.incoming, self.outgoing = connection, incoming, outgoing

    async def send_text(self, data: str) -> None:
        size = len(data.encode())
        if size > _MAX_BYTES:
            raise SessionOverflow("in-process message exceeds buffer limit")
        async with self.connection.changed:
            await self.connection.changed.wait_for(lambda: self.connection.closed or (
                len(self.outgoing.messages) < _MAX_MESSAGES
                and self.outgoing.size + size <= _MAX_BYTES
            ))
            if self.connection.closed:
                raise SessionClosed("in-process connection closed")
            self.outgoing.messages.append((data, size))
            self.outgoing.size += size
            self.connection.changed.notify_all()

    async def receive_text(self) -> str:
        async with self.connection.changed:
            await self.connection.changed.wait_for(
                lambda: self.connection.closed or bool(self.incoming.messages)
            )
            if self.connection.closed:
                raise SessionClosed("in-process connection closed")
            data, size = self.incoming.messages.popleft()
            self.incoming.size -= size
            self.connection.changed.notify_all()
            return data

    async def close(self, code: int = 1000) -> None:
        async with self.connection.changed:
            self.connection.closed = True
            for buffer in (self.incoming, self.outgoing):
                buffer.messages.clear()
                buffer.size = 0
            self.connection.changed.notify_all()


def in_process_transport_pair() -> tuple[Transport, Transport]:
    """Return two bounded endpoints; closing either wakes all blocked readers and writers."""
    connection, first, second = _Connection(), _Buffer(), _Buffer()
    return (
        _InProcessTransport(connection, first, second),
        _InProcessTransport(connection, second, first),
    )
