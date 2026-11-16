"""Archive proves allocation ownership and survives release/result/ACK crash windows."""

import json
import shutil
from pathlib import Path

import pytest

from app.contracts.protocol import (
    ArchiveResult,
    ArchiveTask,
    AssignmentArguments,
    Failed,
    InitialTaskResult,
    StopFence,
    Succeeded,
)
from app.domain.models import JobState
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.execution.vcs import VcsVerificationError
from app.worker.execution.workspaces import RepositoryWorkspaces
from app.worker.state.execution import ExecutionState, ExecutionStateError
from tests.test_vcs import _git_fixture
from tests.test_worker_executor import RecordingAgent, RecordingToolCaller
from tests.test_worker_session import _assignment
from tests.test_worker_workspaces import acquire


def archive_assignment(
    job_id: int = 10, *, workspace: str = "a" * 32, generation: int = 1,
) -> AssignmentArguments:
    return AssignmentArguments(
        job_id=job_id,
        flow_id=3,
        assignment_token=f"token-{job_id}",
        workspace=workspace,
        generation=generation,
        task=ArchiveTask(
            flow_id=3,
            attempt_id=7,
            repo="owner/repo",
            worker_id="worker-a",
            repo_identity="worker-a:owner/repo",
            delivery_version=2,
        ),
    )


async def workspace(tmp_path: Path, *, start=True, overflow=False, archive=True):
    seed, _ = _git_fixture(tmp_path)
    root = tmp_path / "worker"
    managed_seed = root / "repos/owner/repo"
    managed_seed.parent.mkdir(parents=True)
    shutil.move(seed, managed_seed)
    pool = RepositoryWorkspaces(root, "worker-a", limit=2)
    if overflow:
        await acquire(pool, 999)
    binding = await acquire(pool, 3)
    repo = pool.path(binding)
    journal = ExecutionState(root, 3)
    journal.record_received(_assignment(workspace=binding.slot_id, generation=binding.generation))
    journal.record_started()
    journal.bind_workspace(binding)
    journal.record_terminal(Succeeded(
        job_id=5, assignment_token="assignment-1",
        result=InitialTaskResult(session_id="session", last_message="done", exit_code=0),
    ))
    journal.acknowledge(5, JobState.DONE)
    journal.reconcile_acknowledgement()
    assignment = archive_assignment(workspace=binding.slot_id, generation=binding.generation)
    if archive:
        journal.record_received(assignment)
        if start:
            journal.record_started()
    executor = WorkerExecutor(root, RecordingAgent(), worker_id="worker-a", state=journal,
                              workspaces=pool)
    return managed_seed, repo, journal, executor, assignment


async def test_archive_releases_copy_retains_directory_and_replays_same_fence(tmp_path):
    seed, repo, journal, executor, assignment = await workspace(tmp_path)
    other = await acquire(executor.workspaces, 4)
    binding = journal.load().workspace
    result = await executor.execute(assignment.task, tool_caller=RecordingToolCaller())
    assert isinstance(result, ArchiveResult) and result.cleanup == "released"
    assert (result.flow_id, result.attempt_id, result.repo_identity, result.delivery_version) == (
        3, 7, "worker-a:owner/repo", 2,
    )
    assert repo.is_dir() and seed.is_dir() and executor.workspaces.path(other).is_dir()
    assert journal.path.exists()
    assert (await executor.execute(assignment.task, tool_caller=RecordingToolCaller())).cleanup == "released"
    reused = await acquire(executor.workspaces, 5, previous=binding)
    assert reused.generation == binding.generation + 1
    other_journal = ExecutionState(journal.worker_root, 4)
    other_journal.record_received(_assignment(job_id=11, flow_id=4, workspace=other.slot_id))
    other_bytes = other_journal.path.read_bytes()
    pool_bytes = executor.workspaces.manifest_path.read_bytes()
    journal.complete(result)
    journal.acknowledge(10, JobState.DONE)
    assert journal.path.exists()  # ACK retirement is a journal-only operation, safe after reuse.
    journal.reconcile_acknowledgement()
    assert not journal.path.exists() and other_journal.path.read_bytes() == other_bytes
    assert executor.workspaces.manifest_path.read_bytes() == pool_bytes
    with pytest.raises(VcsVerificationError, match="ownership"):
        await executor.workspaces.release(binding, job_id=10, token="token-10")
    assert executor.workspaces.lookup("owner/repo", 5) == reused


@pytest.mark.parametrize("overflow", [False, True])
async def test_stop_ack_hands_same_physical_workspace_to_merged_archive(tmp_path, overflow):
    # Merge can be observed after stop ACK; archive keeps the original allocation unchanged.
    seed, repo, journal, executor, assignment = await workspace(tmp_path, overflow=overflow, archive=False)
    previous = journal.load()
    binding = previous.workspace
    pool_bytes = executor.workspaces.manifest_path.read_bytes()
    fence = StopFence(request_id="stop", flow_id=3, job_id=5, assignment_token="assignment-1")
    owner = WorkerExecution(worker_id="worker-a", executor=executor, state=journal)
    receipt = await owner.stop(fence)
    assert receipt.execution_nonce == previous.execution_nonce
    owner.on_stop_accepted(fence)
    journal.reconcile_acknowledgement()
    assert executor.workspaces.manifest_path.read_bytes() == pool_bytes
    assert repo.is_dir()
    journal.record_received(assignment)
    journal.record_started()
    result = await executor.execute(assignment.task, tool_caller=RecordingToolCaller())
    assert result.cleanup == ("removed" if overflow else "released")
    assert seed.is_dir()
    journal.complete(result)
    pool_bytes = executor.workspaces.manifest_path.read_bytes()
    journal.acknowledge(assignment.job_id, JobState.DONE)
    journal.reconcile_acknowledgement()
    assert executor.workspaces.manifest_path.read_bytes() == pool_bytes
    item = next(i for i in executor.workspaces.inventory() if i.workspace == binding.slot_id)
    assert item.generation == binding.generation and item.flow_id == binding.flow_id
    assert not journal.path.exists()


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
        if json.loads(payload)["slots"][1]["directory_identity"] is None:
            raise OSError("simulated crash before release commit")
        write(path, payload)

    monkeypatch.setattr(workspaces, "write_private_record", crash_after_removal)
    with pytest.raises(OSError, match="simulated crash"):
        await executor.execute(assignment.task, tool_caller=RecordingToolCaller())
    assert not repo.exists() and journal.load().cleanup_verified
    slot = json.loads(executor.workspaces.manifest_path.read_text())["slots"][1]
    assert (slot["release_job_id"], slot["release_assignment_token"]) == (10, "token-10")
    journal.record_terminal(Failed(job_id=10, assignment_token="token-10",
                                   error="release commit failed"))
    journal.acknowledge(10, JobState.FAILED)
    journal.reconcile_acknowledgement()
    retry = archive_assignment(11, workspace=assignment.workspace, generation=assignment.generation)
    journal.record_received(retry)
    journal.record_started()
    monkeypatch.setattr(workspaces, "write_private_record", write)
    assert (await executor.execute(retry.task, tool_caller=RecordingToolCaller())).cleanup == "already_absent"
    assert seed.exists() and not repo.exists()


async def test_copied_journal_cannot_authorize_another_worker_root(tmp_path):
    _, _, _journal, executor, assignment = await workspace(tmp_path)
    await executor.execute(assignment.task, tool_caller=RecordingToolCaller())
    another_root = tmp_path / "new-root"
    shutil.copytree(tmp_path / "worker", another_root)
    with pytest.raises(ExecutionStateError, match="root"):
        ExecutionState(another_root, 3).load()


async def test_unstarted_archive_reassignment_retains_original_allocation_proof(tmp_path):
    _, _, journal, _executor, assignment = await workspace(tmp_path, start=False)
    binding = journal.load().workspace
    journal.clear_rejected(assignment.job_id)
    journal.record_received(archive_assignment(11, workspace=binding.slot_id,
                                               generation=binding.generation))
    assert journal.load().archive_task == assignment.task
    assert journal.load().workspace == binding


async def test_archive_with_lost_journal_does_not_adopt_an_existing_directory(tmp_path):
    seed, repo, journal, _executor, assignment = await workspace(tmp_path)
    journal.path.unlink()
    with pytest.raises(ExecutionStateError, match="original Flow"):
        journal.record_received(assignment)
    assert seed.is_dir() and repo.is_dir()


@pytest.mark.parametrize("window", ["before-delete", "after-delete"])
async def test_archive_ack_retirement_retries_without_mutating_pool(tmp_path, monkeypatch, window):
    _, _, journal, executor, assignment = await workspace(tmp_path)
    result = await executor.execute(assignment.task, tool_caller=RecordingToolCaller())
    journal.complete(result)
    journal.acknowledge(assignment.job_id, JobState.DONE)
    assert journal.path.exists() and journal.load().retire_after_ack
    pool = executor.workspaces
    before = pool.manifest_path.read_bytes()
    unlink = Path.unlink
    from app.worker.state import execution
    sync = execution.sync_directory

    def interrupted_unlink(path, *args, **kwargs):
        if path == journal.path:
            raise OSError("journal retirement interrupted")
        return unlink(path, *args, **kwargs)

    def interrupted_sync(path):
        if path == journal.directory:
            raise OSError("journal retirement interrupted")
        sync(path)

    if window == "before-delete":
        monkeypatch.setattr(Path, "unlink", interrupted_unlink)
    else:
        monkeypatch.setattr(execution, "sync_directory", interrupted_sync)
    with pytest.raises(OSError, match="retirement interrupted"):
        journal.reconcile_acknowledgement()
    assert journal.path.exists() is (window == "before-delete")
    assert pool.manifest_path.read_bytes() == before
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(execution, "sync_directory", sync)
    restarted = RepositoryWorkspaces(journal.worker_root, "worker-a")
    assert ExecutionState.scan(journal.worker_root) == ([3] if window == "before-delete" else [])
    ExecutionState(journal.worker_root, 3).reconcile_acknowledgement()
    assert not journal.path.exists()
    assert restarted.manifest_path.read_bytes() == before
    item = next(i for i in restarted.inventory() if i.workspace == assignment.workspace)
    assert item.flow_id == assignment.flow_id and item.generation == assignment.generation
