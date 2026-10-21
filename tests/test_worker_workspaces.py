"""Public workspace lifecycle: bounded reuse, bootstrap and ownership fencing."""

import asyncio
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from app.worker.execution.vcs import VcsVerificationError
from app.worker.execution.workspaces import RepositoryWorkspaces
from tests.test_vcs import _git, _git_fixture, _jj
from tests.test_worker_executor import assignment_secret


def managed_seed(tmp_path: Path) -> tuple[Path, Path]:
    seed, remote = _git_fixture(tmp_path)
    root = tmp_path / "worker"
    target = root / "repos/owner/repo"
    target.parent.mkdir(parents=True)
    shutil.move(seed, target)
    (target / ".gitignore").write_text(".venv/\nbuild/\n")
    _git("add", ".gitignore", cwd=target)
    _git("commit", "-m", "ignore caches", cwd=target)
    return root, remote


async def acquire(pool: RepositoryWorkspaces, flow_id: int):
    return await pool.acquire("owner/repo", flow_id, base_branch="main", remote="origin",
                              push_token=assignment_secret().push.token)


async def test_pool_reuses_warm_copy_bounds_overflow_and_fences_late_archive(tmp_path):
    # Review/recovery retains occupancy; only a successful archive returns a copy to the pool.
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a", limit=2)
    first = await acquire(pool, 1)
    repo = pool.path(first)
    (repo / ".venv").mkdir()
    (repo / ".venv/keep").write_text("warm")
    assert await acquire(pool, 1) == first
    overflow = await acquire(pool, 2)
    worktree = pool.path(overflow)
    assert (worktree / ".git").is_file()
    assert not (worktree / ".venv").exists()
    with pytest.raises(VcsVerificationError, match="capacity"):
        await acquire(pool, 3)
    # Lowering the startup limit must not evict a previously occupied location.
    smaller = RepositoryWorkspaces(root, "worker-a", limit=1)
    assert await acquire(smaller, 2) == overflow
    assert await pool.release(first) == "released"
    reused = await acquire(pool, 3)
    assert pool.path(reused) == repo and reused.generation > first.generation
    assert (repo / ".venv/keep").read_text() == "warm"
    with pytest.raises(VcsVerificationError, match="ownership"):
        await pool.release(first)
    assert pool.lookup("owner/repo", 3) == reused
    assert await pool.release(overflow) == "removed"
    assert not worktree.exists()
    registry = await asyncio.to_thread(subprocess.check_output,
        ["git", "worktree", "list", "--porcelain"], cwd=root / "repos/owner/repo", text=True,
    )
    assert str(worktree) not in registry
    replacement = await acquire(pool, 2)
    assert replacement.slot_id == overflow.slot_id and replacement.generation > overflow.generation
    with pytest.raises(VcsVerificationError, match="ownership"):
        await pool.release(overflow)
    assert pool.lookup("owner/repo", 2) == replacement and worktree.exists()


@pytest.mark.parametrize("problem", ["tracked", "unknown", "operation", "symlink"])
async def test_pool_quarantines_unsafe_release_without_discarding_contents(tmp_path, problem):
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a", limit=1)
    binding = await acquire(pool, 1)
    repo = pool.path(binding)
    if problem == "tracked":
        (repo / "README.md").write_text("preserve changes")
    elif problem == "unknown":
        (repo / "unknown").write_text("preserve unknown")
    elif problem == "operation":
        (repo / ".git/MERGE_HEAD").write_text("unsettled")
    else:
        saved = tmp_path / "saved"
        repo.rename(saved)
        repo.symlink_to(saved, target_is_directory=True)
    with pytest.raises(VcsVerificationError):
        await pool.release(binding)
    with pytest.raises(VcsVerificationError, match="capacity"):
        await acquire(pool, 2)
    assert repo.exists()


async def test_pool_rejects_corrupt_manifest_and_unproven_missing_workspace(tmp_path):
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a", limit=2)
    binding = await acquire(pool, 1)
    shutil.rmtree(pool.path(binding))
    with pytest.raises(VcsVerificationError, match="missing"):
        await pool.release(binding)
    manifest = root / "workspace-pool.json"
    manifest.write_text("corrupt")
    with pytest.raises(VcsVerificationError, match="manifest"):
        await acquire(pool, 2)
    assert manifest.read_text() == "corrupt"


async def test_pool_does_not_adopt_a_replacement_at_a_previously_removed_location(tmp_path):
    # An operator may populate an old path after archive; a manifest path alone is not ownership.
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a", limit=2)
    await acquire(pool, 1)
    removed = await acquire(pool, 2)
    path = pool.path(removed)
    assert await pool.release(removed) == "removed"
    shutil.copytree(root / "repos/owner/repo", path)
    with pytest.raises(VcsVerificationError, match="unexpectedly exists"):
        await acquire(pool, 3)
    assert (path / "README.md").read_text() == "base\n"


@pytest.mark.parametrize("empty", [False, True])
async def test_bootstrap_clones_atomically_with_current_token_and_task_baseline(
    tmp_path, monkeypatch, empty,
):
    seed, remote = _git_fixture(tmp_path)
    _git("branch", "trunk", cwd=seed)
    _git("push", "origin", "trunk", cwd=seed)
    root = tmp_path / "worker"
    target = root / "repos/owner/repo"
    if empty:
        target.mkdir(parents=True)
    from app.worker.execution import vcs
    original = vcs._run
    clones = []

    async def local_remote(*args, **kwargs):
        if "clone" in args:
            assert "https://github.com/owner/repo.git" in args
            assert kwargs["environment"]["FLOWTOOL_PUSH_TOKEN"] == "push-token"
            assert "push-token" not in " ".join(args)
            clones.append(args)
            args = tuple(str(remote) if arg == "https://github.com/owner/repo.git" else arg
                         for arg in args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(vcs, "_run", local_remote)
    pool = RepositoryWorkspaces(root, "worker-a", limit=2)
    binding = await pool.acquire("owner/repo", 1, base_branch="trunk", remote="upstream",
                                 push_token=assignment_secret().push.token)
    assert len(clones) == 1 and (target / ".git").is_dir()
    repo = pool.path(binding)
    assert (await asyncio.to_thread(subprocess.check_output, ["git", "remote"], cwd=repo,
                                   text=True)).strip() == "upstream"
    assert await asyncio.to_thread(subprocess.check_output,
                                   ["git", "show", "HEAD:README.md"], cwd=repo) == b"base\n"
    assert not list(target.parent.glob("repo.clone-*"))
    assert "push-token" not in (root / "workspace-pool.json").read_text()


@pytest.mark.parametrize("failure", ["exit", "cancel"])
async def test_failed_clone_never_publishes_seed_or_leaves_owned_temporary_data(
    tmp_path, monkeypatch, failure,
):
    root = tmp_path / "worker"

    async def broken(*args, **kwargs):
        if "clone" in args:
            if failure == "cancel":
                raise asyncio.CancelledError()
            return 1, "", "push-token must not escape"
        raise AssertionError(args)

    monkeypatch.setattr("app.worker.execution.vcs._run", broken)
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else VcsVerificationError):
        await acquire(RepositoryWorkspaces(root, "worker-a", limit=2), 1)
    assert not (root / "repos/owner/repo").exists()
    assert not list((root / "repos/owner").glob("repo.clone-*"))


@pytest.mark.parametrize("repository", ["../escape", "owner/../escape", "owner/repo--flow-1"])
async def test_repository_identity_cannot_escape_managed_paths(tmp_path, repository):
    pool = RepositoryWorkspaces(tmp_path, "worker-a", limit=2)
    with pytest.raises(VcsVerificationError):
        await pool.acquire(repository, 1, base_branch="main", remote="origin", push_token=None)
    assert not (tmp_path / "workspace-pool.json").exists()


async def test_preparing_state_after_process_loss_is_not_adopted(tmp_path):
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a", limit=1)
    binding = await acquire(pool, 1)
    manifest = root / "workspace-pool.json"
    payload = json.loads(manifest.read_text())
    payload["slots"][0]["state"] = "preparing"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(VcsVerificationError, match="preparing"):
        await acquire(RepositoryWorkspaces(root, "worker-a", limit=1), 1)
    assert pool.path(binding).exists()


async def test_reuse_refuses_baseline_that_would_overwrite_ignored_cache(tmp_path):
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a", limit=2)
    binding = await acquire(pool, 1)
    repo = pool.path(binding)
    (repo / ".venv").mkdir()
    (repo / ".venv/keep").write_text("cache must survive")
    await pool.release(binding)
    seed = root / "repos/owner/repo"
    (seed / ".venv").mkdir()
    (seed / ".venv/keep").write_text("new tracked source")
    _git("add", "-f", ".venv/keep", cwd=seed)
    _git("commit", "-m", "new baseline conflicts with old cache", cwd=seed)
    with pytest.raises(VcsVerificationError):
        await acquire(pool, 2)
    assert (repo / ".venv/keep").read_text() == "cache must survive"


@pytest.mark.skipif(shutil.which("jj") is None, reason="jj is not installed")
@pytest.mark.parametrize("cache", ["quoted-path", "symlink-parent", "file-parent"])
async def test_jj_reuse_preserves_cache_with_unusual_paths_or_blocking_ancestors(tmp_path, cache):
    # A later baseline may track a formerly ignored cache path, including through an ancestor.
    root, _ = managed_seed(tmp_path)
    seed = root / "repos/owner/repo"
    (seed / ".gitignore").write_text(".venv\nbuild/\n")
    _git("add", ".gitignore", cwd=seed)
    _git("commit", "-m", "ignore cache files as well as directories", cwd=seed)
    await _jj("git", "init", "--colocate", cwd=seed, env={"PATH": os.environ["PATH"]})
    pool = RepositoryWorkspaces(root, "worker-a", limit=1)
    binding = await acquire(pool, 1)
    repo = pool.path(binding)
    name = ' leading "quote"\nline' if cache == "quoted-path" else "keep"
    if cache == "file-parent":
        preserved = repo / ".venv"
        preserved.write_text("cache must survive")
    elif cache == "symlink-parent":
        outside = tmp_path / "outside-cache"
        outside.mkdir()
        preserved = outside / "untouched"
        preserved.write_text("cache must survive")
        (repo / ".venv").symlink_to(outside, target_is_directory=True)
    else:
        (repo / ".venv").mkdir()
        preserved = repo / ".venv" / name
        preserved.write_text("cache must survive")
    await pool.release(binding)
    (seed / ".venv").mkdir()
    (seed / ".venv" / name).write_text("new tracked source")
    _git("add", "-f", f".venv/{name}", cwd=seed)
    _git("commit", "-m", "new baseline conflicts with cache", cwd=seed)
    with pytest.raises(VcsVerificationError, match="overwrite retained cache"):
        await acquire(pool, 2)
    assert preserved.read_text() == "cache must survive"
    if cache == "symlink-parent":
        assert not (outside / name).exists()


@pytest.mark.skipif(shutil.which("jj") is None, reason="jj is not installed")
@pytest.mark.parametrize("colocated", [True, False])
async def test_jj_copy_can_return_to_pool_and_reuse_committed_baseline(tmp_path, colocated):
    root, remote = managed_seed(tmp_path)
    seed = root / "repos/owner/repo"
    environment = {"PATH": os.environ["PATH"]}
    if colocated:
        await _jj("git", "init", "--colocate", cwd=seed, env=environment)
    else:
        shutil.rmtree(seed)
        await _jj("git", "clone", "--no-colocate", "-b", "main", str(remote), str(seed),
                  cwd=root, env=environment)
    pool = RepositoryWorkspaces(root, "worker-a", limit=2)
    binding = await acquire(pool, 1)
    assert (pool.path(binding) / ".jj").is_dir()
    assert await pool.release(binding) == "released"
    (seed / "README.md").write_text("updated committed baseline\n")
    await _jj("describe", "-m", "advance the seed baseline", cwd=seed, env=environment)
    await _jj("bookmark", "set", "main", "-r", "@", cwd=seed, env=environment)
    await _jj("new", cwd=seed, env=environment)
    reused = await acquire(pool, 2)
    assert pool.path(reused) == pool.path(binding)
    assert (pool.path(reused) / "README.md").read_text() == "updated committed baseline\n"


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancelled_copy_waits_for_thread_and_cleans_only_owned_partial(
    tmp_path, monkeypatch, cancellations,
):
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a", limit=2)
    entered, release = asyncio.Event(), threading.Event()
    copytree = shutil.copytree
    loop = asyncio.get_running_loop()

    def delayed_copy(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return copytree(*args, **kwargs)

    monkeypatch.setattr("app.worker.execution.workspaces.shutil.copytree", delayed_copy)
    task = asyncio.create_task(acquire(pool, 1))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    manifest = json.loads((root / "workspace-pool.json").read_text())
    slot = manifest["slots"][0]
    assert slot["state"] == "absent"
    assert not (root / slot["relative_path"]).exists()
    assert (root / "repos/owner/repo/README.md").exists()
