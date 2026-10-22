"""Acknowledge availability changes through the assignment stream's single reader."""

import asyncio
from typing import Protocol
from uuid import uuid4

from websockets.exceptions import ConnectionClosed

from app.contracts.protocol import AvailabilityAccepted, WorkerAvailability


class AvailabilitySocket(Protocol):
    async def send(self, data: str) -> None: ...

    async def close(self) -> None: ...


class AvailabilityChannel:
    """Availability and replies belong only to the current registered connection."""

    def __init__(self) -> None:
        self.epoch: str | None = None
        self._socket: AvailabilitySocket | None = None
        self._pending: dict[str, asyncio.Future[AvailabilityAccepted]] = {}

    def connect(self, socket: AvailabilitySocket, epoch: str) -> None:
        self._socket, self.epoch = socket, epoch

    def disconnect(self) -> None:
        self._socket, self.epoch = None, None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("availability connection changed"))

    def accept(self, message: AvailabilityAccepted) -> None:
        future = self._pending.get(message.request_id)
        if future is not None and not future.done() and message.connection_epoch == self.epoch:
            future.set_result(message)

    async def set_paused(self, paused: bool) -> str:
        """Return the acknowledged epoch; callers must undo an ambiguous timeout."""
        socket, epoch = self._socket, self.epoch
        if socket is None or epoch is None:
            raise ConnectionError("availability requires a registered connection")
        message = WorkerAvailability(
            paused=paused,
            request_id=uuid4().hex,
            connection_epoch=epoch,
        )
        future: asyncio.Future[AvailabilityAccepted] = asyncio.get_running_loop().create_future()
        self._pending[message.request_id] = future
        try:
            async with asyncio.timeout(15):
                await socket.send(message.model_dump_json())
                reply = await future
            if reply.paused != paused or self.epoch != epoch:
                raise ConnectionError("availability changed before confirmation")
            return epoch
        except ConnectionClosed as error:
            future.cancel()
            if self._socket is socket and self.epoch == epoch:
                self.disconnect()
            raise ConnectionError("availability connection closed") from error
        finally:
            self._pending.pop(message.request_id, None)

    async def close(self) -> None:
        """Discard an ambiguous pause by closing its connection, then reconnect normally."""
        socket = self._socket
        self.disconnect()
        if socket is not None:
            await socket.close()
