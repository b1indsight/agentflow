"""Archive must prove ownership before deletion and retain that proof across crashes."""

import asyncio
import shutil
import threading
from pathlib import Path

import pytest

from app.contracts.protocol import (
    ArchiveResult,
    ArchiveTask,
    Assignment,
    Failed,
    InitialResult,
    Succeeded,
)
from app.domain.models import JobState
from app.worker.executor import WorkerExecutor
from app.worker.journal import JournalError, WorkerJournal
from app.worker.vcs import prepare_flow_repo
from tests.test_vcs import _git_fixture
from tests.test_worker_executor import RecordingAgent, RecordingJudge, RecordingToolCaller
from tests.test_worker_journal import _assignment


def archive_assignment(job_id: int = 10) -> Assignment:
    return Assignment(
        job_id=job_id, flow_id=3, assignment_token=f"token-{job_id}", connection_epoch="epoch",
        task=ArchiveTask(flow_id=3, attempt_id=7, repo="owner/repo", worker_id="worker-a",
                         repo_identity="worker-a:owner/repo", delivery_version=2),
    )


async def workspace(tmp_path: Path, *, start=True):
    seed, _ = _git_fixture(tmp_path)
    root = tmp_path / "worker"
    managed_seed = root / "repos/owner/repo"
    managed_seed.parent.mkdir(parents=True)
    shutil.move(seed, managed_seed)
    repo = await prepare_flow_repo(managed_seed, 3)
    journal = WorkerJournal(root)
    journal.record_received(_assignment())
    journal.record_started()
    journal.record_terminal(Succeeded(
        job_id=5, assignment_token="assignment-1", connection_epoch="epoch-1",
        result=InitialResult(session_id="session", last_message="done", exit_code=0),
    ))
    journal.acknowledge(5, JobState.DONE)
    assignment = archive_assignment()
    journal.record_received(assignment)
    if start:
        journal.record_started()
    executor = WorkerExecutor(root, RecordingAgent(), RecordingJudge(),
                              worker_id="worker-a", journal=journal)
    return managed_seed, repo, journal, executor, assignment


async def test_cleanup_preserves_seed_other_flow_journal_and_internal_symlink_target(tmp_path):
    seed, repo, journal, executor, assignment = await workspace(tmp_path)
    other = await prepare_flow_repo(seed, 4)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (repo / "link").symlink_to(outside, target_is_directory=True)
    partial = repo.with_name(repo.name + ".partial")
    partial.mkdir()

    result = await executor.execute(assignment.task, tool_caller=RecordingToolCaller())

    assert isinstance(result, ArchiveResult) and result.cleanup == "removed"
    assert (result.flow_id, result.attempt_id, result.repo_identity, result.delivery_version) == (
        3, 7, "worker-a:owner/repo", 2,
    )
    assert not repo.exists() and not partial.exists()
    assert seed.is_dir() and other.is_dir() and journal.path.exists()
    assert (outside / "keep").read_text() == "keep"
    # A crash after removal but before the terminal journal write must reuse durable proof.
    assert (await executor.execute(assignment.task, tool_caller=RecordingToolCaller())).cleanup == "already_absent"


@pytest.mark.parametrize("case", ["absent", "top-symlink", "partial-symlink", "parent-symlink",
                                  "wrong-worker", "wrong-repo", "wrong-attempt", "escape"])
async def test_cleanup_rejects_missing_ownership_or_unsafe_target(tmp_path, case):
    seed, repo, _journal, executor, assignment = await workspace(tmp_path)
    task = assignment.task
    if case == "absent":
        shutil.rmtree(repo)
    elif case == "top-symlink":
        shutil.rmtree(repo)
        repo.symlink_to(seed, target_is_directory=True)
    elif case == "partial-symlink":
        repo.with_name(repo.name + ".partial").symlink_to(seed, target_is_directory=True)
    elif case == "parent-symlink":
        real = seed.parent.with_name("real-owner")
        seed.parent.rename(real)
        seed.parent.symlink_to(real, target_is_directory=True)
    elif case == "escape":
        task = task.model_copy(update={"repo": "../../outside"})
    else:
        field, value = {
            "wrong-worker": ("worker_id", "worker-b"),
            "wrong-repo": ("repo_identity", "worker-a:owner/other"),
            "wrong-attempt": ("attempt_id", 8),
        }[case]
        task = task.model_copy(update={field: value})

    with pytest.raises((ValueError, JournalError), match="archive|cleanup|repository"):
        await executor.execute(task, tool_caller=RecordingToolCaller())
    assert seed.is_dir()


async def test_failed_cleanup_keeps_proof_through_ack_and_explicit_retry(tmp_path, monkeypatch):
    seed, repo, journal, executor, assignment = await workspace(tmp_path)
    partial = repo.with_name(repo.name + ".partial")
    partial.mkdir()
    original = shutil.rmtree

    def fail_partial(path, *args, **kwargs):
        if Path(path) == partial:
            raise PermissionError("cleanup denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", fail_partial)
    with pytest.raises(PermissionError):
        await executor.execute(assignment.task, tool_caller=RecordingToolCaller())
    assert not repo.exists() and partial.exists()
    journal.record_terminal(Failed(job_id=10, assignment_token="token-10",
                                   connection_epoch="epoch", error="cleanup denied"))
    journal.acknowledge(10, JobState.FAILED)
    assert journal.load() is not None
    retry = archive_assignment(11)
    journal.record_received(retry)
    journal.record_started()
    monkeypatch.setattr(shutil, "rmtree", original)
    assert (await executor.execute(retry.task, tool_caller=RecordingToolCaller())).cleanup == "removed"
    assert seed.exists() and not partial.exists()


async def test_copied_journal_cannot_authorize_another_worker_root(tmp_path):
    _, _, journal, executor, assignment = await workspace(tmp_path)
    await executor.execute(assignment.task, tool_caller=RecordingToolCaller())
    another_root = tmp_path / "new-root"
    shutil.copytree(journal.path.parent, another_root)
    moved = WorkerJournal(another_root)
    with pytest.raises(JournalError, match="root"):
        moved.load()


async def test_unstarted_archive_reassignment_retains_original_root_proof(tmp_path):
    _, _, journal, _executor, assignment = await workspace(tmp_path, start=False)
    # A delivery can disconnect before Started reaches serve. Requeue must keep root provenance.
    journal.clear_rejected(assignment.job_id)
    journal.record_received(archive_assignment(11))
    assert journal.load().archive_task == assignment.task


async def test_archive_with_lost_journal_does_not_adopt_an_existing_directory(tmp_path):
    seed, repo, journal, _executor, assignment = await workspace(tmp_path)
    journal.path.unlink()
    with pytest.raises(JournalError, match="original Flow"):
        journal.record_received(assignment)
    assert seed.is_dir() and repo.is_dir()


async def test_rejected_unstarted_coding_retains_flow_provenance_for_archive(tmp_path):
    seed, repo, journal, executor, assignment = await workspace(tmp_path, start=False)
    # Reproduce merge after an ordinary task was journaled but before it could start.
    journal.path.unlink()
    journal.record_received(_assignment())
    journal.clear_rejected(5)
    journal.record_received(assignment)
    journal.record_started()
    assert (await executor.execute(assignment.task, tool_caller=RecordingToolCaller())).cleanup == "removed"
    assert seed.exists() and not repo.exists()


async def test_cancelling_archive_waits_for_filesystem_thread_to_stop(tmp_path, monkeypatch):
    _, repo, journal, executor, assignment = await workspace(tmp_path)
    entered = asyncio.Event()
    release = threading.Event()
    original = shutil.rmtree
    loop = asyncio.get_running_loop()

    def delayed_delete(path):
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        original(path)

    monkeypatch.setattr(shutil, "rmtree", delayed_delete)
    execution = asyncio.create_task(executor.execute(assignment.task, tool_caller=RecordingToolCaller()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        execution.cancel()
        await asyncio.sleep(0)
        assert not execution.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await execution
    assert not repo.exists() and journal.load().cleanup_verified
