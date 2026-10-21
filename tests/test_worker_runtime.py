from pathlib import Path

import pytest
from pydantic import SecretStr

from app.worker.config import WorkerSettings
from app.worker.execution.amp import AmpAgent
from app.worker.execution.codex import CodexAgent
from app.worker.runtime import WorkerRootLock, WorkerRuntime


def test_worker_root_lock_prevents_a_second_process_root_owner(tmp_path: Path) -> None:
    first = WorkerRootLock(tmp_path)
    second = WorkerRootLock(tmp_path)

    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_distinct_worker_roots_record_identity_and_lock_independently(tmp_path: Path) -> None:
    first_root = tmp_path / "worker-a"
    second_root = tmp_path / "worker-b"
    first = WorkerRootLock(first_root, "worker-a")
    second = WorkerRootLock(second_root, "worker-b")

    first.acquire()
    second.acquire()
    try:
        assert (first_root / ".worker-id").read_text() == "worker-a\n"
        assert (second_root / ".worker-id").read_text() == "worker-b\n"
    finally:
        first.release()
        second.release()


def test_worker_root_rejects_sequential_identity_reuse(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    first = WorkerRootLock(root, "worker-a")
    first.acquire()
    first.release()

    with pytest.raises(RuntimeError, match="belongs to worker-a"):
        WorkerRootLock(root, "worker-b").acquire()


@pytest.mark.parametrize("backend,expected,timeout", [(None, CodexAgent, 23), ("amp", AmpAgent, 47)])
def test_runtime_selects_agent_and_its_revision_deadline(tmp_path, monkeypatch, backend, expected, timeout):
    monkeypatch.setattr("app.worker.runtime.configure_logging", lambda *args: None)
    if backend is None:
        monkeypatch.delenv("FLOWTOOL_CODING_AGENT", raising=False)
    else:
        monkeypatch.setenv("FLOWTOOL_CODING_AGENT", backend)
    settings = WorkerSettings(worker_id="worker", worker_token=SecretStr("secret"),
                              worker_root=tmp_path, serve_ws_url="wss://control.example/connect",
                              codex_run_timeout_seconds=23, amp_run_timeout_seconds=47)
    runtime = WorkerRuntime(settings)
    assert isinstance(runtime.client.executor.code_agent, expected)
    assert runtime.client.executor.code_agent.timeout == timeout
    assert runtime.client.executor.revision_timeout == timeout
