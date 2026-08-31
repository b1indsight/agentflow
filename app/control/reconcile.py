"""Control-plane reconciliation poller for missed GitHub events."""

from __future__ import annotations

import asyncio

from loguru import logger

from app.store import Store

_RECONCILE_INTERVAL = 300.0


class Reconciler:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._stop = asyncio.Event()

    async def run(self) -> None:
        logger.info("reconciler started")
        while not self._stop.is_set():
            try:
                await self._sweep()
            except Exception as exc:  # noqa: BLE001
                logger.warning("reconcile sweep error: {}", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_RECONCILE_INTERVAL)
            except TimeoutError:
                pass

    async def _sweep(self) -> None:
        for flow in self.store.list_flows(active_only=True):
            logger.debug("reconcile flow {} ({})", flow.id, flow.state)

    def stop(self) -> None:
        self._stop.set()
