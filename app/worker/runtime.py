"""Composition root and process-lifetime root lock for a remote worker."""

from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO

from app.codex_driver import CodexAgent
from app.config import WorkerSettings, get_worker_settings
from app.judge import BubJudge
from app.worker.client import WorkerClient
from app.worker.executor import WorkerExecutor


class WorkerRootLock:
    """Prevent two worker processes from mutating the same workspace root."""

    def __init__(self, worker_root: Path) -> None:
        self.worker_root = worker_root
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.worker_root.mkdir(parents=True, exist_ok=True)
        handle = (self.worker_root / ".worker.lock").open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(f"worker root already in use: {self.worker_root}") from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


class WorkerRuntime:
    """Own the execution adapters without importing control-plane dependencies."""

    def __init__(self, settings: WorkerSettings | None = None) -> None:
        self.settings = settings or get_worker_settings()
        self.root_lock = WorkerRootLock(self.settings.worker_root)
        executor = WorkerExecutor(
            self.settings.worker_root,
            CodexAgent(timeout=self.settings.codex_run_timeout_seconds),
            BubJudge(),
        )
        self.client = WorkerClient(
            worker_id=self.settings.worker_id,
            worker_token=self.settings.worker_token.get_secret_value(),
            serve_ws_url=self.settings.serve_ws_url,
            executor=executor,
        )

    async def run(self) -> None:
        self.root_lock.acquire()
        try:
            await self.client.run()
        finally:
            self.root_lock.release()

    def stop(self) -> None:
        self.client.stop()
