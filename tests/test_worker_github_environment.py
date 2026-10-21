"""Offline subprocess contracts for bot-only Git/gh credentials and wrapper lifecycle."""

import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.domain.models import CodingExecutionContext, GitHubCapability
from app.worker.execution.codex import CodexAgent
from app.worker.execution.credentials import git_credential_environment
from app.worker.execution.gh import github_environment
from app.worker.execution.vcs import VcsVerificationError


def capability() -> GitHubCapability:
    return GitHubCapability(SecretStr("current-bot-secret"), "owner/repo",
                            datetime.now(UTC) + timedelta(minutes=5))


def test_git_uses_assignment_even_with_a_personal_helper(tmp_path):
    # macOS/keychain or a preconfigured gh helper otherwise wins before the appended bot helper.
    config = tmp_path / "global.gitconfig"
    subprocess.run(["git", "config", "--file", str(config), "credential.helper",
                    '!f() { printf "username=personal\\npassword=inherited-secret\\n"; }; f'],
                   check=True)
    env = os.environ | git_credential_environment(capability().token) | {
        "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1",
    }
    result = subprocess.run(["git", "credential", "fill"], input="url=https://github.com/owner/repo.git\n\n",
                             env=env, cwd=tmp_path, text=True, capture_output=True, check=True)
    assert "username=x-access-token" in result.stdout
    assert "password=current-bot-secret" in result.stdout
    assert "personal" not in result.stdout and "inherited-secret" not in result.stdout


@pytest.mark.parametrize("resume", [False, True])
async def test_real_agent_process_inherits_bot_gh_and_discards_invocation_environment(
    tmp_path, monkeypatch, resume,
):
    binaries = tmp_path / "tools with spaces"
    binaries.mkdir()
    gh = binaries / "gh"
    gh.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
assert os.environ['GH_TOKEN'] == 'current-bot-secret'
assert os.environ['GH_REPO'] == 'owner/repo'
assert os.environ['GH_HOST'] == 'github.com'
assert 'GITHUB_TOKEN' not in os.environ and 'GH_ENTERPRISE_TOKEN' not in os.environ
assert 'GH_DEBUG' not in os.environ
assert not list(__import__('pathlib').Path(os.environ['GH_CONFIG_DIR']).iterdir())
print(json.dumps({'args':sys.argv[1:], 'stdin':sys.stdin.read()}))
print('fake error', file=sys.stderr)
sys.exit(7)
''')
    gh.chmod(0o700)
    codex = binaries / "codex"
    codex.write_text(f"#!{sys.executable}\n" + f'''import json, os, pathlib, subprocess, sys
args = ['api', 'value with spaces', '"quoted"', '$(touch forbidden)']
config = pathlib.Path(os.environ['GH_CONFIG_DIR'])
wrapper = pathlib.Path(os.environ['PATH'].split(os.pathsep)[0]) / 'gh'
assert 'current-bot-secret' not in wrapper.read_text()
for command in ('gh', {str(gh)!r}):
    result = subprocess.run([command, *args], input='request body', text=True, capture_output=True)
    assert result.returncode == 7 and result.stderr == 'fake error\\n'
    assert json.loads(result.stdout) == {{'args':args, 'stdin':'request body'}}
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(str(config)+'\\ncurrent-bot-secret')
print('{{"type":"thread.started","thread_id":"session"}}')
''')
    codex.chmod(0o700)
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_TOKEN", "personal")
    monkeypatch.setenv("GITHUB_TOKEN", "personal")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "personal-enterprise")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "personal"))
    monkeypatch.setenv("GH_DEBUG", "api")
    agent = CodexAgent(trusted_host=True)
    context = CodingExecutionContext(github=capability())
    result = (await agent.resume("session", "task", cwd=tmp_path, context=context) if resume
              else await agent.start("task", tmp_path, context=context))
    assert result.exit_code == 0, result.failure_detail
    config, redacted = result.last_message.splitlines()
    assert redacted == "[REDACTED]" and not Path(config).parent.exists()
    assert not (tmp_path / "forbidden").exists()


async def test_expired_wrapper_refuses_to_exec_and_cancel_reaps_before_cleanup(tmp_path, monkeypatch):
    binaries = tmp_path / "tools"
    binaries.mkdir()
    gh = binaries / "gh"
    gh.write_text("#!/bin/sh\nprintf 'must not execute'\n")
    gh.chmod(0o700)
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    expired = replace(capability(), expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(VcsVerificationError, match="expired"):
        github_environment(expired, tmp_path, os.environ)
    env = github_environment(capability(), tmp_path, os.environ)
    wrapper = Path(env["PATH"].split(os.pathsep)[0]) / "gh"
    # Advance the wrapper clock without a sleep or changing the prepared capability.
    wrapper.write_text(wrapper.read_text().replace("$(/bin/date +%s)", "9999999999"))
    result = await asyncio.to_thread(subprocess.run, [str(wrapper)], env=env, text=True,
                                    capture_output=True, check=False)
    assert result.returncode != 0 and result.stdout == "" and "expired" in result.stderr

    entered = asyncio.Event()
    seen = []

    class Process:
        pid = 12345

        async def communicate(self):
            entered.set()
            await asyncio.Future()

        async def wait(self):
            assert seen[0].exists()  # Private files survive until the process group is reaped.

    async def spawn(*args, **kwargs):
        seen.append(Path(kwargs["env"]["GH_CONFIG_DIR"]))
        return Process()

    monkeypatch.setattr("app.worker.execution.codex.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("app.worker.execution.codex.os.killpg", lambda *_: None)
    run = asyncio.create_task(CodexAgent(trusted_host=True).start(
        "task", tmp_path, context=CodingExecutionContext(github=capability()),
    ))
    await entered.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert not seen[0].parent.exists()
