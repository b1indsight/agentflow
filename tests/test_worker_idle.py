"""Observable CLI activity, through both public CodingAgent implementations."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.worker.execution.amp_agent import AmpCodingAgent
from app.worker.execution.codex import CodexAgent
from app.worker.execution.idle import wait_for_inactivity
from tests.test_amp_driver import THREAD, stream


@pytest.mark.parametrize("backend", ["amp", "codex"])
async def test_cli_partial_output_renews_idle_timeout(monkeypatch, tmp_path, backend):
    # Each pipe alone stays active longer than the deadline; no newline is needed.
    spawn = asyncio.create_subprocess_exec

    async def fake_cli(*args, **kwargs):
        final = stream() if backend == "amp" else b'{"type":"thread.started","thread_id":"s"}\n'
        script = ("import os,time\nfor fd in (2,1):\n for _ in range(8):\n"
                  "  os.write(fd,b' '); time.sleep(.08)\n" f"os.write(1,{final!r})\n")
        if backend == "codex":
            script += f"open({args[args.index('-o') + 1]!r},'w').write('{{\"caller_owned\":true}}')\n"
        return await spawn(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_cli)
    agent = (AmpCodingAgent if backend == "amp" else CodexAgent)(trusted_host=True, timeout=0.4)
    result = await agent.start("task", tmp_path)
    assert result.exit_code == 0 and result.last_message == '{"caller_owned":true}'


@pytest.mark.parametrize("backend", ["amp", "codex"])
async def test_silent_cli_reports_timeout_even_after_process_exit(monkeypatch, tmp_path, backend):
    # A CLI can exit while a descendant holds its pipes open; wait() alone is insufficient.
    process = SimpleNamespace(pid=999999999, returncode=0, stdin=None, wait=AsyncMock(return_value=0),
                              stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader())
    process.stderr.feed_data(b"last progress")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr("app.worker.execution.amp_process._stop_amp", AsyncMock())
    monkeypatch.setattr("app.worker.execution.codex.os.killpg", lambda *_args: None)
    agent = (AmpCodingAgent if backend == "amp" else CodexAgent)(trusted_host=True, timeout=0.01)
    result = await agent.resume(THREAD if backend == "amp" else "session-1", "task", cwd=tmp_path)
    assert result.exit_code == 124
    assert result.failure_detail == f"{backend.title()}: no new stdout/stderr for 0.01s; execution timed out"


@pytest.mark.parametrize("completed", [False, True])
async def test_idle_deadline_preserves_completed_results_and_pending_io(completed):
    # A late watchdog wake-up must not replace an observed result or cancel caller-owned I/O.
    loop = asyncio.get_running_loop()
    io = loop.create_future()
    last_activity = loop.time() - 2
    if completed:
        io.set_result("done")
        assert await wait_for_inactivity(io, timeout=1, last_activity=lambda: last_activity) == "done"
    else:
        with pytest.raises(TimeoutError):
            await wait_for_inactivity(io, timeout=1, last_activity=lambda: last_activity)
        assert not io.done()
