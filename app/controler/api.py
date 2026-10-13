"""FastAPI endpoints for GitHub webhooks and the internal worker connection."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, Header, HTTPException, Path, Query, Request, Response, WebSocket
from loguru import logger
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from starlette.middleware.base import RequestResponseEndpoint

from app.contracts.conversation import ConversationFailure, ConversationPage
from app.controler.config import ServerSettings
from app.controler.runtime import ServerRuntime
from app.persistence.webhooks import DeliveryConflict


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature_header):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


async def _wait_for_disconnect(request: Request, cancel_scope: anyio.CancelScope) -> None:
    """Consume the GET request stream until the caller disconnects."""
    with cancel_scope:
        while (await request.receive())["type"] != "http.disconnect":
            pass


def create_app(runtime: ServerRuntime | None = None) -> FastAPI:
    """Create the HTTP app; construct the default runtime when its lifespan starts."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        active_runtime = runtime if runtime is not None else ServerRuntime(ServerSettings())  # type: ignore[call-arg]
        application.state.runtime = active_runtime
        application.state.metrics = active_runtime.metrics
        active_runtime.startup()
        logger.info("FlowTool control plane up on {}:{}", active_runtime.settings.host, active_runtime.settings.port)
        try:
            yield
        finally:
            await active_runtime.shutdown()

    application = FastAPI(title="FlowTool", lifespan=lifespan)

    @application.middleware("http")
    async def observe_request(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if request.url.path == "/webhook":
            request.app.state.metrics.request(response.status_code)
            logger.bind(event="webhook_received", reason_code="ok" if response.status_code == 202 else "unspecified",
                        delivery_id=request.headers.get("x-github-delivery", "")).info("")
        return response

    @application.get("/api/flows/{flow_id}/conversation", response_model=ConversationPage)
    async def conversation(
        request: Request,
        response: Response,
        flow_id: int = Path(gt=0),
        cursor: str | None = Query(default=None, min_length=1, max_length=2048),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> ConversationPage | Response:
        runtime: ServerRuntime = request.app.state.runtime
        token = runtime.settings.control_api_token
        headers = {"Cache-Control": "no-store"}
        if token is None or not token.get_secret_value():
            raise HTTPException(503, "conversation_api_disabled", headers=headers)
        expected = "Bearer " + token.get_secret_value()
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
            raise HTTPException(401, "unauthorized", headers={**headers, "WWW-Authenticate": "Bearer"})
        query = asyncio.create_task(runtime.conversations.read(flow_id, cursor, limit))
        # Starlette's receive middleware uses AnyIO scopes; level cancellation is
        # needed so cancelling the watcher cannot be swallowed by its nested scope.
        disconnect_scope = anyio.CancelScope()
        disconnected = asyncio.create_task(_wait_for_disconnect(request, disconnect_scope))
        try:
            await asyncio.wait({query, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if not query.done():
                return Response(status_code=499, headers=headers)
            response.headers.update(headers)
            return query.result()
        except ConversationFailure as exc:
            status = {"flow_not_found": 404, "worker_unavailable": 503, "query_timeout": 504,
                      "query_busy": 503, "cursor_invalid": 409}.get(exc.code, 502)
            raise HTTPException(status, exc.code, headers=headers) from exc
        except (DatabaseError, PoolTimeout) as exc:
            raise HTTPException(503, "database_unavailable", headers=headers) from exc
        finally:
            query.cancel()
            disconnect_scope.cancel()
            await asyncio.gather(query, disconnected, return_exceptions=True)

    @application.get("/metrics")
    async def metrics(request: Request) -> Response:
        runtime: ServerRuntime = request.app.state.runtime
        token = runtime.settings.metrics_token
        if token is None or not token.get_secret_value():
            return Response(status_code=404)
        expected = "Bearer " + token.get_secret_value()
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
            return Response(status_code=401)
        return Response(request.app.state.metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @application.get("/readyz")
    async def readyz(request: Request) -> Response:
        runtime: ServerRuntime = request.app.state.runtime
        ready = await runtime.ready()
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
        runtime: ServerRuntime = request.app.state.runtime
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > runtime.settings.webhook_max_body_bytes:
                return Response(status_code=413)
            body.extend(chunk)
        if not verify_signature(
            runtime.settings.github_webhook_secret, bytes(body), x_hub_signature_256
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
            await runtime.receive_webhook(
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
        runtime: ServerRuntime = socket.app.state.runtime
        await runtime.gateway.handle(socket)

    return application


app = create_app()
