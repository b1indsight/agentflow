"""Register opaque file links and route bounded reads to their owning worker."""

from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import WebSocketDisconnect
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from app.contracts.files import MAX_FILE_BYTES, FileFailure
from app.contracts.protocol import (
    FileQuery,
    FileRegister,
    FileRegistered,
    FileResult,
    WorkerMessage,
)
from app.persistence.database import Database
from app.persistence.file_links import FileLinkStore

if TYPE_CHECKING:
    from app.controler.workers.connection import WorkerConnection


@dataclass
class PendingFile:
    connection: WorkerConnection
    request: FileQuery
    future: asyncio.Future[bytes]


class FileService:
    """Keep durable routing separate from ephemeral connection-fenced requests.

    The pending-query lifecycle follows ConversationBroker; file registration and
    byte validation stay here instead of coupling files to conversation sessions.
    """

    def __init__(self, database: Database) -> None:
        self.store = FileLinkStore(database)
        self.timeout = 10.0
        self.connections: dict[str, tuple[str, WorkerConnection]] = {}
        self.pending: dict[str, PendingFile] = {}

    def connect(self, worker_id: str, epoch: str, connection: WorkerConnection) -> None:
        previous = self.connections.get(worker_id)
        if previous is not None:
            self.disconnect(previous[1])
        self.connections[worker_id] = (epoch, connection)

    def disconnect(self, connection: WorkerConnection) -> None:
        for worker_id, (_, current) in list(self.connections.items()):
            if current is connection:
                del self.connections[worker_id]
        for pending in self.pending.values():
            if pending.connection is connection and not pending.future.done():
                pending.future.set_exception(FileFailure("worker_unavailable"))

    async def register(
        self, worker_id: str, connection: WorkerConnection, request: FileRegister,
    ) -> None:
        """Use the authenticated connection identity, never a worker-supplied identity."""
        if self.connections.get(worker_id) != (request.connection_epoch, connection):
            return
        try:
            link = await asyncio.to_thread(self.store.register, worker_id, request.file_id)
            response = FileRegistered(request_id=request.request_id,
                                      connection_epoch=request.connection_epoch,
                                      link=f"/api/files/{link.link_id}")
        except (DatabaseError, PoolTimeout):
            response = FileRegistered(request_id=request.request_id,
                                      connection_epoch=request.connection_epoch,
                                      error="registration_unavailable")
        if self.connections.get(worker_id) == (request.connection_epoch, connection):
            await connection.send_text(response.model_dump_json())

    async def read(self, link_id: str) -> bytes:
        """Resolve a persisted link, then retrieve current bytes with no cache or retries."""
        if not re.fullmatch(r"[0-9a-f]{32}", link_id):
            raise FileFailure("file_not_found")
        link = await asyncio.to_thread(self.store.get, link_id)
        if link is None:
            raise FileFailure("file_not_found")
        session = self.connections.get(link.worker_id)
        if session is None:
            raise FileFailure("worker_unavailable")
        epoch, connection = session
        if len(self.pending) >= 64 or sum(p.connection is connection for p in self.pending.values()) >= 4:
            raise FileFailure("query_busy")
        request = FileQuery(request_id=uuid4().hex, connection_epoch=epoch, file_id=link.file_id)
        future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self.pending[request.request_id] = PendingFile(connection, request, future)
        try:
            async with asyncio.timeout(self.timeout):
                await connection.send_text(request.model_dump_json())
                return await future
        except TimeoutError:
            raise FileFailure("query_timeout") from None
        except (OSError, RuntimeError, WebSocketDisconnect):
            raise FileFailure("worker_unavailable") from None
        finally:
            self.pending.pop(request.request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def accept(self, connection: WorkerConnection, message: WorkerMessage) -> bool:
        """Ignore stale/cross-worker responses even when their request ID matches."""
        if not isinstance(message, FileResult):
            return False
        pending = self.pending.get(message.request_id)
        if (pending is None or pending.connection is not connection or pending.future.done()
                or message.connection_epoch != pending.request.connection_epoch):
            return True
        if message.error is not None:
            pending.future.set_exception(FileFailure(message.error))
        else:
            assert message.content_base64 is not None
            try:
                content = base64.b64decode(message.content_base64, validate=True)
                if len(content) > MAX_FILE_BYTES:
                    raise ValueError("file exceeds limit")
            except ValueError:
                pending.future.set_exception(FileFailure("invalid_response"))
            else:
                pending.future.set_result(content)
        return True
