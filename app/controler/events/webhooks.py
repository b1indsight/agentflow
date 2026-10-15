"""Replay durable webhook receipts through one transaction-bound router."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial

from loguru import logger
from pluggy import PluginManager
from pydantic import ValidationError

from app.controler.config import ServerSettings
from app.controler.events.router import EventRouter
from app.controler.workflow.service import ControlService
from app.persistence.database import Database
from app.persistence.webhooks import WebhookStore


class WebhookConsumer:
    def __init__(
        self,
        settings: ServerSettings,
        *,
        database: Database,
        plugins: PluginManager,
        jobs_available: Callable[[], None],
    ) -> None:
        self.database = database
        self.webhook_store = WebhookStore(database)
        self.settings = settings
        self.plugins = plugins
        self.jobs_available = jobs_available
        self.wakeup = asyncio.Event()

    def sweep(self, loop: asyncio.AbstractEventLoop | None = None) -> int:
        """Create services per receipt and publish notifications after its transaction commits."""
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
                    with tx.connection() as conn:
                        conn.execute("SET LOCAL statement_timeout = '2s'")
                    leased = self.webhook_store.using(tx)
                    if not leased.lock(item):
                        continue
                    if item.payload is None:
                        raise ValueError("pending webhook payload missing")
                    # dispatch is synchronous DB-only work; nested transactions share this connection.
                    service = ControlService(
                        plugins=self.plugins,
                        jobs_available=partial(callbacks.append, self.jobs_available),
                        after_commit=callbacks,
                        database=tx,
                    )
                    router = EventRouter(service, self.settings, database=tx)
                    router.dispatch(
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
