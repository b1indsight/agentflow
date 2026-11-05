"""Route HTTP conversation reads to the owning worker's live connection."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

from pydantic import ValidationError

from app.communication.gateway import Gateway
from app.communication.session import Session, SessionClosed
from app.contracts.conversation import (
    ConversationFailure,
    ConversationItem,
    ConversationPage,
    HistoryPage,
    HistoryQuery,
    SessionHistory,
)
from app.contracts.protocol import (
    ConversationQuery,
    ConversationResult,
    HistoryArguments,
    ServerMessage,
    ServerMessageUnion,
    WorkerMessageUnion,
    call_header,
    message_header,
)
from app.domain.models import Attempt
from app.persistence.database import Database
from app.persistence.flows import FlowStore
from app.persistence.workers import WorkerStore


class ConversationBroker:
    """Own bounded HTTP query waiters; Dispatcher correlates replies on the serial driver."""

    def __init__(self, peers: Gateway[WorkerMessageUnion, ServerMessageUnion]) -> None:
        # Amp export allows 20s, followed by up to 9s of descendant/process cleanup.
        self.timeout = 30.0
        self._peers = peers
        self._inflight: dict[Session[WorkerMessageUnion, ServerMessageUnion], int] = {}

    async def query(
        self,
        worker_id: str,
        query: HistoryQuery,
    ) -> HistoryPage:
        registration = self._peers.get(worker_id)
        if registration is None or registration.session.epoch is None:
            raise ConversationFailure("worker_unavailable")
        session, routes = registration.session, registration.routes
        if sum(self._inflight.values()) >= 64 or self._inflight.get(session, 0) >= 4:
            raise ConversationFailure("query_busy")
        request = ConversationQuery(
            **call_header(),
            method="worker.history",
            arguments=HistoryArguments(**query.model_dump()),
        )
        future: asyncio.Future[tuple[ConversationResult, WorkerMessageUnion]] = (
            asyncio.get_running_loop().create_future()
        )
        sending: asyncio.Task[bool] | None = None

        async def deliver(reply: ConversationResult, message: WorkerMessageUnion) -> None:
            if not future.done():
                future.set_result((reply, message))

        def rejected(reason: str) -> None:
            if not future.done():
                future.set_exception(ConversationFailure("invalid_response"))

        def closed() -> None:
            if not future.done():
                future.set_exception(SessionClosed("query connection closed"))
            # Session closes any unfinished write. Do not cancel a completed write's
            # still-resuming send task: a reply drained before EOF remains valid.

        self._inflight[session] = self._inflight.get(session, 0) + 1
        try:
            session.check_request()
            routes.expect_reply(
                request, ConversationResult, deliver, on_rejected=rejected, on_closed=closed
            )
            async with asyncio.timeout(self.timeout):
                sending = asyncio.create_task(
                    session.send(
                        ServerMessage(
                            **message_header(session.epoch), control=None, payload=[request]
                        ),
                        is_current=lambda: not future.done(),
                    )
                )
                await asyncio.wait((sending, future), return_when=asyncio.FIRST_COMPLETED)
                if future.done():
                    future.result()  # Rejection/closure wins even while the send lock is held.
                if not await sending:
                    raise SessionClosed("query not written")
                response, message = await future
            return self._validated(request, response, message)
        except TimeoutError as exc:
            raise ConversationFailure("query_timeout") from exc
        except SessionClosed as exc:
            raise ConversationFailure("worker_unavailable") from exc
        finally:
            routes.forget_reply(request.message_id)
            try:
                if sending is not None:
                    sending.cancel()
                    await asyncio.gather(sending, return_exceptions=True)
            finally:
                self._inflight[session] -= 1
                if not self._inflight[session]:
                    del self._inflight[session]
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()

    def _validated(
        self,
        request: ConversationQuery,
        response: ConversationResult,
        message: WorkerMessageUnion,
    ) -> HistoryPage:
        """Correlation alone does not make a worker's history page safe to expose."""
        if response.reply_to != request.message_id:
            raise ConversationFailure("invalid_response")
        if response.error is not None:
            raise ConversationFailure(response.error)
        page = response.page
        assert page is not None
        # Other results sharing the delivery do not consume this page's byte allowance.
        selected = message.model_copy(update={"payload": [response]})
        if (
            page.session_id != request.arguments.session_id
            or len(page.items) > request.arguments.limit
            or len(selected.model_dump_json().encode()) > 512 * 1024
            or (page.has_more and page.next_cursor is None)
            or any(not item.id.startswith(f"{page.session_id}:") for item in page.items)
        ):
            raise ConversationFailure("invalid_response")
        try:
            HistoryQuery(session_id=page.session_id, cursor=page.next_cursor)
        except (ConversationFailure, ValidationError) as exc:
            raise ConversationFailure("invalid_response") from exc
        return page


class ConversationService:
    """Resolve trusted flow/session metadata before sending a worker query."""

    def __init__(self, broker: ConversationBroker, *, database: Database) -> None:
        self.flows = FlowStore(database)
        self.workers = WorkerStore(database)
        self.broker = broker

    def _resolve(self, flow_id: int) -> tuple[str | None, list[Attempt]]:
        if self.flows.get_flow(flow_id) is None:
            raise ConversationFailure("flow_not_found")
        execution = self.workers.get_flow_execution(flow_id)
        if execution is None:
            return None, []
        attempts: dict[str, Attempt] = {}
        for attempt in sorted(self.flows.list_attempts(flow_id), key=lambda a: a.id):
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
            return ConversationPage(
                flow_id=flow_id,
                unavailable_reason="no_worker" if worker_id is None else "no_session",
            )
        fingerprint = hashlib.sha256(
            json.dumps(
                [flow_id, [[a.id, a.codex_session_id, a.session_generation] for a in attempts]]
            ).encode()
        ).hexdigest()
        # The outer cursor binds session order/generation; worker sees only its inner cursor.
        index, inner_cursor = 0, None
        if cursor is not None:
            try:
                data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if not isinstance(data, list) or len(data) != 3:
                    raise ValueError("invalid flow cursor")
                scope, index, inner_cursor = data
                if (
                    scope != fingerprint
                    or type(index) is not int
                    or not 0 <= index < len(attempts)
                    or (inner_cursor is not None and not isinstance(inner_cursor, str))
                ):
                    raise ValueError("invalid flow cursor scope")
            except (ValueError, TypeError) as exc:
                raise ConversationFailure("cursor_invalid") from exc
        attempt = attempts[index]
        assert attempt.codex_session_id is not None
        try:
            query = HistoryQuery(
                session_id=attempt.codex_session_id, cursor=inner_cursor, limit=limit
            )
        except ValidationError as exc:
            raise ConversationFailure("cursor_invalid") from exc
        page = await self.broker.query(worker_id, query)
        # Only control attaches trusted Flow/Attempt attribution to worker records.
        attribution = {
            "attempt_id": attempt.id,
            "session_id": attempt.codex_session_id,
            "session_generation": attempt.session_generation,
        }
        next_index, next_inner = index, page.next_cursor
        has_more = page.has_more
        if not has_more and index + 1 < len(attempts):
            next_index, next_inner, has_more = index + 1, None, True
        # At the final session, retain its EOF cursor so later polls see appended records.
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(
                [fingerprint, next_index, next_inner],
            ).encode()
        ).decode()
        return ConversationPage(
            flow_id=flow_id,
            items=[
                ConversationItem.model_validate({**item.model_dump(), **attribution})
                for item in page.items
            ],
            sessions=[
                SessionHistory.model_validate(
                    {
                        **attribution,
                        "status": page.status,
                        "starts_at_beginning": inner_cursor is None,
                        "reached_end": not page.has_more,
                    }
                )
            ],
            next_cursor=next_cursor,
            has_more=has_more,
        )
