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

KEEP_ALIVE_SECONDS = 15


async def _frames(runtime: ServerRuntime, read: Callable[[int], FlowChange], last_id: str | None) -> AsyncGenerator[str]:
    async for identity, event in runtime.flow_events.after(last_id):
        if event is None:
            yield f"id: {identity}\nevent: ready\ndata: {{}}\n\n"
            continue
        data = {"flow_id": event.flow_id, "payload": event.payload}
        if event.event_type == "flow.job.finished.v1":
            board = await runtime.console.db(partial(read, event.flow_id))
            data["board"] = board.model_dump(mode="json")
        yield f"id: {identity}\nevent: {event.event_type}\ndata: {json.dumps(data)}\n\n"


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
