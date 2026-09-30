from pathlib import Path

import pytest
from pydantic import SecretStr

from app.codex_driver import CodexAgent, _codex_environment, _start_args
from app.models import CodeAgentContext


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

    monkeypatch.setattr("app.codex_driver.asyncio.create_subprocess_exec", spawn)
    run = await CodexAgent(trusted_host=True).start("task", tmp_path, output_schema=schema)
    assert (run.session_id, run.last_message, run.exit_code) == ("session-1", raw, 0)


def test_trusted_host_start_uses_full_access_in_the_flow_repo() -> None:
    args = _start_args("do the task", Path("/worker/repos/flow-1"), trusted_host=True)

    assert args[:3] == ["codex", "exec", "do the task"]
    assert ["-s", "danger-full-access"] == args[args.index("-s") : args.index("-s") + 2]
    assert "workspace-write" not in args
    assert "/worker/repos/flow-1" in args


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
    context = CodeAgentContext(
        push_token=SecretStr("push-secret"),
        push_repository="owner/repo",
        mcp_socket=Path("/tmp/flowtool.sock"),
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
    assert any("mcp_servers.flow.command" in item for item in args)
    assert any("FLOWTOOL_MCP_SOCKET" in item for item in args)
    assert 'mcp_servers.flow.env.FLOWTOOL_PUSH_TOKEN=""' in args


@pytest.mark.asyncio
async def test_resume_also_requires_explicit_trusted_host_mode() -> None:
    agent = CodexAgent(trusted_host=False)

    with pytest.raises(RuntimeError, match="trusted-host"):
        await agent.resume("session-1", "continue")


async def test_resume_refreshes_environment_and_disables_initial_broker(monkeypatch, tmp_path):
    from app.models import AgentRun

    observed = {}

    async def fake_run(args, output_schema, **kwargs):
        observed.update(args=args, **kwargs)
        return AgentRun("session-1", "", 0)

    agent = CodexAgent(trusted_host=True)
    monkeypatch.setattr(agent, "_run", fake_run)
    context = CodeAgentContext(cwd=tmp_path, push_token=SecretStr("ghs_unique_fresh_capability"),
                               push_repository="owner/repo")
    await agent.resume("session-1", "revise", context=context)
    assert observed["context"] is context
    assert "mcp_servers.flow.enabled=false" in observed["args"]
    assert f'cwd="{tmp_path}"' in observed["args"]
    assert "ghs_unique_fresh_capability" not in " ".join(observed["args"])


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
    from app.models import SessionUnavailable

    class Process:
        returncode = 1

        async def communicate(self):
            return events, error.encode()

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr("app.codex_driver.asyncio.create_subprocess_exec", spawn)
    agent = CodexAgent(trusted_host=True)
    if unavailable:
        with pytest.raises(SessionUnavailable):
            await agent.resume("session-1", "fix", context=CodeAgentContext(cwd=tmp_path))
    else:
        run = await agent.resume("session-1", "fix", context=CodeAgentContext(cwd=tmp_path))
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

    monkeypatch.setattr("app.codex_driver.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("app.codex_driver.os.killpg", lambda pid, sig: stopped.append((pid, sig)))
    running = asyncio.create_task(CodexAgent(trusted_host=True).resume(
        "session-1", "fix", context=CodeAgentContext(cwd=tmp_path),
    ))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert stopped == [(12345, signal.SIGKILL), "waited"]
    assert not files[0].exists()
