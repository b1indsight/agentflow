import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.worker.config import WorkerSettings
from app.worker.execution.amp_agent import AmpCodingAgent
from app.worker.execution.codex import CodexAgent
from app.worker.execution.vcs import _run
from app.worker.runtime import WorkerRootLock, WorkerRuntime
from tests.test_worker_client import ScriptedConnection


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


@pytest.mark.parametrize("backend,expected,timeout", [(None, CodexAgent, 23), ("amp", AmpCodingAgent, 47)])
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
    assert isinstance(runtime.execution.executor.code_agent, expected)
    assert runtime.execution.executor.code_agent.timeout == timeout
    assert runtime.execution.executor.revision_timeout == timeout


@pytest.mark.parametrize("shutdown", ["stop", "cancel"])
async def test_runtime_keeps_root_locked_until_owned_process_cleanup_finishes(tmp_path, monkeypatch, shutdown):
    # Process shutdown must survive repeated caller cancellation, without inventing a stop receipt.
    monkeypatch.setattr("app.worker.runtime.configure_logging", lambda *args: None)
    cleaning, release = asyncio.Event(), asyncio.Event()
    ready = tmp_path / "pids"
    script = ("import os,subprocess,sys,time; from pathlib import Path; "
              "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
              "Path(sys.argv[1]).write_text(str(os.getpid())+' '+str(child.pid)); time.sleep(60)")

    class Executor:
        async def execute(self, task, secret=None, *, tool_caller):
            process = asyncio.create_task(_run(sys.executable, "-c", script, str(ready)))
            try:
                await asyncio.shield(process)
            finally:
                cleaning.set()
                await release.wait()
                process.cancel()
                await asyncio.gather(process, return_exceptions=True)

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield ScriptedConnection()

    monkeypatch.setattr("app.worker.transport.client.connect", connect)
    runtime = WorkerRuntime(WorkerSettings(worker_id="worker", worker_token=SecretStr("secret"),
        worker_root=tmp_path, serve_ws_url="wss://control.example/connect"))
    runtime.execution.executor = Executor()
    running = asyncio.create_task(runtime.run())
    second = WorkerRootLock(tmp_path)
    try:
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(0.01)
        pids = list(map(int, ready.read_text().split()))
        if shutdown == "stop":
            runtime.stop()
        else:
            running.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        for _ in range(2):
            running.cancel()
            await asyncio.sleep(0)
        assert not running.done()
        with pytest.raises(RuntimeError, match="already in use"):
            second.acquire()
        for pid in pids:
            os.kill(pid, 0)
        release.set()
        if shutdown == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 5)
        else:
            await asyncio.wait_for(running, 5)
        async with asyncio.timeout(5):
            for pid in pids:
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.01)
        record = runtime.execution.state.load()
        assert record.state == "started" and record.stopped is None and record.terminal is None
        assert not record.acknowledged
        second.acquire()
    finally:
        second.release()
        release.set()
        runtime.stop()
        await asyncio.gather(running, return_exceptions=True)
