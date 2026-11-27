"""Count changes in the live worktree without staging files or snapshotting jj's @."""

import asyncio
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import anyio

from app.contracts.protocol import WorkspaceDiff
from app.worker.execution.vcs import VcsVerificationError, _required_stdout, _vcs_environment

_HUNK = re.compile(rb"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", re.MULTILINE)


async def workspace_diff(repo: Path, baseline: str) -> WorkspaceDiff:
    """Compare the allocation commit to disk, pairing replaced lines per zero-context hunk."""
    async with asyncio.timeout(8):
        if (repo / ".git").exists():
            git_dir = await _required_stdout("workspace", "git", "rev-parse", "--absolute-git-dir", cwd=repo)
        else:
            git_dir = await _required_stdout("workspace", "jj", "--ignore-working-copy", "git", "root", cwd=repo)
        common = Path(await _required_stdout("workspace", "git", "--git-dir", git_dir, "rev-parse",
                                             "--path-format=absolute", "--git-common-dir", cwd=repo))
        # Use the source objects and ignores, but never its writable index, hooks or diff/clean drivers.
        with tempfile.TemporaryDirectory() as scratch:
            private = Path(scratch) / "git"
            await _required_stdout("workspace", "git", "init", "--bare", "--template=",
                                   f"--object-format={'sha256' if len(baseline) == 64 else 'sha1'}",
                                   str(private), cwd=repo)
            env = {**_vcs_environment(), "GIT_DIR": str(private), "GIT_WORK_TREE": str(repo),
                   "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common / "objects")}
            git = ["git", "-c", f"core.excludesFile={common / 'info/exclude'}"]
            index = Path(git_dir) / "index"
            if (repo / ".git").exists() and index.is_file():
                await anyio.to_thread.run_sync(shutil.copyfile, index, private / "index")
            else:
                await anyio.run_process([*git, "read-tree", baseline], env=env, cwd=repo)
            await anyio.run_process([*git, "add", "--intent-to-add", "--", ".", ":(exclude).jj"],
                                    env=env, cwd=repo, stderr=subprocess.DEVNULL)
            async with await anyio.open_process(
                [*git, "diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "--find-renames",
                 "--no-color", "--unified=0", baseline, "--"], cwd=repo, env=env, stderr=subprocess.DEVNULL
            ) as proc:
                assert proc.stdout is not None
                patch = bytearray()
                try:
                    async for chunk in proc.stdout:
                        if len(patch) + len(chunk) > 8 * 1024 * 1024:
                            raise VcsVerificationError("workspace", "diff exceeds preview limit")
                        patch.extend(chunk)
                    if await proc.wait():
                        raise VcsVerificationError("workspace", "diff unavailable")
                finally:
                    if proc.returncode is None:
                        proc.kill()
    added = deleted = modified = 0
    for hunk in _HUNK.finditer(patch):
        old, new = (int(size or b"1") for size in hunk.groups())
        paired = min(old, new)
        added += new - paired
        deleted += old - paired
        modified += paired
    return WorkspaceDiff(added=added, deleted=deleted, modified=modified)
