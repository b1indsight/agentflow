"""Adapt an outbound WebSocket to the shared Session transport."""

from typing import Protocol


class Connection(Protocol):
    async def send(self, data: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self, code: int = 1000) -> None: ...


class WebSocketTransport:
    """Map outbound WebSocket IO to the transport shared with the server."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    async def send_text(self, data: str) -> None:
        await self.connection.send(data)

    async def receive_text(self) -> str:
        raw = await self.connection.recv()
        return raw.decode() if isinstance(raw, bytes) else raw

    async def close(self, code: int = 1000) -> None:
        await self.connection.close(code)
