"""Replay durable webhook receipts through one transaction-bound router."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial

from loguru import logger
from pydantic import ValidationError

from app.controler.config import ServerSettings
from app.controler.events.router import EventRouter
from app.controler.workflow.service import ControlService
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.reconciliation import ReconcileStore
from app.persistence.results import ResultStore
from app.persistence.reviews import ReviewStore
from app.persistence.stops import StopStore
from app.persistence.webhooks import WebhookStore
from app.persistence.workers import WorkerStore


class WebhookConsumer:
    def __init__(
        self,
        settings: ServerSettings,
        service: ControlService,
        *,
        database: Database,
        deliveries: DeliveryStore,
        flows: FlowStore,
        jobs: JobStore,
        reconcile_store: ReconcileStore,
        result_store: ResultStore,
        reviews: ReviewStore,
        stops: StopStore,
        webhook_store: WebhookStore,
        workers: WorkerStore,
    ) -> None:
        self.database = database
        self.deliveries = deliveries
        self.flows = flows
        self.jobs = jobs
        self.reconcile_store = reconcile_store
        self.result_store = result_store
        self.reviews = reviews
        self.stops = stops
        self.webhook_store = webhook_store
        self.workers = workers
        self.settings = settings
        self.service = service
        self.wakeup = asyncio.Event()

    def sweep(self, loop: asyncio.AbstractEventLoop | None = None) -> int:
        inbox = self.webhook_store
        handled = 0
        for _ in range(50):
            item = inbox.claim()
            if item is None:
                break
            callbacks: list[Callable[[], None]] = []
            try:
                if item.attempts > 10:
                    inbox.fail(item, "AttemptsExhausted", permanent=True)
                    continue
                with self.database.transaction() as tx:
                    deliveries_tx = self.deliveries.using(tx)
                    flows_tx = self.flows.using(tx)
                    jobs_tx = self.jobs.using(tx)
                    reconcile_store_tx = self.reconcile_store.using(tx)
                    result_store_tx = self.result_store.using(tx)
                    reviews_tx = self.reviews.using(tx)
                    stops_tx = self.stops.using(tx)
                    webhook_store_tx = self.webhook_store.using(tx)
                    workers_tx = self.workers.using(tx)
                    with tx.connection() as conn:
                        conn.execute("SET LOCAL statement_timeout = '2s'")
                    leased = webhook_store_tx
                    if not leased.lock(item):
                        continue
                    service = ControlService(
                        self.service.plugins,
                        partial(callbacks.append, self.service.notify_jobs),
                        after_commit=callbacks,
                        database=tx,
                        deliveries=deliveries_tx,
                        flows=flows_tx,
                        jobs=jobs_tx,
                        result_store=result_store_tx,
                        reviews=reviews_tx,
                        stops=stops_tx,
                        webhook_store=webhook_store_tx,
                        workers=workers_tx,
                    )
                    if item.payload is None:
                        raise ValueError("pending webhook payload missing")
                    # dispatch is synchronous DB-only work; nested transactions share this connection.
                    EventRouter(
                        service,
                        self.settings,
                        database=tx,
                        flows=flows_tx,
                        reconcile_store=reconcile_store_tx,
                        reviews=reviews_tx,
                        webhook_store=webhook_store_tx,
                    ).dispatch(
                        item.event_type,
                        item.delivery_id,
                        item.payload,
                    )
                    if not leased.complete(item):
                        raise RuntimeError("webhook claim changed")
            except Exception as error:  # noqa: BLE001 -- each failure has durable retry/blocked state
                inbox.fail(item, type(error).__name__, permanent=isinstance(
                    error, (KeyError, TypeError, ValueError, ValidationError),
                ))
                logger.warning("webhook processing failed: reason={}", type(error).__name__)
            else:
                logger.bind(event="webhook_applied", reason_code="ok",
                            delivery_id=item.delivery_id).info("")
                handled += 1
                for callback in callbacks:
                    try:
                        if loop is None:
                            callback()
                        else:
                            loop.call_soon_threadsafe(callback)
                    except Exception:  # noqa: BLE001 -- committed facts are recovered by periodic sweeps
                        logger.warning("post-commit webhook notification failed")
        return handled

    def _cycle(self, loop: asyncio.AbstractEventLoop) -> None:
        self.sweep(loop)
        self.webhook_store.cleanup()

    async def run(self) -> None:
        while True:
            self.wakeup.clear()
            try:
                work = asyncio.create_task(asyncio.to_thread(self._cycle, asyncio.get_running_loop()))
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    # Do not close the connection pool while a DB-only transaction is still running.
                    await work
                    raise
            except Exception as error:  # noqa: BLE001 -- DB outage leaves durable claims for the next sweep
                logger.warning("webhook sweep unavailable: reason={}", type(error).__name__)
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=1)
            except TimeoutError:
                pass
