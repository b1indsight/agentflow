"""Control-plane composition root."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any

from loguru import logger

from app.config import ServeSettings, get_serve_settings
from app.control.coordinator import JobCoordinator
from app.control.gateway import WorkerGateway
from app.control.reconcile import Reconciler
from app.control.reconcile_feedback import FeedbackReconciler
from app.control.router import EventRouter
from app.control.service import ControlService
from app.github_client import GitHubApp
from app.plugins import get_plugin_manager
from app.store import Store


class ControlRuntime:
    """Own all stateful control adapters; no execution adapter is constructed here."""

    def __init__(self, settings: ServeSettings | None = None) -> None:
        self.settings = settings or get_serve_settings()
        self.store = Store(
            self.settings.database_url,
            lease_duration=timedelta(seconds=self.settings.worker_lease_duration),
            recovery_grace=timedelta(seconds=self.settings.worker_recovery_grace),
        )
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
            recovery_concurrency=self.settings.result_recovery_concurrency,
            recovery_batch_size=self.settings.result_recovery_batch_size,
        )
        self.service = ControlService(self.store, plugins, self.gateway.notify_jobs)
        self.router = EventRouter(self.store, self.service, self.settings)
        self.reconciler = Reconciler(
            self.store, self.coordinator.github, self.service,
            interval=self.settings.reconcile_interval_seconds,
            batch_size=self.settings.reconcile_batch_size,
            concurrency=self.settings.reconcile_concurrency,
        )
        self.feedback_reconciler = FeedbackReconciler(
            self.store, self.coordinator.github, settle_seconds=self.settings.settle_seconds,
            bot_login=self.settings.bot_login, interval=self.settings.reconcile_interval_seconds,
            batch_size=self.settings.reconcile_batch_size, concurrency=self.settings.reconcile_concurrency,
        )
        self._tasks: list[asyncio.Task[None]] = []
        self._handlers: set[asyncio.Task[None]] = set()

    def startup(self) -> None:
        self.store.init_db()
        self.store.reviews.check_upgrade()
        self.store.results.recover_claims()
        requeued, failed = self.store.recover_abandoned_assignments()
        if requeued or failed:
            logger.warning(
                "recovered abandoned assignments: {} requeued, {} failed closed",
                requeued,
                failed,
            )
        self._tasks = [
            asyncio.create_task(self.reconciler.run()),
            asyncio.create_task(self.feedback_reconciler.run()),
            asyncio.create_task(self._sweep_assignments()),
            asyncio.create_task(self._sweep_results()),
            asyncio.create_task(self._sweep_reviews()),
        ]
        self.router.settle.sweep()

    async def _sweep_assignments(self) -> None:
        while True:
            await asyncio.sleep(self.settings.worker_sweep_interval)
            try:
                requeued, failed = self.store.sweep_expired_assignments()
                if requeued or failed:
                    logger.warning(
                        "expired worker assignments: {} requeued, {} failed",
                        requeued,
                        failed,
                    )
                    self.gateway.notify_jobs()
            except Exception:  # noqa: BLE001 -- keep periodic recovery alive after operational errors
                logger.exception("assignment recovery sweep failed; retrying next sweep")

    async def _sweep_results(self) -> None:
        """Recover immediately at startup; network waits never hold the local scheduling loops."""
        while True:
            try:
                await self.gateway.recover_pending_results()
            except Exception:  # noqa: BLE001 -- a failed inbox query is retried next sweep
                logger.exception("result recovery sweep failed; retrying next sweep")
            await asyncio.sleep(self.settings.worker_sweep_interval)

    async def _sweep_reviews(self) -> None:
        """Keep settle independent of both lease errors and slow remote receipt checks."""
        while True:
            await asyncio.sleep(self.settings.worker_sweep_interval)
            try:
                self.router.settle.sweep()
            except Exception:  # noqa: BLE001 -- a failed claim leaves durable deadlines to retry
                logger.exception("review settle sweep failed; retrying next sweep")

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
