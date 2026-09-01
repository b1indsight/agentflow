"""Control-plane composition root."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from loguru import logger

from app.config import ServeSettings, get_serve_settings
from app.control.coordinator import JobCoordinator
from app.control.gateway import WorkerGateway
from app.control.reconcile import Reconciler
from app.control.router import EventRouter
from app.control.service import ControlService
from app.github_client import GitHubApp
from app.plugins import get_plugin_manager
from app.store import Store


class ControlRuntime:
    """Own all stateful control adapters; no execution adapter is constructed here."""

    def __init__(self, settings: ServeSettings | None = None) -> None:
        self.settings = settings or get_serve_settings()
        self.store = Store(self.settings.database_url)
        plugins = get_plugin_manager()
        self.coordinator = JobCoordinator(
            self.store, GitHubApp(self.settings), self.settings, plugins
        )
        self.gateway = WorkerGateway(
            self.store,
            self.coordinator,
            worker_tokens={
                worker_id: token.get_secret_value()
                for worker_id, token in self.settings.worker_tokens.items()
            },
            heartbeat_interval=self.settings.worker_heartbeat_interval,
            heartbeat_timeout=self.settings.worker_heartbeat_timeout,
            poll_interval=self.settings.worker_poll_interval,
        )
        self.service = ControlService(self.store, plugins, self.gateway.notify_jobs)
        self.router = EventRouter(self.store, self.service, self.settings)
        self.reconciler = Reconciler(self.store)
        self._tasks: list[asyncio.Task[None]] = []
        self._handlers: set[asyncio.Task[None]] = set()

    def startup(self) -> None:
        self.store.init_db()
        requeued, failed = self.store.recover_abandoned_assignments()
        if requeued or failed:
            logger.warning(
                "recovered abandoned assignments: {} requeued, {} failed closed",
                requeued,
                failed,
            )
        self._tasks = [asyncio.create_task(self.reconciler.run())]

    def spawn_handler(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._handlers.add(task)
        task.add_done_callback(self._on_handler_done)

    def _on_handler_done(self, task: asyncio.Task[None]) -> None:
        self._handlers.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.opt(exception=exception).error("webhook handler crashed")

    async def shutdown(self) -> None:
        self.reconciler.stop()
        tasks = [*self._tasks, *self._handlers]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()
