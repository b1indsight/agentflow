"""Job 队列消费 + 崩溃恢复。ARCHITECTURE §4.2 / §10-1。

MVP：进程内单 worker，轮询 jobs 表。启动时 recover 悬空 job。
这是 §9-6 JobDispatch 端口的种子（日后换队列 + 远程 worker）。
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from loguru import logger

from app.models import Job, JobState
from app.store import Store

_POLL_INTERVAL = 2.0
_HEARTBEAT_INTERVAL = 15.0


class JobRunner(Protocol):
    async def run_job(self, job: Job) -> None: ...


class Worker:
    def __init__(self, store: Store, engine: JobRunner) -> None:
        self.store = store
        self.engine = engine
        self._stop = asyncio.Event()

    async def run(self) -> None:
        recovered = self.store.recover_stale_jobs()
        if recovered:
            logger.info("recovered {} stale job(s) on startup", recovered)
        logger.info("worker started")
        while not self._stop.is_set():
            job = self.store.claim_job()
            if job is None:
                await asyncio.sleep(_POLL_INTERVAL)
                continue
            await self._dispatch(job)

    async def _dispatch(self, job: Job) -> None:
        logger.info("dispatch job {} kind={} flow={}", job.id, job.kind, job.flow_id)
        hb = asyncio.create_task(self._heartbeat(job.id))
        try:
            await self.engine.run_job(job)
            self.store.finish_job(job.id, JobState.DONE)
        except Exception as e:  # noqa: BLE001
            logger.exception("job {} failed: {}", job.id, e)
            self.store.finish_job(job.id, JobState.FAILED)
        finally:
            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning("heartbeat task for job {} failed: {}", job.id, e)

    async def _heartbeat(self, job_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                try:
                    self.store.heartbeat(job_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "heartbeat for job {} failed; retrying: {}", job_id, e
                    )
        except asyncio.CancelledError:
            return

    def stop(self) -> None:
        self._stop.set()
