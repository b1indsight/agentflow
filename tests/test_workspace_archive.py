"""Archive proves allocation ownership and survives release/result/ACK crash windows."""

import json
import shutil
from pathlib import Path

import pytest

from app.contracts.protocol import (
    ArchiveResult,
    ArchiveTask,
    Assignment,
    Failed,
    InitialTaskResult,
    Succeeded,
)
from app.domain.models import JobState
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.vcs import VcsVerificationError
from app.worker.execution.workspaces import RepositoryWorkspaces
from app.worker.state.session import SessionError, WorkerSession
from tests.test_vcs import _git_fixture
from tests.test_worker_executor import RecordingAgent, RecordingToolCaller
from tests.test_worker_session import _assignment


def archive_assignment(job_id: int = 10) -> Assignment:
    return Assignment(
        job_id=job_id, flow_id=3, assignment_token=f"token-{job_id}", connection_epoch="epoch",
        task=ArchiveTask(flow_id=3, attempt_id=7, repo="owner/repo", worker_id="worker-a",
                         repo_identity="worker-a:owner/repo", delivery_version=2),
    )


async def workspace(tmp_path: Path, *, start=True, overflow=False):
    seed, _ = _git_fixture(tmp_path)
    root = tmp_path / "worker"
    managed_seed = root / "repos/owner/repo"
    managed_seed.parent.mkdir(parents=True)
    shutil.move(seed, managed_seed)
    pool = RepositoryWorkspaces(root, "worker-a", limit=2)
    if overflow:
        await pool.acquire("owner/repo", 999, base_branch="main", remote="origin", push_token=None)
    binding = await pool.acquire("owner/repo", 3, base_branch="main", remote="origin", push_token=None)
    repo = pool.path(binding)
    journal = WorkerSession(root)
    journal.record_received(_assignment())
    journal.record_started()
    journal.bind_workspace(binding)
    journal.record_terminal(Succeeded(
        job_id=5, assignment_token="assignment-1", connection_epoch="epoch-1",
        result=InitialTaskResult(session_id="session", last_message="done", exit_code=0),
    ))
    journal.acknowledge(5, JobState.DONE)
    assignment = archive_assignment()
    journal.record_received(assignment)
    if start:
        journal.record_started()
    executor = WorkerExecutor(root, RecordingAgent(), worker_id="worker-a", session=journal)
    return managed_seed, repo, journal, executor, assignment


async def test_archive_releases_copy_retains_directory_and_replays_same_fence(tmp_path):
    seed, repo, journal, executor, assignment = await workspace(tmp_path)
    other = await executor.workspaces.acquire("owner/repo", 4, base_branch="main", remote="origin",
                                              push_token=None)
    result = await executor.execute(assignment.task, tool_caller=RecordingToolCaller())
    assert isinstance(result, ArchiveResult) and result.cleanup == "released"
    assert (result.flow_id, result.attempt_id, result.repo_identity, result.delivery_version) == (
        3, 7, "worker-a:owner/repo", 2,
    )
    assert repo.is_dir() and seed.is_dir() and executor.workspaces.path(other).is_dir()
    assert journal.path.exists()
    assert (await executor.execute(assignment.task, tool_caller=RecordingToolCaller())).cleanup == "released"
    reused = await executor.workspaces.acquire("owner/repo", 5, base_branch="main", remote="origin",
                                               push_token=None)
    with pytest.raises(VcsVerificationError, match="ownership"):
        await executor.execute(assignment.task, tool_caller=RecordingToolCaller())
    assert executor.workspaces.lookup("owner/repo", 5) == reused


@pytest.mark.parametrize("case", ["absent", "top-symlink", "parent-symlink"])
async def test_cleanup_rejects_missing_ownership_or_unsafe_target(tmp_path, case):
    seed, repo, _journal, executor, assignment = await workspace(tmp_path)
    if case == "absent":
        shutil.rmtree(repo)
    elif case == "top-symlink":
        shutil.rmtree(repo)
        repo.symlink_to(seed, target_is_directory=True)
    else:
        real = repo.parent.with_name("saved-parent")
        repo.parent.rename(real)
        repo.parent.symlink_to(real, target_is_directory=True)
    with pytest.raises(VcsVerificationError):
        await executor.execute(assignment.task, tool_caller=RecordingToolCaller())
    assert seed.is_dir()


async def test_worktree_removal_crash_keeps_proof_through_ack_and_explicit_retry(tmp_path, monkeypatch):
    seed, repo, journal, executor, assignment = await workspace(tmp_path, overflow=True)
    from app.worker.execution import workspaces
    write = workspaces.write_private_record

    def crash_after_removal(path, payload):
        if json.loads(payload)["slots"][1]["state"] == "absent":
            raise OSError("simulated crash before release commit")
        write(path, payload)

    monkeypatch.setattr(workspaces, "write_private_record", crash_after_removal)
    with pytest.raises(OSError, match="simulated crash"):
        await executor.execute(assignment.task, tool_caller=RecordingToolCaller())
    assert not repo.exists() and journal.load().cleanup_verified
    journal.record_terminal(Failed(job_id=10, assignment_token="token-10",
                                   connection_epoch="epoch", error="release commit failed"))
    journal.acknowledge(10, JobState.FAILED)
    retry = archive_assignment(11)
    journal.record_received(retry)
    journal.record_started()
    monkeypatch.setattr(workspaces, "write_private_record", write)
    assert (await executor.execute(retry.task, tool_caller=RecordingToolCaller())).cleanup == "already_absent"
    assert seed.exists() and not repo.exists()


async def test_copied_journal_cannot_authorize_another_worker_root(tmp_path):
    _, _, journal, executor, assignment = await workspace(tmp_path)
    await executor.execute(assignment.task, tool_caller=RecordingToolCaller())
    another_root = tmp_path / "new-root"
    shutil.copytree(journal.path.parent, another_root)
    with pytest.raises(SessionError, match="root"):
        WorkerSession(another_root).load()


async def test_unstarted_archive_reassignment_retains_original_allocation_proof(tmp_path):
    _, _, journal, _executor, assignment = await workspace(tmp_path, start=False)
    binding = journal.load().workspace
    journal.clear_rejected(assignment.job_id)
    journal.record_received(archive_assignment(11))
    assert journal.load().archive_task == assignment.task
    assert journal.load().workspace == binding


async def test_archive_with_lost_journal_does_not_adopt_an_existing_directory(tmp_path):
    seed, repo, journal, _executor, assignment = await workspace(tmp_path)
    journal.path.unlink()
    with pytest.raises(SessionError, match="original Flow"):
        journal.record_received(assignment)
    assert seed.is_dir() and repo.is_dir()
