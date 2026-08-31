"""FastAPI endpoints for GitHub webhooks and the internal worker connection."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any, Protocol

from fastapi import FastAPI, Header, Request, Response, WebSocket
from loguru import logger


class WebhookSettings(Protocol):
    github_webhook_secret: str


class WebhookRouter(Protocol):
    async def handle(self, event_type: str, delivery_id: str, payload: dict) -> None: ...


class WorkerGateway(Protocol):
    async def handle(self, socket: WebSocket) -> None: ...


class ApplicationContext(Protocol):
    @property
    def settings(self) -> WebhookSettings: ...

    @property
    def router(self) -> WebhookRouter: ...

    @property
    def gateway(self) -> WorkerGateway: ...

    def startup(self) -> None: ...

    def spawn_handler(self, coroutine: Coroutine[Any, Any, None]) -> None: ...

    async def shutdown(self) -> None: ...


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _default_context() -> ApplicationContext:
    from app.control.runtime import ControlRuntime

    return ControlRuntime()


def create_app(
    context_factory: Callable[[], ApplicationContext] | None = None,
) -> FastAPI:
    factory = context_factory or _default_context

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        context = factory()
        application.state.ctx = context
        context.startup()
        host = getattr(context.settings, "host", "test")
        port = getattr(context.settings, "port", 0)
        logger.info("FlowTool control plane up on {}:{}", host, port)
        try:
            yield
        finally:
            await context.shutdown()

    application = FastAPI(title="FlowTool", lifespan=lifespan)

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/webhook")
    async def webhook(
        request: Request,
        x_github_event: str = Header(default=""),
        x_github_delivery: str = Header(default=""),
        x_hub_signature_256: str | None = Header(default=None),
    ) -> Response:
        context: ApplicationContext = request.app.state.ctx
        body = await request.body()
        if not verify_signature(
            context.settings.github_webhook_secret, body, x_hub_signature_256
        ):
            return Response(status_code=401, content="bad signature")
        payload = await request.json()
        context.spawn_handler(
            context.router.handle(x_github_event, x_github_delivery, payload)
        )
        return Response(status_code=202)

    @application.websocket("/internal/workers/connect")
    async def worker_connect(socket: WebSocket) -> None:
        context: ApplicationContext = socket.app.state.ctx
        await context.gateway.handle(socket)

    return application


app = create_app()
