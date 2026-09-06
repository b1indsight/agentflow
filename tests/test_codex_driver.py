from pathlib import Path

import pytest
from pydantic import SecretStr

from app.codex_driver import CodexAgent, _codex_environment, _start_args
from app.models import CodeAgentContext


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
