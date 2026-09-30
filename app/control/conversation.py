"""Route HTTP conversation reads to the owning worker's live connection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError

from app.contracts.conversation import ConversationFailure, ConversationPage, SessionRef
from app.contracts.protocol import ConversationQuery, ConversationResult, WorkerMessage
from app.domain.models import Attempt, Flow, FlowExecution


class ConversationSocket(Protocol):
    async def send_text(self, data: str) -> None: ...


class ConversationStore(Protocol):
    def get_flow(self, flow_id: int) -> Flow | None: ...

    def get_flow_execution(self, flow_id: int) -> FlowExecution | None: ...

    def list_attempts(self, flow_id: int) -> list[Attempt]: ...


@dataclass
class PendingQuery:
    socket: ConversationSocket
    request: ConversationQuery
    future: asyncio.Future[ConversationPage]


class ConversationBroker:
    """Correlate bounded live requests; no queue, retries, or transcript storage."""

    def __init__(self) -> None:
        self.timeout = 10.0
        self.connections: dict[str, tuple[str, ConversationSocket]] = {}
        self.pending: dict[str, PendingQuery] = {}

    def connect(self, worker_id: str, epoch: str, socket: ConversationSocket) -> None:
        previous = self.connections.get(worker_id)
        if previous is not None:
            self.disconnect(previous[1])
        self.connections[worker_id] = (epoch, socket)

    def disconnect(self, socket: ConversationSocket) -> None:
        for worker_id, (_, current) in list(self.connections.items()):
            if current is socket:
                del self.connections[worker_id]
        for pending in self.pending.values():
            if pending.socket is socket and not pending.future.done():
                pending.future.set_exception(ConversationFailure("worker_unavailable"))

    async def query(
        self, worker_id: str, flow_id: int, sessions: list[SessionRef], cursor: str | None, limit: int,
    ) -> ConversationPage:
        connection = self.connections.get(worker_id)
        if connection is None:
            raise ConversationFailure("worker_unavailable")
        epoch, socket = connection
        if len(self.pending) >= 64 or sum(p.socket is socket for p in self.pending.values()) >= 4:
            raise ConversationFailure("query_busy")
        request = ConversationQuery(request_id=uuid4().hex, connection_epoch=epoch,
                                    flow_id=flow_id, sessions=sessions, cursor=cursor, limit=limit)
        future: asyncio.Future[ConversationPage] = asyncio.get_running_loop().create_future()
        self.pending[request.request_id] = PendingQuery(socket, request, future)
        try:
            async with asyncio.timeout(self.timeout):
                await socket.send_text(request.model_dump_json())
                return await future
        except TimeoutError as exc:
            raise ConversationFailure("query_timeout") from exc
        except (ConnectionError, RuntimeError, OSError) as exc:
            raise ConversationFailure("worker_unavailable") from exc
        finally:
            self.pending.pop(request.request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def accept(self, socket: ConversationSocket, message: WorkerMessage) -> bool:
        """Consume query responses in every execution state, ignoring stale replies."""

        if not isinstance(message, ConversationResult):
            return False
        pending = self.pending.get(message.request_id)
        if (pending is None or pending.socket is not socket or pending.future.done()
                or message.connection_epoch != pending.request.connection_epoch):
            return True
        if message.error is not None:
            pending.future.set_exception(ConversationFailure(message.error))
        else:
            page = message.page
            assert page is not None
            allowed = {ref.session_id: ref for ref in pending.request.sessions}
            if (page.flow_id != pending.request.flow_id or len(page.items) > pending.request.limit
                    or len(message.model_dump_json().encode()) > 512 * 1024
                    or any(item.session_id not in allowed
                           or item.attempt_id != allowed[item.session_id].attempt_id
                           or item.session_generation != allowed[item.session_id].session_generation
                           for item in [*page.items, *page.sessions])):
                pending.future.set_exception(ConversationFailure("invalid_response"))
            else:
                pending.future.set_result(page)
        return True


class ConversationService:
    """Resolve trusted flow/session metadata before sending a worker query."""

    def __init__(self, store: ConversationStore, broker: ConversationBroker) -> None:
        self.store = store
        self.broker = broker

    def _resolve(self, flow_id: int) -> tuple[str | None, list[SessionRef]]:
        if self.store.get_flow(flow_id) is None:
            raise ConversationFailure("flow_not_found")
        execution = self.store.get_flow_execution(flow_id)
        if execution is None:
            return None, []
        refs: dict[str, SessionRef] = {}
        for attempt in sorted(self.store.list_attempts(flow_id), key=lambda a: a.id):
            if attempt.codex_session_id:
                try:
                    refs.setdefault(attempt.codex_session_id, SessionRef(
                        attempt_id=attempt.id, session_id=attempt.codex_session_id,
                        session_generation=attempt.session_generation,
                    ))
                except ValidationError as exc:
                    raise ConversationFailure("history_unreadable") from exc
        if len(refs) > 128:
            raise ConversationFailure("history_limit")
        return execution.worker_id, list(refs.values())

    async def read(self, flow_id: int, cursor: str | None, limit: int) -> ConversationPage:
        worker_id, sessions = await asyncio.to_thread(self._resolve, flow_id)
        if worker_id is None or not sessions:
            if cursor:
                raise ConversationFailure("cursor_invalid")
            return ConversationPage(flow_id=flow_id,
                                    unavailable_reason="no_worker" if worker_id is None else "no_session")
        return await self.broker.query(worker_id, flow_id, sessions, cursor, limit)
