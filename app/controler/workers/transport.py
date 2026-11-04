"""Authenticate inbound worker sockets and hand one Session to the business driver."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol
from uuid import uuid4

from fastapi import WebSocketDisconnect
from loguru import logger
from pydantic import ValidationError

from app.communication.session import Session, SessionOverflow, Transport
from app.contracts.protocol import (
    ProtocolError,
    Register,
    ServerFrame,
    ServerMessage,
    WorkerMessage,
    frame_header,
    parse_worker_message,
)
from app.contracts.validation import parse_worker_input
from app.controler.workers.conversation import conversation_result_key
from app.controler.workers.errors import ProtocolViolation


class WorkerSocket(Transport, Protocol):
    @property
    def headers(self) -> Mapping[str, str]: ...

    async def accept(self) -> None: ...


class _SocketTransport:
    """Keep already-disconnected ASGI closes out of shared Session cleanup."""

    def __init__(self, socket: WorkerSocket) -> None:
        self.socket = socket

    async def send_text(self, data: str) -> None:
        await self.socket.send_text(data)

    async def receive_text(self) -> str:
        return await self.socket.receive_text()

    async def close(self, code: int = 1000) -> None:
        try:
            await self.socket.close(code)
        except WebSocketDisconnect:
            pass  # The peer is already gone; Session must still join its communication tasks.


def _authenticated_identity(
    headers: Mapping[str, str],
    worker_tokens: Mapping[str, str],
) -> str | None:
    identity = headers.get("x-flowtool-worker-id", "")
    authorization = headers.get("authorization", "")
    token = worker_tokens.get(identity)
    if token is None:
        return None
    expected_authorization = f"Bearer {token}"
    return identity if hmac.compare_digest(authorization, expected_authorization) else None


def control_ack(message_id: str) -> ServerFrame:
    """Build a receipt-only frame; it grants no connection or assignment authority."""
    return ServerFrame(
        **(frame_header(None) | {"ack": True, "ack_id": message_id}),
        control=None,
        payload=[],
    )


async def serve_worker(
    socket: WorkerSocket,
    *,
    worker_tokens: Mapping[str, str],
    serve: Callable[[str, Register, Session[WorkerMessage, ServerMessage]], Awaitable[None]],
) -> None:
    """Validate the actual first frame, then transfer cleanup to the Runtime's driver."""
    worker_id = _authenticated_identity(socket.headers, worker_tokens)
    if worker_id is None:
        await socket.close(code=1008)
        return
    session: Session[WorkerMessage, ServerMessage] | None = None
    close_code = 1000
    handed_off = False
    try:
        try:
            await socket.accept()
            # A dispatching Session would discard unsolicited replies before this check.
            register = parse_worker_message(await socket.receive_text())
            if not isinstance(register, Register) or register.worker_id != worker_id:
                raise ProtocolViolation("worker registration does not match authenticated identity")
            session = Session(
                _SocketTransport(socket),
                parse=parse_worker_input,
                reply_key=conversation_result_key,
                make_ack=control_ack,
                epoch=uuid4().hex,
            )
            session.register_control(register)
            session.start()
        except (WebSocketDisconnect, ConnectionError, TimeoutError) as exc:
            logger.bind(
                event="worker_disconnected", reason_code="connection_lost", worker_id=worker_id
            ).opt(exception=exc).info("")
        except (ProtocolViolation, SessionOverflow, ValidationError) as exc:
            # Keep handshake mapping aligned with ServerCallbacks' business error mapping.
            invalid = isinstance(exc, ValidationError)
            reason = "message failed protocol validation" if invalid else str(exc)
            close_code = 1008
            logger.warning("worker protocol violation: {}", reason)
            try:
                error = ProtocolError(
                    **frame_header(None),
                    control="error",
                    payload=[],
                    code="invalid_message" if invalid else "protocol_violation",
                    detail=reason,
                )
                if session is None:
                    await socket.send_text(error.model_dump_json())
                else:
                    await session.send(error)
            except (WebSocketDisconnect, ConnectionError):
                pass
        else:
            # No await between transfer and the Runtime entering its own cleanup scope.
            handed_off = True
            await serve(worker_id, register, session)
    finally:
        if not handed_off:
            if session is not None:
                await session.close(close_code)
            else:
                await socket.close(close_code)
