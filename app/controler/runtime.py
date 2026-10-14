"""Control-plane composition root."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from loguru import logger

from app.controler.config import ServerSettings
from app.controler.events.router import EventRouter
from app.controler.events.webhooks import WebhookConsumer
from app.controler.reconciliation.feedback import FeedbackReconciler
from app.controler.reconciliation.projects import ProjectSync
from app.controler.reconciliation.pull_requests import Reconciler
from app.controler.review.judge import BubJudge
from app.controler.workers.conversation import ConversationBroker, ConversationService
from app.controler.workers.gateway import WorkerGateway
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.effects import ResultEffects
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.service import ControlService
from app.controler.workflow.tools import FlowToolService
from app.github import GitHubApp
from app.persistence.database import Database
from app.persistence.readiness import database_ready
from app.persistence.results import ResultStore
from app.persistence.reviews import ReviewStore
from app.persistence.webhooks import WebhookStore
from app.persistence.workers import WorkerStore
from app.plugins import get_plugin_manager
from app.telemetry.logging import configure_logging
from app.telemetry.metrics import Metrics


class ServerRuntime:
    """Own control adapters, local review decisions and background recovery."""

    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        configure_logging("serve", self.settings.log_format)
        self.metrics = Metrics()
        self._ready = False
        self._readiness_lock = asyncio.Lock()
        database = Database(
            self.settings.database_url,
            lease_duration=timedelta(seconds=self.settings.worker_lease_duration),
            recovery_grace=timedelta(seconds=self.settings.worker_recovery_grace),
        )
        plugins = get_plugin_manager()
        github = GitHubApp(self.settings)
        effects = ResultEffects(github, database=database)
        self.results = ResultApplication(
            github,
            self.settings,
            plugins,
            effects=effects,
            database=database,
        )
        tools = FlowToolService(github, database=database)
        conversations = ConversationBroker()
        self.coordinator = JobCoordinator(
            github,
            self.settings,
            plugins,
            database=database,
            results=self.results,
        )
        self.gateway = WorkerGateway(
            self.coordinator,
            database=database,
            tools=tools,
            results=self.results,
            conversations=conversations,
            worker_tokens={
                worker_id: token.get_secret_value()
                for worker_id, token in self.settings.worker_tokens.items()
            },
            heartbeat_interval=self.settings.worker_heartbeat_interval,
            heartbeat_timeout=self.settings.worker_heartbeat_timeout,
            poll_interval=self.settings.worker_poll_interval,
            metrics=self.metrics,
        )
        self.judge = BubJudge(model=settings.judge_model, api_key=settings.judge_api_key,
                         api_base=settings.judge_api_base, timeout=settings.judge_timeout_seconds)
        self._triage_task: asyncio.Task[None] | None = None
        self.service = ControlService(
            plugins,
            self.gateway.notify_jobs,
            database=database,
        )
        self.conversations = ConversationService(conversations, database=database)
        router = EventRouter(
            self.service,
            self.settings,
            database=database,
        )
        self.webhooks = WebhookConsumer(
            router,
            database=database,
        )
        self.reconciler = Reconciler(
            github,
            self.service,
            settle_seconds=self.settings.settle_seconds,
            interval=self.settings.reconcile_interval_seconds,
            batch_size=self.settings.reconcile_batch_size,
            concurrency=self.settings.reconcile_concurrency,
            database=database,
        )
        self.feedback_reconciler = FeedbackReconciler(
            github,
            settle_seconds=self.settings.settle_seconds,
            bot_login=self.settings.bot_login,
            interval=self.settings.reconcile_interval_seconds,
            batch_size=self.settings.reconcile_batch_size,
            concurrency=self.settings.reconcile_concurrency,
            database=database,
        )
        self.project_sync = ProjectSync(
            self.settings.projects,
            github,
            interval=self.settings.reconcile_interval_seconds,
            batch_size=self.settings.reconcile_batch_size,
            concurrency=self.settings.reconcile_concurrency,
            database=database,
        )
        self.database = database
        self.workers = WorkerStore(database)
        self.reviews = ReviewStore(database)
        self.result_store = ResultStore(database)
        self.webhook_store = WebhookStore(database)
        self._tasks: list[asyncio.Task[None]] = []

    def startup(self) -> None:
        self.database.init_db()
        self.result_store.recover_claims()
        self.workers.recover_abandoned_assignments()
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
        self._settle_reviews()
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
                requeued, failed = self.workers.sweep_expired_assignments()
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
                await self.results.recover_pending_results(
                    concurrency=self.settings.result_recovery_concurrency,
                    batch_size=self.settings.result_recovery_batch_size,
                )
                self.gateway.notify_jobs()
            except Exception:  # noqa: BLE001 -- a failed inbox query is retried next sweep
                logger.exception("result recovery sweep failed; retrying next sweep")
            await asyncio.sleep(self.settings.worker_sweep_interval)

    def _settle_reviews(self) -> None:
        if self.reviews.claim_due():
            self.gateway.notify_jobs()

        if self._triage_task is None or self._triage_task.done():
            self._triage_task = asyncio.create_task(self._run_triage())

    async def _run_triage(self) -> None:
        try:
            batches = self.reviews.pending_triage()
        except Exception:  # noqa: BLE001 -- retry a temporarily unavailable database next sweep
            logger.exception("could not read pending review batches")
            return
        for batch in batches:
            try:
                await self.coordinator.triage(batch, self.judge)
            except Exception:  # noqa: BLE001 -- isolate unavailable providers/feedback batches
                logger.exception("review prerequisite interrupted; unfinished batch will be retried")
        self.gateway.notify_jobs()

    async def _sweep_reviews(self) -> None:
        """Keep settle independent of both lease errors and slow remote receipt checks."""
        while True:
            await asyncio.sleep(self.settings.worker_sweep_interval)
            try:
                self._settle_reviews()
            except Exception:  # noqa: BLE001 -- a failed claim leaves durable deadlines to retry
                logger.exception("review settle sweep failed; retrying next sweep")

    async def receive_webhook(self, event_type: str, delivery_id: str, payload: dict, digest: str) -> None:
        await asyncio.to_thread(self.webhook_store.receive, delivery_id, event_type, payload, digest)
        self.webhooks.wakeup.set()

    async def shutdown(self) -> None:
        self._ready = False
        self.reconciler.stop()
        tasks = list(self._tasks)
        if self._triage_task is not None:
            tasks.append(self._triage_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.database.close()
