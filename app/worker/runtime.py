"""Composition root and process-lifetime root lock for a remote worker."""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import IO

from app.telemetry.logging import configure_logging
from app.worker.config import WorkerSettings
from app.worker.execution.codex import CodexAgent
from app.worker.execution.executor import WorkerExecutor
from app.worker.health import HealthFile
from app.worker.state.session import WorkerSession
from app.worker.transport.client import WorkerClient

_CONTROL_ENVIRONMENT_KEYS = frozenset(
    {
        "FLOWTOOL_DATABASE_URL",
        "FLOWTOOL_CONTROL_API_TOKEN",
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
            "trusted-host CodingAgent cannot start with control-plane secrets in the worker "
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

    def __init__(self, settings: WorkerSettings) -> None:
        self._shutdown_requested = False
        self.settings = settings
        configure_logging("worker", self.settings.log_format)
        self.root_lock = WorkerRootLock(self.settings.worker_root, self.settings.worker_id)
        session = WorkerSession(self.settings.worker_root)
        executor = WorkerExecutor(
            self.settings.worker_root,
            CodexAgent(
                trusted_host=self.settings.trusted_host_execution,
                timeout=self.settings.codex_run_timeout_seconds,
            ),
            worker_id=self.settings.worker_id,
            session=session,
            revision_timeout=self.settings.codex_run_timeout_seconds,
        )
        self.client = WorkerClient(
            worker_id=self.settings.worker_id,
            worker_token=self.settings.worker_token.get_secret_value(),
            serve_ws_url=self.settings.serve_ws_url,
            executor=executor,
            session=session,
            reconnect_min_backoff=self.settings.reconnect_min_backoff,
            reconnect_max_backoff=self.settings.reconnect_max_backoff,
            reconnect_jitter=self.settings.reconnect_jitter,
        )

    async def run(self) -> None:
        validate_worker_environment(self.settings)
        self.root_lock.acquire()
        health_task = asyncio.create_task(self._report_health()) if self.settings.worker_status_path else None
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self.stop)
        try:
            await self.client.run()
        except asyncio.CancelledError:
            if not self._shutdown_requested:
                raise
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            if health_task is not None:
                health_task.cancel()
                await asyncio.gather(health_task, return_exceptions=True)
            self.root_lock.release()

    async def _report_health(self) -> None:
        assert self.settings.worker_status_path is not None
        marker = HealthFile(self.settings.worker_status_path)
        try:
            while True:
                marker.write(self.client.connected)
                await asyncio.sleep(5)
        finally:
            marker.write(False)

    def stop(self) -> None:
        self._shutdown_requested = True
        self.client.stop()
