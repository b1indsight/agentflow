"""对账 poller：补 webhook 漏投。

见 docs/architecture/components.md §4.2、docs/architecture/decisions.md §10-2。

webhook 为主、周期轮询兜底：启动时 + 定期，把 open flows 从 GitHub 重新同步
（新 review？PR 是否已被合并？）。

M0：骨架循环 + TODO(verify) 实际同步逻辑。
"""

from __future__ import annotations

import asyncio

from loguru import logger

from app.engine import FlowEngine
from app.store import Store

_RECONCILE_INTERVAL = 300.0  # 5 min


class Reconciler:
    def __init__(self, store: Store, engine: FlowEngine) -> None:
        self.store = store
        self.engine = engine
        self._stop = asyncio.Event()

    async def run(self) -> None:
        logger.info("reconciler started")
        while not self._stop.is_set():
            try:
                await self._sweep()
            except Exception as e:  # noqa: BLE001
                logger.warning("reconcile sweep error: {}", e)
            await asyncio.sleep(_RECONCILE_INTERVAL)

    async def _sweep(self) -> None:
        for flow in self.store.list_flows(active_only=True):
            # TODO(verify) V1：从 GitHub 拉该 flow 的 PR/review 状态，与本地对齐：
            #   - PR 已合并但没收到 webhook → engine.on_merged
            #   - 有新 review/comment（依 updated_at 游标）→ 落 review_threads + settle
            logger.debug("reconcile flow {} ({})", flow.id, flow.state)

    def stop(self) -> None:
        self._stop.set()
