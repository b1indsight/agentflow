"""Observable CLI activity, through both public CodingAgent implementations."""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.worker.execution.amp_agent import AmpCodingAgent
from app.worker.execution.codex import CodexAgent
from app.worker.execution.executor import WorkerExecutor
from app.worker.state.execution import ExecutionState
from tests.test_amp_driver import THREAD, stream
from tests.test_revision_delivery import revision_output
from tests.test_revision_delivery import revision_workspace as revision_workspace  # noqa: PLC0414
from tests.test_worker_executor import RecordingToolCaller


@pytest.fixture(params=["amp", "codex"])
async def cli(request, monkeypatch, tmp_path):
    # Advance only the monotonic clock; the event loop still schedules real pipe readers.
    loop = asyncio.get_running_loop()
    real_time = loop.time
    offset = 0.0
    monkeypatch.setattr(loop, "time", lambda: real_time() + offset)

    async def tick(seconds=0):
        nonlocal offset
        offset += seconds
        for _ in range(20):
            await asyncio.sleep(0)

    class Process:
        pid = 999999999
        returncode = None
        stdin = None

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.exited = asyncio.Event()

        async def wait(self):
            await self.exited.wait()
            return self.returncode

        def finish(self, code=0):
            if self.returncode is None:
                self.returncode = code
            for pipe in (self.stdout, self.stderr):
                if not pipe.at_eof():
                    pipe.feed_eof()
            self.exited.set()

    process = Process()
    backend = request.param
    session = THREAD if backend == "amp" else "session-1"
    agent = (AmpCodingAgent if backend == "amp" else CodexAgent)(trusted_host=True)
    runs = []
    last_files = []
    started = asyncio.Event()
    real_spawn, real_killpg = asyncio.create_subprocess_exec, os.killpg

    async def spawn(*args, **kwargs):
        if args[0] not in {"amp", "codex"}:
            return await real_spawn(*args, **kwargs)
        assert kwargs["start_new_session"]
        if backend == "codex":
            last_files.append(Path(args[args.index("-o") + 1]))
        started.set()
        return process

    async def stop(_process):
        process.finish(-9)

    def killpg(pid, sig):
        if pid == process.pid:
            process.finish(-9)
        else:
            real_killpg(pid, sig)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("app.worker.execution.amp_process._stop_amp", stop)
    monkeypatch.setattr(os, "killpg", killpg)

    async def start(resume=False):
        running = asyncio.create_task(
            agent.resume(session, "continue", cwd=tmp_path) if resume
            else agent.start("task", tmp_path)
        )
        runs.append(running)
        await tick()
        return running

    def complete(output='{"caller_owned":true}'):
        for path in last_files:
            path.write_text(output)
        process.stdout.feed_data(stream(result=output) if backend == "amp" else
                                b'{"type":"thread.started","thread_id":"session-1"}\n')
        process.finish()

    try:
        yield SimpleNamespace(process=process, start=start, tick=tick, complete=complete,
                              agent=agent, started=started, session=session, runs=runs)
    finally:
        for running in runs:
            if not running.done():
                running.cancel()
        await asyncio.gather(*runs, return_exceptions=True)


@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
@pytest.mark.parametrize("resume", [False, True])
async def test_cli_activity_outlives_multiple_idle_windows(cli, pipe, resume):
    # A tool's partial progress must renew the deadline without a newline or JSON record.
    running = await cli.start(resume)
    for _ in range(4):
        getattr(cli.process, pipe).feed_data(b" ")
        await cli.tick(800)
        assert not running.done()
    cli.complete()
    result = await running
    assert result.exit_code == 0


@pytest.mark.parametrize("activity", [False, True])
@pytest.mark.parametrize("end_state", ["eof", "exited"])
async def test_cli_times_out_only_after_a_full_silent_window(cli, activity, end_state):
    running = await cli.start(resume=True)
    if activity:
        cli.process.stderr.feed_data(b"tool is running")
        await cli.tick(1000)
    await cli.tick(1190)
    assert not running.done()
    # Neither late EOF nor CLI exit with a descendant holding pipes open renews activity.
    if end_state == "eof":
        cli.process.stdout.feed_eof()
        cli.process.stderr.feed_eof()
    else:
        cli.process.returncode = 0
        cli.process.exited.set()
    await cli.tick(20)
    result = await running
    assert result.exit_code == 124
    assert result.failure_detail and "1200" in result.failure_detail
    assert "no new stdout/stderr" in result.failure_detail
    assert cli.process.exited.is_set()


async def test_cli_cancellation_reaps_process_and_pipe_readers(cli):
    existing = asyncio.all_tasks()
    running = await cli.start()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert cli.process.exited.is_set()
    assert not asyncio.all_tasks() - existing


async def test_completed_cli_wins_when_deadline_wakes_at_the_same_time(cli):
    running = await cli.start()
    cli.complete()
    await cli.tick(1201)
    assert (await running).exit_code == 0


@pytest.mark.parametrize("resume", [False, True])
async def test_active_revision_outlives_former_outer_deadline(revision_workspace, cli, resume):
    # The executor used to cancel even an active adapter at the total revision deadline.
    root, _, task, secret = revision_workspace
    task = task.model_copy(update={"session_id": cli.session if resume else None})
    executor = WorkerExecutor(root, cli.agent, state=ExecutionState(root), worker_id="w")
    running = asyncio.create_task(executor.execute(task, secret, tool_caller=RecordingToolCaller()))
    cli.runs.append(running)
    await cli.started.wait()
    await cli.tick()
    for _ in range(4):
        cli.process.stderr.feed_data(b"progress")
        await cli.tick(800)
        assert not running.done()
    cli.complete(revision_output().model_dump_json())
    result = await running
    assert result.exit_code == 0 and result.clean_worktree


@pytest.mark.parametrize("backend", ["amp", "codex"])
async def test_cli_real_pipes_keep_partial_stderr_activity_alive(monkeypatch, tmp_path, backend):
    spawn = asyncio.create_subprocess_exec

    async def fake_cli(*args, **kwargs):
        final = stream() if backend == "amp" else b'{"type":"thread.started","thread_id":"s"}\n'
        script = ("import os,time\nfor _ in range(10):\n os.write(2,b'.'); time.sleep(.08)\n"
                  f"os.write(1,{final!r})\n")
        if backend == "codex":
            script += f"open({args[args.index('-o') + 1]!r},'w').write('{{\"caller_owned\":true}}')\n"
        return await spawn(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_cli)
    agent = (AmpCodingAgent if backend == "amp" else CodexAgent)(trusted_host=True, timeout=0.4)
    result = await agent.start("task", tmp_path)
    assert result.exit_code == 0 and result.last_message == '{"caller_owned":true}'
