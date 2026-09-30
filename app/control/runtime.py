"""Control-plane composition root."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from loguru import logger

from app.config import ServeSettings, get_serve_settings
from app.control.coordinator import JobCoordinator
from app.control.gateway import WorkerGateway
from app.control.projects import ProjectSync
from app.control.reconcile import Reconciler
from app.control.reconcile_feedback import FeedbackReconciler
from app.control.router import SettleScheduler
from app.control.service import ControlService
from app.control.webhooks import WebhookConsumer
from app.github_client import GitHubApp
from app.observability import Metrics, configure_logging
from app.plugins import get_plugin_manager
from app.readiness import database_ready
from app.stop_store import StopStore
from app.store import Store
from app.webhook_store import WebhookStore


class ControlRuntime:
    """Own all stateful control adapters; no execution adapter is constructed here."""

    def __init__(self, settings: ServeSettings | None = None) -> None:
        self.settings = settings or get_serve_settings()
        configure_logging("serve", self.settings.log_format)
        self.metrics = Metrics()
        self._ready = False
        self._readiness_lock = asyncio.Lock()
        self.store = Store(
            self.settings.database_url,
            lease_duration=timedelta(seconds=self.settings.worker_lease_duration),
            recovery_grace=timedelta(seconds=self.settings.worker_recovery_grace),
        )
        plugins = get_plugin_manager()
        github = GitHubApp(self.settings)
        self.coordinator = JobCoordinator(self.store, github, self.settings, plugins)
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
            metrics=self.metrics,
            execution_control=StopStore(self.store),
        )
        self.service = ControlService(self.store, plugins, self.gateway.notify_jobs)
        self.settle = SettleScheduler(self.store, self.service.notify_jobs)
        self.webhooks = WebhookConsumer(self.store, self.settings, self.service)
        self.reconciler = Reconciler(
            self.store, self.coordinator.github, self.service,
            settle_seconds=self.settings.settle_seconds,
            interval=self.settings.reconcile_interval_seconds,
            batch_size=self.settings.reconcile_batch_size,
            concurrency=self.settings.reconcile_concurrency,
        )
        self.feedback_reconciler = FeedbackReconciler(
            self.store, self.coordinator.github, settle_seconds=self.settings.settle_seconds,
            bot_login=self.settings.bot_login, interval=self.settings.reconcile_interval_seconds,
            batch_size=self.settings.reconcile_batch_size, concurrency=self.settings.reconcile_concurrency,
        )
        self.project_sync = ProjectSync(
            self.store, self.settings.projects, github.project_client,
            interval=self.settings.reconcile_interval_seconds, batch_size=self.settings.reconcile_batch_size,
            concurrency=self.settings.reconcile_concurrency,
        )
        self._tasks: list[asyncio.Task[None]] = []

    def startup(self) -> None:
        self.store.init_db()
        self.store.results.recover_claims()
        requeued, failed = self.store.recover_abandoned_assignments()
        if requeued or failed:
            logger.warning(
                "recovered abandoned assignments: {} requeued, {} failed closed",
                requeued,
                failed,
            )
        self._tasks = [
            asyncio.create_task(self.webhooks.run()),
            asyncio.create_task(self.project_sync.run()),
            asyncio.create_task(self.reconciler.run()),
            asyncio.create_task(self.feedback_reconciler.run()),
            asyncio.create_task(self._sweep_assignments()),
            asyncio.create_task(self._sweep_results()),
            asyncio.create_task(self._sweep_reviews()),
        ]
        if self.settings.metrics_token and self.settings.metrics_token.get_secret_value():
            self._tasks.append(asyncio.create_task(self.metrics.run(
                self.settings.database_url, self.settings.metrics_interval_seconds,
            )))
        self.settle.sweep()
        self._ready = True

    async def ready(self) -> bool:
        if not self._ready or self._readiness_lock.locked():
            return False
        async with self._readiness_lock:
            healthy = await database_ready(self.settings.database_url)
            return self._ready and healthy

    async def _sweep_assignments(self) -> None:
        while True:
            await asyncio.sleep(self.settings.worker_sweep_interval)
            try:
                requeued, failed = self.store.sweep_expired_assignments()
                self.metrics.expired.labels("requeued").inc(requeued)
                self.metrics.expired.labels("failed").inc(failed)
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
                self.settle.sweep()
            except Exception:  # noqa: BLE001 -- a failed claim leaves durable deadlines to retry
                logger.exception("review settle sweep failed; retrying next sweep")

    async def receive_webhook(self, event_type: str, delivery_id: str, payload: dict, digest: str) -> None:
        await asyncio.to_thread(WebhookStore(self.store).receive, delivery_id, event_type, payload, digest)
        self.webhooks.wakeup.set()

    async def shutdown(self) -> None:
        self._ready = False
        self.reconciler.stop()
        tasks = self._tasks
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()
