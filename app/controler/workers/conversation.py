"""Route HTTP conversation reads to the owning worker's live connection."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import ValidationError

from app.contracts.conversation import (
    ConversationFailure,
    ConversationItem,
    ConversationPage,
    HistoryPage,
    HistoryQuery,
    SessionHistory,
)
from app.contracts.protocol import ConversationQuery, ConversationResult, WorkerMessage
from app.domain.models import Attempt
from app.persistence.store import Store

if TYPE_CHECKING:
    from app.controler.workers.connection import WorkerConnection


@dataclass
class PendingQuery:
    connection: WorkerConnection
    request: ConversationQuery
    future: asyncio.Future[HistoryPage]


class ConversationBroker:
    """Correlate bounded live requests; no queue, retries, or transcript storage."""

    def __init__(self) -> None:
        self.timeout = 10.0
        self.connections: dict[str, tuple[str, WorkerConnection]] = {}
        self.pending: dict[str, PendingQuery] = {}

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
                pending.future.set_exception(ConversationFailure("worker_unavailable"))

    async def query(
        self, worker_id: str, query: HistoryQuery,
    ) -> HistoryPage:
        session = self.connections.get(worker_id)
        if session is None:
            raise ConversationFailure("worker_unavailable")
        epoch, connection = session
        if len(self.pending) >= 64 or sum(p.connection is connection for p in self.pending.values()) >= 4:
            raise ConversationFailure("query_busy")
        request = ConversationQuery(request_id=uuid4().hex, connection_epoch=epoch,
                                    **query.model_dump())
        future: asyncio.Future[HistoryPage] = asyncio.get_running_loop().create_future()
        self.pending[request.request_id] = PendingQuery(connection, request, future)
        try:
            async with asyncio.timeout(self.timeout):
                await connection.send_text(request.model_dump_json())
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

    def accept(self, connection: WorkerConnection, message: WorkerMessage) -> bool:
        """Consume query responses in every execution state, ignoring stale replies."""

        if not isinstance(message, ConversationResult):
            return False
        pending = self.pending.get(message.request_id)
        if (pending is None or pending.connection is not connection or pending.future.done()
                or message.connection_epoch != pending.request.connection_epoch):
            return True
        if message.error is not None:
            pending.future.set_exception(ConversationFailure(message.error))
        else:
            page = message.page
            assert page is not None
            if (page.session_id != pending.request.session_id or len(page.items) > pending.request.limit
                    or len(message.model_dump_json().encode()) > 512 * 1024
                    or (page.has_more and page.next_cursor is None)
                    or any(not item.id.startswith(f"{page.session_id}:") for item in page.items)):
                pending.future.set_exception(ConversationFailure("invalid_response"))
            else:
                try:
                    HistoryQuery(session_id=page.session_id, cursor=page.next_cursor)
                except (ConversationFailure, ValidationError):
                    pending.future.set_exception(ConversationFailure("invalid_response"))
                else:
                    pending.future.set_result(page)
        return True


class ConversationService:
    """Resolve trusted flow/session metadata before sending a worker query."""

    def __init__(self, store: Store, broker: ConversationBroker) -> None:
        self.store = store
        self.broker = broker

    def _resolve(self, flow_id: int) -> tuple[str | None, list[Attempt]]:
        if self.store.flows.get_flow(flow_id) is None:
            raise ConversationFailure("flow_not_found")
        execution = self.store.workers.get_flow_execution(flow_id)
        if execution is None:
            return None, []
        attempts: dict[str, Attempt] = {}
        for attempt in sorted(self.store.flows.list_attempts(flow_id), key=lambda a: a.id):
            if attempt.codex_session_id:
                try:
                    HistoryQuery(session_id=attempt.codex_session_id)
                except ValidationError as exc:
                    raise ConversationFailure("history_unreadable") from exc
                attempts.setdefault(attempt.codex_session_id, attempt)
        if len(attempts) > 128:
            raise ConversationFailure("history_limit")
        return execution.worker_id, list(attempts.values())

    async def read(self, flow_id: int, cursor: str | None, limit: int) -> ConversationPage:
        """Read one session per HTTP page; control owns cross-session continuation."""
        worker_id, attempts = await asyncio.to_thread(self._resolve, flow_id)
        if worker_id is None or not attempts:
            if cursor:
                raise ConversationFailure("cursor_invalid")
            return ConversationPage(flow_id=flow_id,
                                    unavailable_reason="no_worker" if worker_id is None else "no_session")
        fingerprint = hashlib.sha256(json.dumps([flow_id, [
            [a.id, a.codex_session_id, a.session_generation] for a in attempts
        ]]).encode()).hexdigest()
        # The outer cursor binds session order/generation; worker sees only its inner cursor.
        index, inner_cursor = 0, None
        if cursor is not None:
            try:
                data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if not isinstance(data, list) or len(data) != 3:
                    raise ValueError("invalid flow cursor")
                scope, index, inner_cursor = data
                if (scope != fingerprint or type(index) is not int or not 0 <= index < len(attempts)
                        or (inner_cursor is not None and not isinstance(inner_cursor, str))):
                    raise ValueError("invalid flow cursor scope")
            except (ValueError, TypeError) as exc:
                raise ConversationFailure("cursor_invalid") from exc
        attempt = attempts[index]
        assert attempt.codex_session_id is not None
        try:
            query = HistoryQuery(session_id=attempt.codex_session_id, cursor=inner_cursor, limit=limit)
        except ValidationError as exc:
            raise ConversationFailure("cursor_invalid") from exc
        page = await self.broker.query(worker_id, query)
        # Only control attaches trusted Flow/Attempt attribution to worker records.
        attribution = {"attempt_id": attempt.id, "session_id": attempt.codex_session_id,
                       "session_generation": attempt.session_generation}
        next_index, next_inner = index, page.next_cursor
        has_more = page.has_more
        if not has_more and index + 1 < len(attempts):
            next_index, next_inner, has_more = index + 1, None, True
        # At the final session, retain its EOF cursor so later polls see appended records.
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            [fingerprint, next_index, next_inner],
        ).encode()).decode()
        return ConversationPage(
            flow_id=flow_id,
            items=[ConversationItem.model_validate({**item.model_dump(), **attribution}) for item in page.items],
            sessions=[SessionHistory.model_validate({**attribution, "status": page.status})],
            next_cursor=next_cursor, has_more=has_more,
        )
