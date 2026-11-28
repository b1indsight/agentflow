import asyncio
import base64
import signal
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.domain.models import CodingExecutionContext, GitHubCapability, SessionUnavailable
from app.worker.execution.codex import CodexAgent, _codex_environment, _start_args
from tests.flow_fixtures import PNG_IMAGE


def pipe(data=b""):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


@pytest.mark.parametrize("model,effort", [
    (None, None), ("custom-codex-model", None), (None, "high"), ("custom-codex-model", "ultra"),
])
async def test_adapter_returns_raw_output_without_interpreting_business_schema(monkeypatch, tmp_path, model, effort):
    # A caller-owned schema need not contain INITIAL/REVISION fields. The adapter
    # must forward the CLI output; only the task executor knows its business type.
    monkeypatch.setenv("EXAMPLE_MCP_TOKEN", "custom-mcp-token")
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}')
    raw = '{"caller_owned":"result"}'

    class Process:
        pid = 999999999
        returncode = 0
        stdout = pipe(b'{"type":"thread.started","thread_id":"session-1"}\n')
        stderr = pipe()

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        assert kwargs["cwd"] == tmp_path
        assert args[:3] == ("codex", "exec", "task")
        assert Path(args[args.index("--image") + 1]).read_bytes() == base64.b64decode(PNG_IMAGE.split(",")[1])
        assert args[args.index("-C") + 1] == str(tmp_path)
        assert args[args.index("-s") + 1] == "danger-full-access"
        assert "workspace-write" not in args
        assert 'mcp_servers.flow={url="http://127.0.0.1", enabled=false}' in args
        assert kwargs["env"]["EXAMPLE_MCP_TOKEN"] == "custom-mcp-token"
        assert args[args.index("--output-schema") + 1] == str(schema)
        if model is None:
            assert "--model" not in args
        else:
            assert args[args.index("--model") + 1] == model
        overrides = [args[index + 1] for index, item in enumerate(args) if item == "-c"]
        assert [item for item in overrides if item.startswith("model_reasoning_effort=")] == (
            [f'model_reasoning_effort="{effort}"'] if effort is not None else []
        )
        Path(args[args.index("-o") + 1]).write_text(raw)
        return Process()

    monkeypatch.setattr("app.worker.execution.codex.asyncio.create_subprocess_exec", spawn)
    run = await CodexAgent(trusted_host=True).start("task", tmp_path, output_schema=schema, model=model, effort=effort,
                                                   context=CodingExecutionContext(images=(PNG_IMAGE,)))
    assert (run.session_id, run.last_message, run.exit_code) == ("session-1", raw, 0)
    assert run.backend == "codex" and run.model == model


def test_full_access_requires_explicit_trusted_host_mode() -> None:
    with pytest.raises(RuntimeError, match="trusted-host"):
        _start_args("do the task", Path("/worker/repos/flow-1"), trusted_host=False)


def test_codex_environment_removes_worker_and_github_credentials() -> None:
    environment = _codex_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/worker",
            "CODEX_HOME": "/home/worker/.codex",
            "OPENAI_API_KEY": "codex-secret",
            "HTTPS_PROXY": "https://proxy.example",
            "SSL_CERT_FILE": "/etc/ssl/corp.pem",
            "AGENTFLOW_WORKER_TOKEN": "worker-secret",
            "AGENTFLOW_DATABASE_URL": "database-secret",
            "GH_TOKEN": "github-secret",
            "GITHUB_TOKEN": "github-secret",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "personal-helper",
            "GIT_DIR": "/parent/repo/.git",
            "SSH_ASKPASS": "/personal/askpass",
            "CODEX_THREAD_ID": "parent-thread",
            "EXAMPLE_MCP_TOKEN": "custom-mcp-token",
        }
    )

    assert environment == {
        "PATH": "/usr/bin",
        "HOME": "/home/worker",
        "CODEX_HOME": "/home/worker/.codex",
        "OPENAI_API_KEY": "codex-secret",
        "HTTPS_PROXY": "https://proxy.example",
        "SSL_CERT_FILE": "/etc/ssl/corp.pem",
        "EXAMPLE_MCP_TOKEN": "custom-mcp-token",
    }


def test_run_context_injects_only_scoped_push_and_mcp_capabilities() -> None:
    context = CodingExecutionContext(
        github=GitHubCapability(SecretStr("push-secret"), "owner/repo",
                                datetime.now(UTC) + timedelta(minutes=5), "agentflow[bot]", 7654321),
        mcp_url="http://127.0.0.1:12345/mcp",
        mcp_token=SecretStr("mcp-secret"),
    )

    environment = _codex_environment({"PATH": "/usr/bin"}, context)
    args = _start_args(
        "do the task",
        Path("/worker/repos/flow-1"),
        trusted_host=True,
        context=context,
    )

    assert environment["AGENTFLOW_PUSH_TOKEN"] == "push-secret"
    assert environment["GIT_CONFIG_KEY_1"].endswith("helper")
    assert "push-secret" not in " ".join(args)
    assert environment["AGENTFLOW_MCP_TOKEN"] == "mcp-secret"
    assert "mcp-secret" not in " ".join(args)
    assert any('url="http://127.0.0.1:12345/mcp"' in item for item in args)
    assert any('bearer_token_env_var="AGENTFLOW_MCP_TOKEN"' in item for item in args)
    config = tomllib.loads(next(item for item in args if item.startswith("mcp_servers.flow=")))
    assert set(config["mcp_servers"]["flow"]["enabled_tools"]) == {"open_pr", "request_user_input", "search_flows"}
    assert not any("mcp_servers.flow.command" in item for item in args)


@pytest.mark.asyncio
async def test_resume_also_requires_explicit_trusted_host_mode() -> None:
    agent = CodexAgent(trusted_host=False)

    with pytest.raises(RuntimeError, match="trusted-host"):
        await agent.resume("session-1", "continue", cwd=Path("/worker/repos/flow-1"))


@pytest.mark.parametrize("model,effort", [(None, None), ("custom-codex-model", "high")])
async def test_resume_uses_current_directory_and_disables_saved_broker(monkeypatch, tmp_path, model, effort):
    # A recovered session can retain another directory and an expired INITIAL broker.
    process = AsyncMock(returncode=0, pid=999999999)
    process.stdout, process.stderr = pipe(), pipe()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("app.worker.execution.codex.asyncio.create_subprocess_exec", spawn)

    result = await CodexAgent(trusted_host=True).resume("session-1", "revise", cwd=tmp_path,
                                                      model=model, effort=effort)

    args, kwargs = spawn.call_args
    assert kwargs["cwd"] == tmp_path
    if model is None:
        assert "--model" not in args and "-m" not in args
    else:
        assert args[args.index("--model") + 1] == model
        assert f'model_reasoning_effort="{effort}"' in args
    assert result.model == model
    assert f'cwd="{tmp_path}"' in args
    assert 'mcp_servers.flow={url="http://127.0.0.1", enabled=false}' in args


@pytest.mark.parametrize("events,error,unavailable", [
    (b"", ("Error: thread/resume: thread/resume failed: no rollout found for thread id "
     "session-1 (code -32600)"), True),
    (b'{"type":"thread.started","thread_id":"session-1"}\n',
     ("Error: thread/resume: thread/resume failed: no rollout found for thread id "
     "session-1 (code -32600)"), False),
    (b"", "Error: network timeout", False),
])
async def test_cli_session_rejection_is_classified_only_before_any_events(
    monkeypatch, tmp_path, events, error, unavailable,
):
    class Process:
        pid = 999999999
        returncode = 1
        stdout = pipe(events)
        stderr = pipe(error.encode())

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr("app.worker.execution.codex.asyncio.create_subprocess_exec", spawn)
    agent = CodexAgent(trusted_host=True)
    if unavailable:
        with pytest.raises(SessionUnavailable):
            await agent.resume("session-1", "fix", cwd=tmp_path)
    else:
        run = await agent.resume("session-1", "fix", cwd=tmp_path)
        assert run.exit_code == 1


async def test_cancellation_reaps_codex_group_before_removing_config(monkeypatch, tmp_path):
    existing = asyncio.all_tasks()
    entered = asyncio.Event()
    stopped = []
    files = []

    async def read(_size):
        entered.set()
        await asyncio.Future()

    class Process:
        pid = 999999999
        returncode = None
        stdout = AsyncMock()
        stdout.read.side_effect = read
        stderr = pipe()

        async def wait(self):
            stopped.append("waited")

    async def spawn(*args, **kwargs):
        assert kwargs["start_new_session"]
        files.append(Path(args[args.index("-o") + 1]))
        return Process()

    monkeypatch.setattr("app.worker.execution.codex.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("app.worker.execution.codex.os.killpg", lambda pid, sig: stopped.append((pid, sig)))
    running = asyncio.create_task(CodexAgent(trusted_host=True).resume(
        "session-1", "fix", cwd=tmp_path,
    ))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert stopped[-2:] == [(Process.pid, signal.SIGKILL), "waited"]
    assert not files[0].exists()
    assert not asyncio.all_tasks() - existing


@pytest.mark.parametrize("fd", [1, 2], ids=["stdout", "stderr"])
async def test_pipe_overflow_keeps_initialized_codex_session(monkeypatch, tmp_path, fd):
    # A verbose tool can overflow either pipe after the new rollout already exists.
    sid = "01a15d70-eb3d-722c-9f6d-563dd65eb0a8"
    cli = tmp_path / "codex"
    cli.write_text(f"#!{sys.executable}\nimport os\n"
                   f"print('{{\"type\":\"thread.started\",\"thread_id\":\"{sid}\"}}', flush=True)\n"
                   f"os.write({fd}, b'x' * (8 * 1024 * 1024 + 1))\n")
    cli.chmod(0o700)
    monkeypatch.setattr("app.worker.execution.codex.CODEX_BIN", str(cli))
    result = await CodexAgent(trusted_host=True).start("task", tmp_path)
    assert (result.session_id, result.exit_code, result.failure_kind, result.last_message) == (
        sid, 1, "output_limit", "",
    )


@pytest.mark.parametrize("with_broker", [False, True])
async def test_run_credentials_are_redacted_before_results_leave_driver(monkeypatch, tmp_path, with_broker):
    # Inference has no assignment context, but the CLI can still echo its own login key.
    monkeypatch.setenv("OPENAI_API_KEY", "provider-test-key")
    echoed = "provider-test-key" + (" and mcp-secret" if with_broker else "")
    expected = "[REDACTED]" + (" and [REDACTED]" if with_broker else "")

    class Process:
        pid = 999999999
        returncode = 1
        stdout = pipe()
        stderr = pipe(f"failed with {echoed}".encode())

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        Path(args[args.index("-o") + 1]).write_text(f"echoed {echoed}")
        return Process()

    monkeypatch.setattr("app.worker.execution.codex.asyncio.create_subprocess_exec", spawn)
    result = await CodexAgent(trusted_host=True).start(
        "task", tmp_path, context=CodingExecutionContext(
            mcp_url="http://127.0.0.1:12345/mcp", mcp_token=SecretStr("mcp-secret"),
        ) if with_broker else None,
    )
    assert result.last_message == f"echoed {expected}"
    assert result.failure_detail == f"failed with {expected}"
