"""Assignment-scoped MCP endpoint hosted in the worker process."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import socket
from collections.abc import Generator
from contextlib import contextmanager
from typing import Protocol, Self
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import SecretStr

from app.domain.delivery_models import DeliveryReceipt
from app.domain.models import VcsExecutionContext, VcsOutcome
from app.worker.execution.vcs import verify_pushed_outcome


class FlowToolCaller(Protocol):
    async def call_tool(
        self, name: str, arguments: dict[str, object], *,
        call_id: str | None = None, vcs_outcome: VcsOutcome,
    ) -> DeliveryReceipt: ...


class _EmbeddedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        # WorkerRuntime owns signals for the entire worker, including Codex cleanup.
        yield


class WorkerMCP:
    """Serve only open_pr for one coding invocation; revoke access when it finishes."""

    def __init__(self, caller: FlowToolCaller, vcs_context: VcsExecutionContext) -> None:
        self.caller = caller
        self.vcs_context = vcs_context
        self.token = SecretStr(secrets.token_urlsafe(32))
        self.url = ""
        self.last_call_id: str | None = None
        self.last_result: DeliveryReceipt | None = None
        self.last_vcs_outcome: VcsOutcome | None = None
        app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
        app.add_api_route("/mcp", self._handle, methods=["POST", "GET", "DELETE"])
        self._server = _EmbeddedServer(uvicorn.Config(
            app, log_config=None, access_log=False, lifespan="off", ws="none",
            timeout_graceful_shutdown=0,
        ))
        self._task: asyncio.Task[None] | None = None
        self._socket: socket.socket | None = None

    async def __aenter__(self) -> Self:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._socket.bind(("127.0.0.1", 0))
            self.url = f"http://127.0.0.1:{self._socket.getsockname()[1]}/mcp"
            self._task = asyncio.create_task(self._server.serve(sockets=[self._socket]))
            async with asyncio.timeout(5):
                while not self._server.started:
                    if self._task.done():
                        await self._task
                        raise RuntimeError("worker MCP server did not start")
                    await asyncio.sleep(0.01)
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task
        if self._socket is not None:
            self._socket.close()

    async def _handle(self, request: Request) -> Response:
        origin = self.url.removesuffix("/mcp")
        if (request.headers.get("host") != origin.removeprefix("http://")
                or request.headers.get("origin") not in (None, origin)):
            return Response(status_code=403)
        if not hmac.compare_digest(request.headers.get("authorization", ""),
                                   f"Bearer {self.token.get_secret_value()}"):
            return Response(status_code=401)
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        if request.headers.get("mcp-protocol-version", "2025-03-26") not in {
            "2025-03-26", "2025-06-18", "2025-11-25",
        }:
            return Response(status_code=400)
        request_id: str | int | None = None
        try:
            data = await request.json()
            if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
                raise ValueError("expected a JSON-RPC 2.0 message")
            request_id = data.get("id")
            if request_id is None:
                return Response(status_code=202)
            if type(request_id) not in (str, int):
                request_id = None
                raise ValueError("invalid request ID")
            result = await self._dispatch(data)
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"jsonrpc": "2.0", "id": request_id,
                                 "error": {"code": -32602, "message": str(exc)}})

    async def _dispatch(self, request: dict) -> dict:
        method = request.get("method")
        if method == "initialize":
            return {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "flowtool", "version": "0.0.1"},
                "instructions": "Call open_pr only after committing and pushing the Flow branch.",
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [{
                "name": "open_pr",
                "description": "Verify the pushed Flow branch and open or reuse its pull request.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                    "required": ["title", "body"], "additionalProperties": False,
                },
            }]}
        params = request.get("params")
        if method != "tools/call" or not isinstance(params, dict) or params.get("name") != "open_pr":
            raise ValueError("unknown MCP method or tool")
        arguments = params.get("arguments")
        if (not isinstance(arguments, dict) or set(arguments) != {"title", "body"}
                or any(not isinstance(value, str) for value in arguments.values())):
            raise ValueError("open_pr requires title and body strings")
        try:
            outcome = await verify_pushed_outcome(self.vcs_context)
            call_id = uuid4().hex
            result = await self.caller.call_tool("flow.open_pr", arguments, call_id=call_id, vcs_outcome=outcome)
            self.last_call_id, self.last_result, self.last_vcs_outcome = call_id, result, outcome
            payload = result.model_dump(mode="json")
            return {"content": [{"type": "text", "text": json.dumps(payload)}], "structuredContent": payload}
        except (RuntimeError, ValueError) as exc:
            return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
