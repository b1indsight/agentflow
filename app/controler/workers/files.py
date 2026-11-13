"""Register opaque file links and route bounded reads to their owning worker."""

import asyncio
import base64
import re

from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from app.communication.dispatch import Dispatcher
from app.communication.gateway import Gateway
from app.communication.requests import ReplyRejected, request_reply
from app.communication.session import Session, SessionClosed
from app.contracts.files import MAX_FILE_BYTES, FileFailure
from app.contracts.protocol import (
    FileQuery,
    FileRegister,
    FileRegistered,
    FileResult,
    ServerMessage,
    ServerMessageUnion,
    WorkerMessageUnion,
    call_header,
    message_header,
    result_header,
)
from app.persistence.database import Database
from app.persistence.file_links import FileLinkStore


class FileService:
    """Resolve durable links; the selected Session owns reply correlation and cleanup."""

    def __init__(self, peers: Gateway[WorkerMessageUnion, ServerMessageUnion], *, database: Database) -> None:
        self.store = FileLinkStore(database)
        self.timeout = 10.0
        self._peers = peers
        self._inflight: dict[Session[WorkerMessageUnion, ServerMessageUnion], int] = {}

    async def read(self, link_id: str) -> bytes:
        """Retrieve current bytes with no cache or retry on a different connection."""
        if not re.fullmatch(r"[0-9a-f]{32}", link_id):
            raise FileFailure("file_not_found")
        link = await asyncio.to_thread(self.store.get, link_id)
        if link is None:
            raise FileFailure("file_not_found")
        registration = self._peers.get(link.worker_id)
        if registration is None or registration.session.epoch is None:
            raise FileFailure("worker_unavailable")
        session = registration.session
        if sum(self._inflight.values()) >= 64 or self._inflight.get(session, 0) >= 4:
            raise FileFailure("query_busy")
        request = FileQuery(**call_header(), method="worker.file_read", file_id=link.file_id)
        self._inflight[session] = self._inflight.get(session, 0) + 1
        try:
            response, _ = await request_reply(
                registration, request, FileResult,
                ServerMessage(**message_header(session.epoch), control=None, payload=[request]),
                timeout=self.timeout,
            )
            if response.error is not None:
                raise FileFailure(response.error)
            assert response.content_base64 is not None
            try:
                content = base64.b64decode(response.content_base64, validate=True)
                if len(content) > MAX_FILE_BYTES:
                    raise ValueError("file exceeds limit")
            except ValueError:
                raise FileFailure("invalid_response") from None
            return content
        except TimeoutError:
            raise FileFailure("query_timeout") from None
        except SessionClosed:
            raise FileFailure("worker_unavailable") from None
        except ReplyRejected:
            raise FileFailure("invalid_response") from None
        finally:
            self._inflight[session] -= 1
            if not self._inflight[session]:
                del self._inflight[session]


class ServerFileCallbacks:
    """Keep bounded registration IO off the serial driver; replies stay on that driver."""

    def __init__(self, *, worker_id: str, session: Session[WorkerMessageUnion, ServerMessageUnion],
                 store: FileLinkStore) -> None:
        self.worker_id, self.session, self.store = worker_id, session, store
        self._pending: set[asyncio.Task[FileRegistered]] = set()

    def bind(self, routes: Dispatcher[WorkerMessageUnion]) -> None:
        routes.register("server.file_register", FileRegister, self.on_register)
        routes.before_wait(self.prepare)

    async def on_register(self, call: FileRegister, message: WorkerMessageUnion) -> None:
        # ServerCallbacks authorizes the envelope before any file callback runs.
        if len(self._pending) >= 4:
            await self._send(FileRegistered(**result_header(call.message_id), method=call.method,
                                            error="query_busy"))
            return
        task = asyncio.create_task(self._register(call))
        self._pending.add(task)
        task.add_done_callback(self._completed)

    def _completed(self, task: asyncio.Task[FileRegistered]) -> None:
        if not task.cancelled():
            task.exception()
        self.session.wake()

    async def _register(self, call: FileRegister) -> FileRegistered:
        """Only the authenticated connection supplies the worker identity."""
        try:
            link = await asyncio.to_thread(self.store.register, self.worker_id, call.file_id)
            return FileRegistered(**result_header(call.message_id), method=call.method,
                                  link=f"/api/files/{link.link_id}")
        except (DatabaseError, PoolTimeout):
            return FileRegistered(**result_header(call.message_id), method=call.method,
                                  error="registration_unavailable")

    async def _send(self, result: FileRegistered) -> None:
        await self.session.send(ServerMessage(**message_header(self.session.epoch),
                                              control=None, payload=[result]))

    async def prepare(self) -> None:
        # Like worker file reads, this bounded burst does not consume Session watch slots.
        for task in tuple(self._pending):
            if task.done():
                self._pending.remove(task)
                await self._send(task.result())

    async def aclose(self) -> None:
        for task in self._pending:
            task.cancel()
        await asyncio.gather(*self._pending, return_exceptions=True)
        self._pending.clear()
