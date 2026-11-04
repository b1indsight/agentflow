"""Outbound WebSocket dialing and reconnect policy, independent of worker business."""

import asyncio
import random
from collections.abc import Callable, Coroutine
from typing import Protocol

from loguru import logger
from websockets.asyncio.client import connect

from app.communication.session import Transport


class Connection(Protocol):
    async def send(self, data: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self, code: int = 1000) -> None: ...


class WebSocketTransport:
    """Map outbound WebSocket IO to the transport shared with the server."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    async def send_text(self, data: str) -> None:
        await self.connection.send(data)

    async def receive_text(self) -> str:
        raw = await self.connection.recv()
        return raw.decode() if isinstance(raw, bytes) else raw

    async def close(self, code: int = 1000) -> None:
        await self.connection.close(code)


class WorkerClient:
    """Dial with authentication and join the injected connection driver on shutdown."""

    def __init__(
        self,
        *,
        worker_id: str,
        worker_token: str,
        serve_ws_url: str,
        serve: Callable[[Transport], Coroutine[object, object, None]],
        reconnect_min_backoff: float = 1.0,
        reconnect_max_backoff: float = 30.0,
        reconnect_jitter: float = 0.2,
    ) -> None:
        self.worker_id, self.worker_token, self.serve_ws_url = worker_id, worker_token, serve_ws_url
        self.serve = serve
        self.reconnect_min_backoff = reconnect_min_backoff
        self.reconnect_max_backoff = reconnect_max_backoff
        self.reconnect_jitter = reconnect_jitter
        self._stop = asyncio.Event()
        self._connection_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        delay = self.reconnect_min_backoff
        try:
            while not self._stop.is_set():
                try:
                    async with connect(
                        self.serve_ws_url,
                        additional_headers={
                            "Authorization": f"Bearer {self.worker_token}",
                            "X-FlowTool-Worker-ID": self.worker_id,
                        },
                    ) as connection:
                        if self._stop.is_set():
                            break
                        delay = self.reconnect_min_backoff
                        self._connection_task = asyncio.create_task(
                            self.serve(WebSocketTransport(connection))
                        )
                        await self._connection_task
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 — reconnect after protocol/transport loss.
                    jitter = random.uniform(1 - self.reconnect_jitter, 1 + self.reconnect_jitter)
                    wait = delay * jitter
                    logger.warning("worker connection lost; retrying in {:.2f}s: {}", wait, error)
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=wait)
                    except TimeoutError:
                        delay = min(delay * 2, self.reconnect_max_backoff)
        finally:
            if self._connection_task is not None:
                self._connection_task.cancel()
                await asyncio.gather(self._connection_task, return_exceptions=True)
                self._connection_task = None

    def stop(self) -> None:
        self._stop.set()
        if self._connection_task is not None:
            self._connection_task.cancel()
