"""Controller-managed infer-only worker using the ordinary registration and RPC protocol."""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

from loguru import logger

from app.communication.dispatch import Dispatcher
from app.communication.gateway import Gateway
from app.communication.inprocess import in_process_transport_pair
from app.communication.session import Ready, Session, SessionClosed
from app.contracts.protocol import (
    CancelJob,
    PayloadValidationError,
    ProtocolError,
    Register,
    Registered,
    ServerMessage,
    ServerMessageUnion,
    ToolCall,
    ToolResult,
    ValidationErrorResult,
    WorkerMessage,
    WorkerMessageUnion,
    message_header,
    parse_worker_message,
    result_header,
)
from app.contracts.validation import RejectedMessage, parse_server_input, parse_worker_input
from app.domain.inference_models import LOCAL_BUB_WORKER_ID, InferenceCapability
from app.domain.ports import Inference
from app.inference.bub import ADAPTER_VERSION
from app.inference.transport import InferenceProtocolError, WorkerInferenceCallbacks


def _server_ack(message_id: str) -> ServerMessage:
    # Same directional receipt as controller transport, without importing controller ownership.
    return ServerMessage(
        **(message_header(None) | {"ack": True, "ack_id": message_id}),
        control=None, payload=[],
    )


def _worker_ack(message_id: str) -> WorkerMessage:
    # Keep aligned with worker.transport.state.control_ack; no coding owner belongs here.
    return WorkerMessage(
        **(message_header(None) | {"ack": True, "ack_id": message_id}),
        control=None, payload=[],
    )


class _LocalConnection:
    """Infer-only handshake and control authority, without coding recovery or Flow routes."""

    def __init__(self, session: Session[ServerMessageUnion, WorkerMessageUnion],
                 inference: Inference) -> None:
        self.session = session
        self.ready = False
        self._error_logged = False
        self.inference = WorkerInferenceCallbacks(
            session=session, inference=inference, ready=lambda: self.ready,
        )

    def bind(self, routes: Dispatcher[ServerMessageUnion]) -> None:
        self.inference.bind(routes)
        routes.configure(
            opened=self.on_opened, message=self.on_message,
            unmatched_reply=self.on_unrouted, unrouted=self.on_unrouted,
            validation_error=self.on_error, rejected=self.on_rejected, closed=self.on_closed,
        )

    async def on_opened(self) -> None:
        await self.session.send(Register(
            **message_header(None), control="register", payload=[],
            worker_id=LOCAL_BUB_WORKER_ID, execute=False, recovery=None,
            inference=InferenceCapability(adapter_version=ADAPTER_VERSION),
        ))
        self.session.start()

    async def on_message(self, message: ServerMessageUnion) -> None:
        if isinstance(message, ProtocolError):
            raise InferenceProtocolError(f"serve protocol error: {message.code}: {message.detail}")
        if self.session.epoch is not None and message.connection_epoch != self.session.epoch:
            if message.control is None and all(
                isinstance(item, ValidationErrorResult) for item in message.payload
            ):
                return  # Like an ordinary worker, stale validation errors grant no authority.
            raise InferenceProtocolError("message belongs to another connection epoch")
        if isinstance(message, Registered):
            if self.ready or message.recovery is not None:
                raise InferenceProtocolError("unexpected local inference registration decision")
            assert message.connection_epoch is not None
            self.session.bind_epoch(message.connection_epoch)
            self.ready = True
            self.session.periodic(message.heartbeat_interval, self.inference.heartbeat)
        elif not self.ready:
            raise InferenceProtocolError("inference requires completed registration")
        if isinstance(message, CancelJob):
            self.inference.cancel_job(message)

    def on_closed(self) -> None:
        self.ready = False

    async def on_unrouted(self, item: ToolCall | ToolResult, message: ServerMessageUnion) -> None:
        raise InferenceProtocolError("unsupported local inference message")

    async def on_error(self, error: ValidationErrorResult, message: ServerMessageUnion) -> None:
        # No outbound business invocations exist here. Result rejection cannot grant a new slot.
        if not self._error_logged:
            logger.warning("ignored unmatched validation error on local inference connection")
            self._error_logged = True

    def on_rejected(self, message: RejectedMessage) -> Ready:
        """Reject malformed infer arguments atomically, without accepting coding traffic."""
        if (not self.ready or message.connection_epoch != self.session.epoch
                or message.control is not None or any(
                    not isinstance(item, ToolCall) or item.method != "worker.infer"
                    for item in message.payload
                )):
            raise InferenceProtocolError("invalid payload has no local inference authority")

        async def respond() -> None:
            await self.session.send(WorkerMessage(
                **message_header(self.session.epoch), control=None,
                payload=[ValidationErrorResult(
                    **result_header(item.message_id), method=item.method,
                    error=PayloadValidationError(
                        code="validation_error", message="message payload failed validation",
                    ),
                ) for item in message.payload],
            ))

        return respond


async def serve_local_inference(
    serve: Callable[
        [str, Register, Session[WorkerMessageUnion, ServerMessageUnion]], Awaitable[None]
    ],
    inference: Inference,
) -> None:
    """Serve a preflighted bub provider until disconnect or controller task cancellation.

    The host receives an already-started Session after registration pre-read, exactly as
    with a network worker. It owns its usual server callbacks and cleanup. No executor,
    repository runtime or Flow routes are created. Cancellation joins both endpoints and
    the provider before returning, including repeated cancellation during child cleanup.
    """
    host_transport, worker_transport = in_process_transport_pair()
    host = Session[WorkerMessageUnion, ServerMessageUnion](
        host_transport, parse=parse_worker_input, make_ack=_server_ack, epoch=uuid4().hex,
    )
    worker = Session[ServerMessageUnion, WorkerMessageUnion](
        worker_transport, parse=parse_server_input, make_ack=_worker_ack,
    )
    connection = _LocalConnection(worker, inference)

    async def serve_host() -> None:
        try:
            register = parse_worker_message(await host_transport.receive_text())
            if not isinstance(register, Register) or register.worker_id != LOCAL_BUB_WORKER_ID:
                raise InferenceProtocolError("invalid local worker registration")
            host.register_control(register)
            host.start()
            await serve(LOCAL_BUB_WORKER_ID, register, host)
        except SessionClosed:
            pass  # Either endpoint can disconnect the local pair normally.
        finally:
            await host.close()

    async def serve_worker() -> None:
        try:
            await Gateway[ServerMessageUnion, WorkerMessageUnion]().serve(
                "server", worker, connection, replace=False,
            )
        except SessionClosed:
            pass
        finally:
            await connection.inference.aclose()

    async with asyncio.TaskGroup() as group:
        group.create_task(serve_host())
        group.create_task(serve_worker())
