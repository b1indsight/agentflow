"""Composition root and process-lifetime root lock for a remote worker."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Mapping
from pathlib import Path
from typing import IO

from loguru import logger

from app.codex_driver import CodexAgent
from app.config import WorkerSettings, get_worker_settings
from app.judge import BubJudge
from app.worker.client import WorkerClient
from app.worker.executor import WorkerExecutor
from app.worker.journal import WorkerJournal

_CONTROL_ENVIRONMENT_KEYS = frozenset(
    {
        "FLOWTOOL_DATABASE_URL",
        "FLOWTOOL_GITHUB_APP_ID",
        "FLOWTOOL_GITHUB_PRIVATE_KEY_PATH",
        "FLOWTOOL_GITHUB_WEBHOOK_SECRET",
        "FLOWTOOL_WORKER_TOKENS",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    }
)


def validate_worker_environment(
    settings: WorkerSettings, source: Mapping[str, str] | None = None
) -> None:
    """Fail closed when full-access Codex shares control-plane process secrets."""

    if not settings.trusted_host_execution:
        return
    values = os.environ if source is None else source
    exposed = sorted(key for key in _CONTROL_ENVIRONMENT_KEYS if values.get(key))
    if exposed:
        raise RuntimeError(
            "trusted-host CodeAgent cannot start with control-plane secrets in the worker "
            f"environment: {', '.join(exposed)}"
        )


class WorkerRootLock:
    """Prevent two worker processes from mutating the same workspace root."""

    def __init__(self, worker_root: Path, worker_id: str | None = None) -> None:
        self.worker_root = worker_root
        self.worker_id = worker_id
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
        if self.worker_id is not None:
            identity_path = self.worker_root / ".worker-id"
            if identity_path.exists():
                recorded = identity_path.read_text().strip()
                if recorded != self.worker_id:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()
                    raise RuntimeError(
                        f"worker root belongs to {recorded}, not {self.worker_id}: "
                        f"{self.worker_root}"
                    )
            else:
                identity_path.write_text(f"{self.worker_id}\n")
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
        self.root_lock = WorkerRootLock(self.settings.worker_root, self.settings.worker_id)
        judge = BubJudge(
            model=self.settings.judge_model, api_key=self.settings.judge_api_key,
            api_base=self.settings.judge_api_base, timeout=self.settings.judge_timeout_seconds,
        )
        if failure := judge.configuration_failure():
            logger.warning("Judge unavailable; triage will wait: {}", failure)
        executor = WorkerExecutor(
            self.settings.worker_root,
            CodexAgent(
                trusted_host=self.settings.trusted_host_execution,
                timeout=self.settings.codex_run_timeout_seconds,
            ),
            judge,
            worker_id=self.settings.worker_id,
        )
        self.client = WorkerClient(
            worker_id=self.settings.worker_id,
            worker_token=self.settings.worker_token.get_secret_value(),
            serve_ws_url=self.settings.serve_ws_url,
            executor=executor,
            journal=WorkerJournal(self.settings.worker_root),
            reconnect_min_backoff=self.settings.reconnect_min_backoff,
            reconnect_max_backoff=self.settings.reconnect_max_backoff,
            reconnect_jitter=self.settings.reconnect_jitter,
        )
        executor.tool_caller = self.client

    async def run(self) -> None:
        validate_worker_environment(self.settings)
        self.root_lock.acquire()
        try:
            await self.client.run()
        finally:
            self.root_lock.release()

    def stop(self) -> None:
        self.client.stop()
