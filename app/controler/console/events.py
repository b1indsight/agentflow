"""Replayable owner-console SSE. PostgreSQL notifications only wake the log reader."""

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from functools import partial
from time import time
from uuid import UUID

import anyio
from psycopg import AsyncConnection

from app.contracts.console import FlowChange
from app.controler.runtime import ServerRuntime

KEEP_ALIVE_SECONDS = 15


def _event_id(ms: int, seq: int, server: UUID) -> str:
    return f"{ms:013d}-{seq:019d}-{server}"


async def _frames(runtime: ServerRuntime, read: Callable[[int], FlowChange], last_id: str | None) -> AsyncGenerator[str]:
    # A long-lived tab must not reserve one of the four pooled query connections.
    listener = await AsyncConnection.connect(runtime.database.database_url, autocommit=True)
    try:
        await listener.execute("LISTEN agentflow_flow")
        clock = await (await listener.execute(
            "SELECT current_schema(),server_id,seq,timestamp_ms FROM console_event_clock"
        )).fetchone()
        assert clock is not None
        schema, server, head, ms = clock
        cursor = None
        if last_id == _event_id(0, 0, server):
            cursor = 0  # A new installation has no log row for its first ready frame.
        elif match := re.fullmatch(r"([0-9]{13})-([0-9]{19})-([0-9a-f-]{36})", last_id or ""):
            stamp, number, origin = match.groups()
            seq = int(number)
            if origin == str(server) and seq <= head and await (await listener.execute(
                "SELECT 1 FROM console_flow_events WHERE seq=%s AND timestamp_ms=%s", (seq, int(stamp))
            )).fetchone():
                cursor = seq
        if cursor is None:
            cursor = head
            yield f"id: {_event_id(ms, head, server)}\nevent: ready\ndata: {{}}\n\n"
        while True:
            rows = await (await listener.execute(
                "SELECT seq,timestamp_ms,event_type,flow_id,payload FROM console_flow_events"
                " WHERE seq>%s ORDER BY seq LIMIT 64", (cursor,)
            )).fetchall()
            for cursor, stamp_ms, event_type, flow_id, payload in rows:
                data = {"flow_id": flow_id, "payload": payload}
                if event_type == "flow.job.finished.v1":
                    board = await runtime.console.db(partial(read, flow_id))
                    data["board"] = board.model_dump(mode="json")
                yield f"id: {_event_id(stamp_ms, cursor, server)}\nevent: {event_type}\ndata: {json.dumps(data)}\n\n"
            if rows:
                continue
            async with aclosing(listener.notifies()) as notices:
                async for notice in notices:
                    if notice.payload == schema:
                        break
    finally:
        await listener.close()


async def flow_events(runtime: ServerRuntime, read: Callable[[int], FlowChange], expires_at: int,
                      last_id: str | None) -> AsyncGenerator[str]:
    """Keep heartbeats and credential expiry independent of idle, busy or replaying readers."""
    loop = asyncio.get_running_loop()
    heartbeat = loop.time() + KEEP_ALIVE_SECONDS
    async with aclosing(_frames(runtime, read, last_id)) as frames:
        pending = asyncio.ensure_future(anext(frames))
        try:
            while (remaining := expires_at - time()) > 0:
                if loop.time() >= heartbeat:
                    heartbeat = loop.time() + KEEP_ALIVE_SECONDS
                    yield ": keep-alive\n\n"
                elif pending.done():
                    yield pending.result()
                    pending = asyncio.ensure_future(anext(frames))
                else:
                    await asyncio.wait({pending}, timeout=min(heartbeat - loop.time(), remaining))
        finally:
            # Starlette cancels the response on disconnect. Drain the active read
            # before closing its generator, even inside that cancelled scope.
            with anyio.CancelScope(shield=True):
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
