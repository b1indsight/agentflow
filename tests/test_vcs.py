from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from app.domain.models import VcsExecutionContext, VcsKind, VcsOutcome
from app.worker.vcs import (
    VcsVerificationError,
    _run,
    _vcs_environment,
    agent_guidance,
    prepare_flow_repo,
    prepare_vcs_context,
    verify_pushed_outcome,
    verify_vcs_outcome,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True, text=True)


def _git_fixture(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _git("init", "--bare", str(remote), cwd=tmp_path)
    _git("init", "-b", "main", str(seed), cwd=tmp_path)
    _git("config", "user.name", "FlowTool Test", cwd=seed)
    _git("config", "user.email", "flowtool@example.test", cwd=seed)
    (seed / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-m", "base", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)
    return seed, remote


async def _jj(*args: str, cwd: Path, env: dict[str, str]) -> None:
    await asyncio.to_thread(
        subprocess.run,
        ("jj", *args),
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_flow_repo_is_reused_by_flow_id_and_separated_between_flows(tmp_path: Path) -> None:
    seed, _ = _git_fixture(tmp_path)

    first = await prepare_flow_repo(seed, 1)
    (first / "flow-marker").write_text("kept")

    assert await prepare_flow_repo(seed, 1) == first
    assert (first / "flow-marker").read_text() == "kept"
    assert await prepare_flow_repo(seed, 2) != first


@pytest.mark.asyncio
async def test_git_outcome_requires_new_clean_commit_on_target_remote_branch(
    tmp_path: Path,
) -> None:
    seed, _ = _git_fixture(tmp_path)
    repo = await prepare_flow_repo(seed, 1)
    context = await prepare_vcs_context(repo, "flow/one")

    _git("checkout", "-b", "flow/one", cwd=repo)
    (repo / "README.md").write_text("changed\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "change", cwd=repo)
    _git("push", "-u", "origin", "flow/one", cwd=repo)

    outcome = await verify_vcs_outcome(context, VcsKind.GIT)

    assert outcome.backend is VcsKind.GIT
    assert outcome.branch == "flow/one"
    assert outcome.commit_sha == outcome.remote_sha
    assert outcome.commit_sha != context.git_base_sha


@pytest.mark.asyncio
async def test_git_outcome_rejects_a_branch_without_a_new_commit(tmp_path: Path) -> None:
    seed, _ = _git_fixture(tmp_path)
    repo = await prepare_flow_repo(seed, 1)
    context = await prepare_vcs_context(repo, "flow/one")
    _git("checkout", "-b", "flow/one", cwd=repo)
    _git("push", "-u", "origin", "flow/one", cwd=repo)

    with pytest.raises(VcsVerificationError, match="new commit") as error:
        await verify_vcs_outcome(context, VcsKind.GIT)

    assert error.value.stage == "commit"


@pytest.mark.asyncio
async def test_declared_backend_must_match_repository_metadata(tmp_path: Path) -> None:
    seed, _ = _git_fixture(tmp_path)
    repo = await prepare_flow_repo(seed, 1)
    context = await prepare_vcs_context(repo, "flow/one")

    with pytest.raises(VcsVerificationError, match="jj") as error:
        await verify_vcs_outcome(context, VcsKind.JJ)

    assert error.value.stage == "discovery"


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("jj") is None, reason="jj is not installed")
async def test_jj_outcome_is_verified_from_a_colocated_repository(tmp_path: Path) -> None:
    seed, _ = _git_fixture(tmp_path)
    env = os.environ | {"JJ_USER": "FlowTool Test", "JJ_EMAIL": "flowtool@example.test"}
    await _jj("git", "init", "--colocate", cwd=seed, env=env)
    repo = await prepare_flow_repo(seed, 1)
    context = await prepare_vcs_context(repo, "flow/jj")
    (repo / "README.md").write_text("changed by jj\n")
    await _jj("commit", "-m", "change", cwd=repo, env=env)
    await _jj("bookmark", "set", "flow/jj", "-r", "@-", cwd=repo, env=env)
    await _jj("git", "push", "-b", "flow/jj", cwd=repo, env=env)

    outcome = await verify_vcs_outcome(context, VcsKind.JJ)

    assert outcome.backend is VcsKind.JJ
    assert outcome.commit_sha == outcome.remote_sha


def test_agent_guidance_requires_self_detection_without_preselecting_a_backend() -> None:
    guidance = agent_guidance("flow/one", remote="origin")

    assert "inspect" in guidance.lower()
    assert "flow/one" in guidance
    assert "origin" in guidance
    assert "uses git" not in guidance.lower()
    assert "uses jujutsu" not in guidance.lower()


def test_vcs_environment_does_not_inherit_worker_credentials() -> None:
    environment = _vcs_environment(
        {"PATH": "/usr/bin", "HOME": "/home/worker", "FLOWTOOL_WORKER_TOKEN": "secret"}
    )

    assert environment["PATH"] == "/usr/bin"
    assert "FLOWTOOL_WORKER_TOKEN" not in environment
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"


@pytest.mark.asyncio
async def test_vcs_command_times_out_instead_of_occupying_the_worker() -> None:
    with pytest.raises(VcsVerificationError, match="timed out") as error:
        await _run(
            "python3",
            "-c",
            "import time; time.sleep(10)",
            timeout=0.01,
            stage="test",
        )

    assert error.value.stage == "test"


@pytest.mark.asyncio
async def test_outer_revision_deadline_kills_and_reaps_vcs_process_group(monkeypatch) -> None:
    processes = []
    killed = []
    spawn, killpg = asyncio.create_subprocess_exec, os.killpg

    async def record_process(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    def record_kill(pid, sig):
        killed.append((pid, sig))
        killpg(pid, sig)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_process)
    monkeypatch.setattr(os, "killpg", record_kill)
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                await _run(sys.executable, "-c", "import time; time.sleep(10)",
                           timeout=30, stage="revision_verification")
        assert len(processes) == 1
        assert killed == [(processes[0].pid, signal.SIGKILL)]
        assert processes[0].returncode is not None
    finally:
        for process in processes:
            if process.returncode is None:
                killpg(process.pid, signal.SIGKILL)
                await process.wait()


@pytest.mark.asyncio
async def test_pushed_outcome_discovers_the_single_backend(monkeypatch) -> None:
    expected = VcsOutcome(
        backend=VcsKind.GIT,
        branch="flow/one",
        commit_sha="abc",
        remote_sha="abc",
    )

    async def fake_verify(context, backend):
        if backend is VcsKind.GIT:
            return expected
        raise VcsVerificationError("discovery", "jj unavailable")

    monkeypatch.setattr("app.worker.vcs.verify_vcs_outcome", fake_verify)

    assert await verify_pushed_outcome(cast(VcsExecutionContext, object())) == expected


@pytest.mark.asyncio
async def test_git_outcome_rejects_unrelated_history(tmp_path: Path) -> None:
    seed, _ = _git_fixture(tmp_path)
    repo = await prepare_flow_repo(seed, 1)
    context = await prepare_vcs_context(repo, "flow/one")
    _git("checkout", "--orphan", "flow/one", cwd=repo)
    _git("rm", "-rf", ".", cwd=repo)
    (repo / "README.md").write_text("unrelated\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "unrelated", cwd=repo)
    _git("push", "-u", "origin", "flow/one", cwd=repo)

    with pytest.raises(VcsVerificationError, match="descend") as error:
        await verify_vcs_outcome(context, VcsKind.GIT)

    assert error.value.stage == "commit"


@pytest.mark.asyncio
async def test_git_outcome_rejects_remote_reconfiguration(tmp_path: Path) -> None:
    seed, _ = _git_fixture(tmp_path)
    repo = await prepare_flow_repo(seed, 1)
    context = await prepare_vcs_context(repo, "flow/one")
    replacement = tmp_path / "replacement.git"
    _git("init", "--bare", str(replacement), cwd=tmp_path)
    _git("checkout", "-b", "flow/one", cwd=repo)
    (repo / "README.md").write_text("changed\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "change", cwd=repo)
    _git("remote", "set-url", "origin", str(replacement), cwd=repo)

    with pytest.raises(VcsVerificationError, match="changed") as error:
        await verify_vcs_outcome(context, VcsKind.GIT)

    assert error.value.stage == "push"
