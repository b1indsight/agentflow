"""Replayable owner-console SSE, woken by committed application events."""

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from functools import partial
from time import time

import anyio

from app.contracts.console import FlowChange
from app.controler.runtime import ServerRuntime
from app.persistence.flow_events import event_id

KEEP_ALIVE_SECONDS = 15


async def _frames(runtime: ServerRuntime, read: Callable[[int], FlowChange], last_id: str | None) -> AsyncGenerator[str]:
    log = runtime.database.flow_events
    with log.subscribe() as changed:
        server, cursor, watermark = await runtime.console.db(partial(log.resume, last_id))
        if watermark is not None:
            yield f"id: {watermark}\nevent: ready\ndata: {{}}\n\n"
        while True:
            changed.clear()  # A commit during or after this read leaves the flag set.
            rows = await runtime.console.db(partial(log.after, cursor))
            for row in rows:
                cursor = row["seq"]
                identity = event_id(row["timestamp_ms"], cursor, server)
                data = {"flow_id": row["flow_id"], "payload": row["payload"]}
                if row["event_type"] == "flow.job.finished.v1":
                    board = await runtime.console.db(partial(read, row["flow_id"]))
                    data["board"] = board.model_dump(mode="json")
                yield f"id: {identity}\nevent: {row['event_type']}\ndata: {json.dumps(data)}\n\n"
            if not rows:
                await changed.wait()


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
