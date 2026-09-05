from pathlib import Path

import pytest

from app.codex_driver import CodexAgent, _codex_environment, _start_args


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


@pytest.mark.asyncio
async def test_resume_also_requires_explicit_trusted_host_mode() -> None:
    agent = CodexAgent(trusted_host=False)

    with pytest.raises(RuntimeError, match="trusted-host"):
        await agent.resume("session-1", "continue")
