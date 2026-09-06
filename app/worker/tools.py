"""Worker-local MCP broker with no control-plane or GitHub credentials."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol, Self
from uuid import uuid4

from app.models import VcsExecutionContext, VcsOutcome
from app.vcs import verify_pushed_outcome


class FlowToolCaller(Protocol):
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        call_id: str | None = None,
        vcs_outcome: VcsOutcome,
    ) -> dict[str, object]: ...


class FlowToolBroker:
    """Expose one assignment-scoped Unix socket to the STDIO MCP subprocess."""

    def __init__(self, caller: FlowToolCaller, vcs_context: VcsExecutionContext) -> None:
        self.caller = caller
        self.vcs_context = vcs_context
        self.socket_path = Path(tempfile.gettempdir()) / f"flowtool-mcp-{uuid4().hex}.sock"
        self.last_call_id: str | None = None
        self.last_result: dict[str, object] | None = None
        self.last_vcs_outcome: VcsOutcome | None = None
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=self.socket_path
        )
        self.socket_path.chmod(0o600)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self.socket_path.unlink(missing_ok=True)

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await reader.readline()
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise TypeError("worker-local Flow tool request must be an object")
            call_id = request.get("call_id")
            name = request.get("name")
            arguments = request.get("arguments")
            if (
                not isinstance(call_id, str)
                or name != "flow.open_pr"
                or not isinstance(arguments, dict)
            ):
                raise ValueError("invalid worker-local Flow tool request")
            outcome = await verify_pushed_outcome(self.vcs_context)
            result = await self.caller.call_tool(
                name, arguments, call_id=call_id, vcs_outcome=outcome
            )
            self.last_call_id = call_id
            self.last_result = result
            self.last_vcs_outcome = outcome
            response: dict[str, object] = {"result": result}
        except (TypeError, ValueError, RuntimeError) as exc:
            response = {"error": str(exc)}
        writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()


@asynccontextmanager
async def flow_tool_broker(
    caller: FlowToolCaller, vcs_context: VcsExecutionContext
) -> AsyncIterator[FlowToolBroker]:
    async with FlowToolBroker(caller, vcs_context) as broker:
        yield broker


def _bridge_call(name: str, arguments: dict[str, object]) -> dict[str, object]:
    socket_path = os.environ.get("FLOWTOOL_MCP_SOCKET")
    if not socket_path:
        raise RuntimeError("Flow tool broker socket is unavailable")
    request = {
        "call_id": uuid4().hex,
        "name": f"flow.{name}",
        "arguments": arguments,
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        stream = client.makefile("rwb")
        stream.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
        stream.flush()
        response = json.loads(stream.readline())
    if not isinstance(response, dict):
        raise TypeError("Flow tool broker returned an invalid response")
    if response.get("error") is not None:
        raise RuntimeError(str(response["error"]))
    result = response.get("result")
    if not isinstance(result, dict):
        raise TypeError("Flow tool broker returned an invalid result")
    return result


def _mcp_result(request: dict[str, object]) -> dict[str, object] | None:
    request_id = request.get("id")
    method = request.get("method")
    if request_id is None:
        return None
    if method == "initialize":
        params = request.get("params")
        protocol_version = (
            params.get("protocolVersion", "2025-06-18")
            if isinstance(params, dict)
            else "2025-06-18"
        )
        result: dict[str, object] = {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "flowtool", "version": "0.0.1"},
            "instructions": "Call open_pr only after committing and pushing the Flow branch.",
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "open_pr",
                    "description": "Verify the pushed Flow branch and open or reuse its pull request.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["title", "body"],
                        "additionalProperties": False,
                    },
                }
            ]
        }
    elif method == "tools/call":
        params = request.get("params")
        if not isinstance(params, dict) or params.get("name") != "open_pr":
            raise ValueError("unknown Flow tool")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("Flow tool arguments must be an object")
        tool_result = _bridge_call("open_pr", arguments)
        result = {
            "content": [{"type": "text", "text": json.dumps(tool_result)}],
            "structuredContent": tool_result,
        }
    else:
        raise ValueError(f"unsupported MCP method: {method}")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> None:
    """Run the minimal newline-delimited STDIO MCP server used by Codex."""

    for line in sys.stdin:
        request: object = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise TypeError("MCP request must be an object")
            response = _mcp_result(request)
        except (json.JSONDecodeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"code": -32602, "message": str(exc)},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
