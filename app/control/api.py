"""FastAPI endpoints for GitHub webhooks and the internal worker connection."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import FastAPI, Header, Request, Response, WebSocket
from loguru import logger
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from pydantic import SecretStr
from starlette.middleware.base import RequestResponseEndpoint

from app.persistence.webhook_store import DeliveryConflict
from app.telemetry.metrics import Metrics


class ApplicationSettings(Protocol):
    github_webhook_secret: str
    webhook_max_body_bytes: int
    metrics_token: SecretStr | None
    host: str
    port: int


class WorkerGateway(Protocol):
    async def handle(self, socket: WebSocket) -> None: ...


class ApplicationContext(Protocol):
    @property
    def settings(self) -> ApplicationSettings: ...

    @property
    def metrics(self) -> Metrics: ...

    @property
    def gateway(self) -> WorkerGateway: ...

    async def ready(self) -> bool: ...

    def startup(self) -> None: ...

    async def receive_webhook(self, event_type: str, delivery_id: str, payload: dict, digest: str) -> None: ...

    async def shutdown(self) -> None: ...


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature_header):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


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
        application.state.metrics = context.metrics
        context.startup()
        logger.info("FlowTool control plane up on {}:{}", context.settings.host, context.settings.port)
        try:
            yield
        finally:
            await context.shutdown()

    application = FastAPI(title="FlowTool", lifespan=lifespan)

    @application.middleware("http")
    async def observe_request(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if request.url.path == "/webhook":
            request.app.state.metrics.request(response.status_code)
            logger.bind(event="webhook_received", reason_code="ok" if response.status_code == 202 else "unspecified",
                        delivery_id=request.headers.get("x-github-delivery", "")).info("")
        return response

    @application.get("/metrics")
    async def metrics(request: Request) -> Response:
        context: ApplicationContext = request.app.state.ctx
        token = context.settings.metrics_token
        if token is None or not token.get_secret_value():
            return Response(status_code=404)
        expected = "Bearer " + token.get_secret_value()
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
            return Response(status_code=401)
        return Response(request.app.state.metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @application.get("/readyz")
    async def readyz(request: Request) -> Response:
        context: ApplicationContext = request.app.state.ctx
        ready = await context.ready()
        return Response(status_code=200 if ready else 503, content="ready" if ready else "not ready")

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
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > context.settings.webhook_max_body_bytes:
                return Response(status_code=413)
            body.extend(chunk)
        if not verify_signature(
            context.settings.github_webhook_secret, bytes(body), x_hub_signature_256
        ):
            return Response(status_code=401, content="bad signature")
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", x_github_delivery)
                or not re.fullmatch(r"[a-z_]{1,100}", x_github_event)):
            return Response(status_code=400)
        try:
            payload = json.loads(body, parse_constant=_reject_json_constant)
        except (ValueError, UnicodeError, RecursionError):
            return Response(status_code=400)
        if not isinstance(payload, dict):
            return Response(status_code=400)
        try:
            await context.receive_webhook(
                x_github_event, x_github_delivery, payload, hashlib.sha256(body).hexdigest(),
            )
        except DeliveryConflict:
            return Response(status_code=409)
        except (DatabaseError, PoolTimeout):
            logger.warning("webhook receipt unavailable")
            return Response(status_code=503)
        return Response(status_code=202)

    @application.websocket("/internal/workers/connect")
    async def worker_connect(socket: WebSocket) -> None:
        context: ApplicationContext = socket.app.state.ctx
        await context.gateway.handle(socket)

    return application


app = create_app()
