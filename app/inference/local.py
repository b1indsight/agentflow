"""Connect a controller-managed inference provider to the normal worker session driver."""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

from loguru import logger
from pydantic import SecretStr

from app.communication.inprocess import in_process_transport_pair
from app.communication.session import Session, SessionClosed
from app.contracts.protocol import (
    Register,
    ServerMessage,
    ServerMessageUnion,
    WorkerMessageUnion,
    message_header,
    parse_worker_message,
)
from app.contracts.validation import parse_worker_input
from app.domain.inference_models import LOCAL_BUB_WORKER_ID
from app.inference.transport import InferenceProtocolError
from app.worker.bub import BubCodingAgent
from app.worker.coding_agent import CodingAgent
from app.worker.config import WorkerSettings
from app.worker.runtime import WorkerRuntime

type ServeSession = Callable[[str, Register, Session[WorkerMessageUnion, ServerMessageUnion]], Awaitable[None]]


def start_local_bub(
    serve: ServeSession, *, model: str, api_key: SecretStr | None, api_base: str | None,
    reconnect_interval: float,
) -> asyncio.Task[None] | None:
    """Start a configured infer-only peer; the controller owns and joins the returned task."""
    agent = BubCodingAgent(model=model, api_key=api_key, api_base=api_base)
    if not agent.configured:
        return None
    if agent.configuration_failure() is not None:
        logger.warning("local bub worker unavailable: configuration preflight failed")
        return None

    async def supervise() -> None:
        while True:
            try:
                await serve_local_inference(serve, agent)
            except Exception:  # noqa: BLE001 -- restart the local connection, not a model attempt
                logger.exception("local bub worker disconnected; reconnecting")
            await asyncio.sleep(reconnect_interval)

    return asyncio.create_task(supervise())


def _server_ack(message_id: str) -> ServerMessage:
    # Same directional receipt as controller transport, without importing controller ownership.
    return ServerMessage(
        **(message_header(None) | {"ack": True, "ack_id": message_id}),
        control=None, payload=[],
    )


async def serve_local_inference(serve: ServeSession, agent: CodingAgent) -> None:
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
    worker = WorkerRuntime(WorkerSettings(worker_id=LOCAL_BUB_WORKER_ID, serve_ws_url=None, execute=False), agent=agent)

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

    async with asyncio.TaskGroup() as group:
        group.create_task(serve_host())
        group.create_task(worker.run(worker_transport))
