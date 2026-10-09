from pathlib import Path

import pytest
from pydantic import SecretStr

from app.domain.models import CodingExecutionContext
from app.worker.execution.codex import CodexAgent, _codex_environment, _start_args


async def test_adapter_returns_raw_output_without_interpreting_business_schema(monkeypatch, tmp_path):
    # A caller-owned schema need not contain INITIAL/REVISION fields. The adapter
    # must forward the CLI output; only the task executor knows its business type.
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}')
    raw = '{"caller_owned":"result"}'

    class Process:
        returncode = 0

        async def communicate(self):
            return b'{"type":"thread.started","thread_id":"session-1"}\n', b""

    async def spawn(*args, **kwargs):
        assert args[args.index("--output-schema") + 1] == str(schema)
        Path(args[args.index("-o") + 1]).write_text(raw)
        return Process()

    monkeypatch.setattr("app.worker.execution.codex.asyncio.create_subprocess_exec", spawn)
    run = await CodexAgent(trusted_host=True).start("task", tmp_path, output_schema=schema)
    assert (run.session_id, run.last_message, run.exit_code) == ("session-1", raw, 0)


def test_trusted_host_start_uses_full_access_in_the_flow_repo() -> None:
    args = _start_args("do the task", Path("/worker/repos/flow-1"), trusted_host=True)

    assert args[:3] == ["codex", "exec", "do the task"]
    assert ["-s", "danger-full-access"] == args[args.index("-s") : args.index("-s") + 2]
    assert "workspace-write" not in args
    assert "/worker/repos/flow-1" in args
    assert 'mcp_servers.flow={url="http://127.0.0.1", enabled=false}' in args


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
            "FLOWTOOL_WORKER_TOKEN": "worker-secret",
            "FLOWTOOL_DATABASE_URL": "database-secret",
            "GH_TOKEN": "github-secret",
            "GITHUB_TOKEN": "github-secret",
        }
    )

    assert environment == {
        "PATH": "/usr/bin",
        "HOME": "/home/worker",
        "CODEX_HOME": "/home/worker/.codex",
        "OPENAI_API_KEY": "codex-secret",
        "HTTPS_PROXY": "https://proxy.example",
        "SSL_CERT_FILE": "/etc/ssl/corp.pem",
    }


def test_run_context_injects_only_scoped_push_and_mcp_capabilities() -> None:
    context = CodingExecutionContext(
        push_token=SecretStr("push-secret"),
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

    assert environment["FLOWTOOL_PUSH_TOKEN"] == "push-secret"
    assert environment["GIT_CONFIG_KEY_1"].endswith("helper")
    assert "push-secret" not in " ".join(args)
    assert environment["FLOWTOOL_MCP_TOKEN"] == "mcp-secret"
    assert "mcp-secret" not in " ".join(args)
    assert any('url="http://127.0.0.1:12345/mcp"' in item for item in args)
    assert any('bearer_token_env_var="FLOWTOOL_MCP_TOKEN"' in item for item in args)
    assert not any("mcp_servers.flow.command" in item for item in args)


@pytest.mark.asyncio
async def test_resume_also_requires_explicit_trusted_host_mode() -> None:
    agent = CodexAgent(trusted_host=False)

    with pytest.raises(RuntimeError, match="trusted-host"):
        await agent.resume("session-1", "continue", cwd=Path("/worker/repos/flow-1"))


@pytest.mark.parametrize("mode", ["start", "resume"])
@pytest.mark.parametrize("with_mcp", [False, True])
async def test_run_uses_explicit_directory_and_current_mcp(monkeypatch, tmp_path, mode, with_mcp):
    # A recovered session may save another directory and an expired INITIAL broker.
    # Every invocation must override both from the current run's explicit inputs.
    context = CodingExecutionContext(
        push_token=SecretStr("fresh-push-secret"),
        mcp_url="http://127.0.0.1:12345/mcp" if with_mcp else None,
        mcp_token=SecretStr("fresh-mcp-secret") if with_mcp else None,
    )

    class Process:
        returncode = 0

        async def communicate(self):
            return b'{"type":"thread.started","thread_id":"session-1"}\n', b""

    async def spawn(*args, **kwargs):
        assert kwargs["cwd"] == tmp_path
        if mode == "start":
            assert args[args.index("-C") + 1] == str(tmp_path)
        else:
            assert f'cwd="{tmp_path}"' in args
        if with_mcp:
            assert any('url="http://127.0.0.1:12345/mcp"' in arg for arg in args)
            assert kwargs["env"]["FLOWTOOL_MCP_TOKEN"] == "fresh-mcp-secret"
        else:
            assert 'mcp_servers.flow={url="http://127.0.0.1", enabled=false}' in args
            assert "FLOWTOOL_MCP_TOKEN" not in kwargs["env"]
        assert kwargs["env"]["FLOWTOOL_PUSH_TOKEN"] == "fresh-push-secret"
        assert "fresh-push-secret" not in " ".join(args)
        assert "fresh-mcp-secret" not in " ".join(args)
        return Process()

    monkeypatch.setattr("app.worker.execution.codex.asyncio.create_subprocess_exec", spawn)
    agent = CodexAgent(trusted_host=True)
    if mode == "start":
        await agent.start("task", tmp_path, context=context)
    else:
        await agent.resume("session-1", "revise", cwd=tmp_path, context=context)


@pytest.mark.parametrize("mode", ["start", "resume"])
@pytest.mark.parametrize("context", [
    CodingExecutionContext(mcp_url="http://127.0.0.1:12345/mcp"),
    CodingExecutionContext(mcp_token=SecretStr("orphan-token")),
])
async def test_mcp_requires_endpoint_and_token_together(tmp_path, mode, context):
    agent = CodexAgent(trusted_host=True)
    with pytest.raises(ValueError, match="MCP"):
        if mode == "start":
            await agent.start("task", tmp_path, context=context)
        else:
            await agent.resume("session-1", "revise", cwd=tmp_path, context=context)


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
    from app.domain.models import SessionUnavailable

    class Process:
        returncode = 1

        async def communicate(self):
            return events, error.encode()

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


async def test_revision_deadline_cancels_the_whole_codex_process_group(monkeypatch, tmp_path):
    import asyncio
    import signal

    entered = asyncio.Event()
    stopped = []
    files = []

    class Process:
        pid = 12345

        async def communicate(self):
            entered.set()
            await asyncio.Future()

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
    assert stopped == [(12345, signal.SIGKILL), "waited"]
    assert not files[0].exists()


async def test_mcp_token_is_redacted_before_results_leave_driver(monkeypatch, tmp_path):
    class Process:
        returncode = 1

        async def communicate(self):
            return b"", b"failed with mcp-secret"

    async def spawn(*args, **kwargs):
        Path(args[args.index("-o") + 1]).write_text("echoed mcp-secret")
        return Process()

    monkeypatch.setattr("app.worker.execution.codex.asyncio.create_subprocess_exec", spawn)
    result = await CodexAgent(trusted_host=True).start(
        "task", tmp_path, context=CodingExecutionContext(
            mcp_url="http://127.0.0.1:12345/mcp", mcp_token=SecretStr("mcp-secret"),
        ),
    )
    assert result.last_message == "echoed [REDACTED]"
    assert result.failure_detail == "failed with [REDACTED]"
