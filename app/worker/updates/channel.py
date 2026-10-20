"""Multiplex update requests without taking the assignment socket's reader."""

import asyncio
from typing import Protocol

from app.contracts.updates import (
    MaintenanceReply,
    ReleaseAvailable,
    ReleaseEvent,
    WorkerMaintenance,
)


class UpdateSocket(Protocol):
    async def send(self, data: str) -> None: ...


class UpdateChannel:
    """Connection-scoped replies and a bounded latest-release notification slot."""

    def __init__(self) -> None:
        self.notices: asyncio.Queue[ReleaseEvent] = asyncio.Queue(maxsize=1)
        self.epoch: str | None = None
        self._socket: UpdateSocket | None = None
        self._pending: dict[str, asyncio.Future[MaintenanceReply]] = {}

    def connect(self, socket: UpdateSocket, epoch: str) -> None:
        self._socket, self.epoch = socket, epoch

    def disconnect(self) -> None:
        self._socket, self.epoch = None, None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("update connection changed"))

    def accept(self, message: MaintenanceReply | ReleaseAvailable) -> None:
        """Called only by the existing single reader, including while executing."""
        if isinstance(message, ReleaseAvailable):
            if not self.notices.empty():
                self.notices.get_nowait()
            self.notices.put_nowait(message.release)
            return
        future = self._pending.get(message.request_id)
        if future is not None and not future.done() and message.connection_epoch == self.epoch:
            future.set_result(message)

    async def request(self, message: WorkerMaintenance) -> MaintenanceReply:
        """A missing response is ambiguous; only the owning operation may reconcile it."""
        socket = self._socket
        if socket is None or self.epoch != message.connection_epoch:
            raise ConnectionError("update requires the original registered connection")
        future: asyncio.Future[MaintenanceReply] = asyncio.get_running_loop().create_future()
        self._pending[message.request_id] = future
        try:
            await socket.send(message.model_dump_json())
            reply = await asyncio.wait_for(future, 15)
            if reply.operation_id != message.operation_id:
                raise RuntimeError("update response belongs to another operation")
            return reply
        finally:
            self._pending.pop(message.request_id, None)
