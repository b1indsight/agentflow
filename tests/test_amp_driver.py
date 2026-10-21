"""Amp's public CodingAgent contract, without network or model calls."""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.domain.models import CodingExecutionContext, GitHubCapability
from app.worker.execution.amp import AmpAgent
from app.worker.execution.amp_cli import AmpProcessResult, amp_environment, run_amp

THREAD = "T-01a15d70-eb3d-722c-9f6d-563dd65eb0a8"


def stream(*, result=None, session=THREAD, **overrides):
    final = {"type": "result", "subtype": "success", "is_error": False,
             "session_id": session, "result": result or '{"caller_owned":true}'} | overrides
    return "\n".join(json.dumps(event) for event in [
        {"type": "system", "subtype": "init", "session_id": THREAD, "tools": ["shell_command"]}, final,
    ]).encode()


@pytest.mark.parametrize("resume", [False, True])
async def test_amp_uses_current_capabilities_and_raw_caller_schema(monkeypatch, tmp_path, resume):
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object","required":["caller_owned"]}')
    context = CodingExecutionContext(
        github=GitHubCapability(SecretStr("bot-secret"), "owner/repo",
                                datetime.now(UTC) + timedelta(minutes=5)),
        mcp_url=None if resume else "http://127.0.0.1:12345/mcp",
        mcp_token=None if resume else SecretStr("broker-secret"),
    )
    directories = []

    def github(capability, directory, source):
        directories.append(directory)
        assert capability == context.github
        return {"GH_TOKEN": "bot-secret", "GH_CONFIG_DIR": str(directory / "config")}

    async def execute(args, *, cwd, env, input_text, **kwargs):
        assert cwd == tmp_path
        assert "bot-secret" not in " ".join(args) and "broker-secret" not in input_text
        assert "caller_owned" in input_text and "task" in input_text
        assert env["GH_TOKEN"] == env["FLOWTOOL_PUSH_TOKEN"] == "bot-secret"
        settings = json.loads(Path(args[args.index("--settings-file") + 1]).read_text())
        assert "async_shell_command" in settings["amp.tools.enable"]
        broker = settings["amp.mcpServers"]["flow"]
        if resume:
            assert args[:3] == ["threads", "continue", THREAD]
            assert broker["disabled"] is True
            assert "FLOWTOOL_MCP_TOKEN" not in env
        else:
            assert broker["url"] == context.mcp_url
            assert broker["headers"]["Authorization"] == "Bearer ${FLOWTOOL_MCP_TOKEN}"
            assert env["FLOWTOOL_MCP_TOKEN"] == "broker-secret"
        return AmpProcessResult(stream(result='{"caller_owned":"bot-secret"}'), b"", 0)

    monkeypatch.setattr("app.worker.execution.amp.github_environment", github)
    monkeypatch.setattr("app.worker.execution.amp.run_amp", execute)
    agent = AmpAgent(trusted_host=True)
    result = (await agent.resume(THREAD, "task", cwd=tmp_path, output_schema=schema, context=context)
              if resume else await agent.start("task", tmp_path, output_schema=schema, context=context))
    assert result.session_id == THREAD and result.exit_code == 0
    assert result.last_message == '{"caller_owned":"[REDACTED]"}'
    assert all(not directory.exists() for directory in directories)


@pytest.mark.parametrize("stdout,exit_code", [
    (stream(is_error=True, subtype="error_during_execution", error="User cancelled"), 0),
    (stream(session="T-00000000-0000-0000-0000-000000000000"), 0),
    (stream() + b'\n{"type":"result"}', 0),
    (b'{"type":"assistant"}\n', 0),
    (b'not json\n' + stream(), 0),
    (stream(), 1),
])
async def test_incomplete_or_failed_execution_never_succeeds(monkeypatch, tmp_path, stdout, exit_code):
    async def execute(*args, **kwargs):
        return AmpProcessResult(stdout, b"", exit_code)

    monkeypatch.setattr("app.worker.execution.amp.run_amp", execute)
    result = await AmpAgent(trusted_host=True).resume(THREAD, "task", cwd=tmp_path)
    assert result.exit_code != 0 and result.failure_detail


async def test_unproven_missing_thread_does_not_trigger_replay(monkeypatch, tmp_path):
    # A single observed stderr spelling cannot establish all authentication/network cases.
    async def execute(*args, **kwargs):
        return AmpProcessResult(b"", f"Thread https://ampcode.com/threads/{THREAD} does not exist.\n".encode(), 1)

    monkeypatch.setattr("app.worker.execution.amp.run_amp", execute)
    result = await AmpAgent(trusted_host=True).resume(THREAD, "task", cwd=tmp_path)
    assert result.exit_code != 0
    with pytest.raises(ValueError, match="Amp thread"):
        await AmpAgent(trusted_host=True).resume("../thread", "task", cwd=tmp_path)
    with pytest.raises(RuntimeError, match="trusted-host"):
        await AmpAgent().start("task", tmp_path)


async def test_workspace_settings_cannot_override_invocation_policy(tmp_path, monkeypatch):
    # Amp loads workspace settings after --settings-file, including MCP policy.
    settings = tmp_path / ".amp" / "settings.jsonc"
    settings.parent.mkdir()
    settings.write_text('{"amp.mcpPermissions":[]}')

    async def execute(*args, **kwargs):
        pytest.fail("conflicting settings must be rejected before starting Amp")

    monkeypatch.setattr("app.worker.execution.amp.run_amp", execute)
    with pytest.raises(ValueError, match="workspace Amp settings"):
        await AmpAgent(trusted_host=True).start("task", tmp_path)
    assert settings.read_text() == '{"amp.mcpPermissions":[]}'


def test_environment_keeps_amp_login_but_excludes_ambient_credentials():
    environment = amp_environment({"HOME": "/worker", "AMP_API_KEY": "amp-secret",
                                   "PATH": "/usr/bin", "GIT_AUTHOR_NAME": "bot",
                                   "FLOWTOOL_WORKER_TOKEN": "worker-secret",
                                   "GH_TOKEN": "personal", "OPENAI_API_KEY": "other",
                                   "AMP_URL": "https://untrusted.example"})
    assert environment["HOME"] == "/worker" and environment["AMP_API_KEY"] == "amp-secret"
    assert environment["GIT_AUTHOR_NAME"] == "bot"
    assert not {"GH_TOKEN", "OPENAI_API_KEY", "AMP_URL", "FLOWTOOL_WORKER_TOKEN"} & environment.keys()


async def test_process_supervisor_drains_both_pipes_and_bounds_output(monkeypatch, tmp_path):
    monkeypatch.setattr("app.worker.execution.amp_cli.AMP_BIN", sys.executable)
    result = await run_amp(["-c", "import os; os.write(1,b'o'*131072); os.write(2,b'e'*131072)"],
                           cwd=tmp_path, max_stdout=262144)
    assert result.exit_code == 0 and len(result.stdout) == len(result.stderr) == 131072
    result = await run_amp(["-c", "import os,time; os.write(2,b'e'*262144); time.sleep(20)"],
                           cwd=tmp_path, max_stdout=1024)
    assert result.overflow and len(result.stderr) <= 1024


@pytest.mark.parametrize("cancel", [False, True])
async def test_supervisor_cleans_separate_child_group_before_returning(monkeypatch, tmp_path, cancel):
    # Native Amp shells use their own PGID; killing just the CLI group leaks them.
    monkeypatch.setattr("app.worker.execution.amp_cli.AMP_BIN", sys.executable)
    monkeypatch.setattr("app.worker.execution.amp_cli.TERMINATION_GRACE", 0.1)
    pidfile = tmp_path / "child.pid"
    code = f"""import pathlib, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))
time.sleep(60)
"""
    task = asyncio.create_task(run_amp(["-c", code], cwd=tmp_path, timeout=0.8))
    for _ in range(100):
        if pidfile.exists():
            break
        await asyncio.sleep(0.01)
    assert pidfile.exists()
    child = int(pidfile.read_text())
    try:
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).timed_out
        for _ in range(100):
            process = await asyncio.create_subprocess_exec(
                "ps", "-o", "stat=", "-p", str(child), stdout=asyncio.subprocess.PIPE)
            stdout, _ = await process.communicate()
            if not stdout.strip() or stdout.strip().startswith(b"Z"):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("separate child group survived supervisor")
    finally:
        try:
            os.killpg(child, 9)
        except ProcessLookupError:
            pass
