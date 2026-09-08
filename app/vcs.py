"""Flow-owned repository preparation and post-run VCS verification.

CodeAgent chooses and operates the repository's native VCS. This module does not choose on its
behalf or perform commits; it snapshots every available backend before the run and verifies the
backend declared by the Agent afterwards.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from app.credentials import git_credential_environment
from app.models import CodeAgentContext, VcsExecutionContext, VcsKind, VcsOutcome


class VcsVerificationError(RuntimeError):
    """A stable VCS completion-stage failure suitable for an INITIAL result."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


VCS_COMMAND_TIMEOUT_SECONDS = 30.0


def _vcs_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a verification environment without worker or credential state."""

    values = os.environ if source is None else source
    environment = {key: values[key] for key in ("PATH", "LANG", "LC_ALL") if key in values}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


async def _run(
    *args: str,
    cwd: Path | None = None,
    timeout: float = VCS_COMMAND_TIMEOUT_SECONDS,
    stage: str = "discovery",
    environment: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment if environment is not None else _vcs_environment(),
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError) as error:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise VcsVerificationError(stage, f"{args[0]} command timed out after {timeout:g}s") from error
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def flow_repo_path(seed_repo_path: Path, flow_id: int) -> Path:
    """Return the unambiguous directory owned by a stable Flow identity."""

    if flow_id <= 0:
        raise ValueError("flow_id must be positive")
    return seed_repo_path.parent / f"{seed_repo_path.name}--flow-{flow_id}"


async def prepare_flow_repo(seed_repo_path: Path, flow_id: int) -> Path:
    """Create a full repo copy once and reuse it for later Attempts of the same Flow."""

    if not seed_repo_path.is_dir():
        raise FileNotFoundError(f"seed repository does not exist: {seed_repo_path}")
    repo_path = flow_repo_path(seed_repo_path, flow_id)
    if repo_path.exists():
        if not repo_path.is_dir():
            raise RuntimeError(f"Flow repository path is not a directory: {repo_path}")
        return repo_path
    partial_path = repo_path.with_name(f"{repo_path.name}.partial")
    if partial_path.exists():
        await asyncio.to_thread(shutil.rmtree, partial_path)
    try:
        await asyncio.to_thread(shutil.copytree, seed_repo_path, partial_path, symlinks=True)
        partial_path.replace(repo_path)
    except BaseException:
        if partial_path.exists():
            await asyncio.to_thread(shutil.rmtree, partial_path)
        raise
    return repo_path


def flow_cleanup_paths(seed_repo_path: Path, flow_id: int, worker_root: Path) -> tuple[Path, Path]:
    """Validate managed directory boundaries, including ancestors and partial copies."""
    root = worker_root.resolve()
    seed = seed_repo_path.absolute()
    if not seed.is_relative_to(worker_root.absolute()):
        raise ValueError("archive repository escapes worker root")
    relative = seed.relative_to(worker_root.absolute())
    if (len(relative.parts) != 3 or relative.parts[0] != "repos"
            or any(part in {".", ".."} for part in relative.parts)
            or "--flow-" in relative.name):
        raise ValueError("archive repository does not have a managed seed path")
    cursor = worker_root.absolute()
    for component in relative.parts:
        cursor /= component
        if cursor.is_symlink() or not cursor.is_dir():
            raise ValueError("archive repository ancestor is missing or a symlink")
    repo_path = flow_repo_path(seed_repo_path, flow_id)
    paths = repo_path, repo_path.with_name(repo_path.name + ".partial")
    for path in paths:
        if (path.is_symlink() or not path.resolve().is_relative_to(root)
                or path.resolve() in {root, seed.resolve()}
                or (path.exists() and not path.is_dir())):
            raise ValueError("unsafe archive cleanup target")
    return paths


async def cleanup_flow_repo(
    seed_repo_path: Path, flow_id: int, *, worker_root: Path,
) -> Literal["removed", "already_absent"]:
    """Delete validated Flow directories; the caller must journal ownership first."""
    paths = flow_cleanup_paths(seed_repo_path, flow_id, worker_root)
    removed = False
    for path in paths:
        if path.exists():
            deletion = asyncio.create_task(asyncio.to_thread(shutil.rmtree, path))
            try:
                await asyncio.shield(deletion)
            except asyncio.CancelledError:
                # Cancelling to_thread does not stop rmtree. Keep the root locked until it exits.
                await deletion
                raise
            removed = True
    return "removed" if removed else "already_absent"


async def prepare_vcs_context(
    repo_path: Path, branch: str, *, remote: str = "origin"
) -> VcsExecutionContext:
    """Snapshot candidate backend revisions without selecting a backend for CodeAgent."""

    git_base = await _optional_stdout("git", "rev-parse", "HEAD", cwd=repo_path)
    jj_base = await _optional_stdout(
        "jj", "log", "-r", "@", "--no-graph", "-T", "commit_id ++ \"\\n\"", cwd=repo_path
    )
    jj_ancestor = await _optional_stdout(
        "jj",
        "log",
        "-r",
        "parents(@)",
        "--no-graph",
        "-T",
        'commit_id ++ "\\n"',
        cwd=repo_path,
    )
    if git_base is None and jj_base is None:
        raise VcsVerificationError("discovery", "repository has neither usable git nor jj metadata")
    git_remote_url = await _git_remote_url(repo_path, remote) if git_base is not None else None
    jj_remote_url = await _jj_remote_url(repo_path, remote) if jj_base is not None else None
    return VcsExecutionContext(
        repo_path=repo_path,
        branch=branch,
        remote=remote,
        git_remote_url=git_remote_url,
        jj_remote_url=jj_remote_url,
        git_base_sha=git_base,
        jj_base_sha=jj_base,
        jj_ancestor_sha=jj_ancestor,
    )


def agent_guidance(branch: str, *, remote: str = "origin") -> str:
    """Tell CodeAgent the VCS completion contract without preselecting Git or jj."""

    return (
        "Inspect the repository conventions and actual VCS metadata, then choose the repository's "
        "native Git or Jujutsu (jj) workflow yourself. Start from the current checkout, create the "
        f"exact branch or bookmark `{branch}`, make at least one new commit, and push it to "
        f"`{remote}`. Leave no uncommitted changes. Do not open a pull request. In the structured "
        "result set `vcs_backend_used` to exactly `git` or `jj` for the backend you actually used."
    )


async def verify_vcs_outcome(
    context: VcsExecutionContext, declared_backend: VcsKind
) -> VcsOutcome:
    """Verify branch, new commit, cleanliness, and the remote ref for the declared backend."""

    if declared_backend is VcsKind.GIT:
        return await _verify_git(context)
    if declared_backend is VcsKind.JJ:
        return await _verify_jj(context)
    raise VcsVerificationError("discovery", f"unsupported VCS backend: {declared_backend}")


async def verify_pushed_outcome(context: VcsExecutionContext) -> VcsOutcome:
    """Discover and verify the pushed backend before a delivery side effect."""

    outcomes: list[VcsOutcome] = []
    errors: list[str] = []
    for backend in (VcsKind.GIT, VcsKind.JJ):
        try:
            outcomes.append(await verify_vcs_outcome(context, backend))
        except VcsVerificationError as error:
            errors.append(f"{backend.value}: {error}")
    if not outcomes:
        raise VcsVerificationError(
            "delivery", "no pushed VCS outcome could be verified (" + "; ".join(errors) + ")"
        )
    first = outcomes[0]
    if any(
        (item.branch, item.commit_sha, item.remote_sha)
        != (first.branch, first.commit_sha, first.remote_sha)
        for item in outcomes[1:]
    ):
        raise VcsVerificationError("delivery", "VCS backends disagree on the pushed outcome")
    return first


async def _verify_git(context: VcsExecutionContext, *, require_new: bool = True) -> VcsOutcome:
    if context.git_base_sha is None or context.git_remote_url is None:
        raise VcsVerificationError("discovery", "Agent declared git but git metadata is unavailable")
    branch = await _required_stdout(
        "branch", "git", "branch", "--show-current", cwd=context.repo_path
    )
    if branch != context.branch:
        raise VcsVerificationError(
            "branch", f"git branch is {branch or '(detached)'}, expected {context.branch}"
        )
    commit = await _required_stdout("commit", "git", "rev-parse", "HEAD", cwd=context.repo_path)
    if require_new or commit != context.git_base_sha:
        await _require_git_descendant(context.repo_path, context.git_base_sha, commit)
    await _require_clean("git", "status", "--porcelain", "--untracked-files=all", cwd=context.repo_path)
    current_url = await _git_remote_url(context.repo_path, context.remote)
    _require_unchanged_remote(current_url, context.git_remote_url)
    remote = await _remote_sha(context.git_remote_url, context.branch, context.push_context)
    return _outcome(VcsKind.GIT, branch, commit, remote)


async def _verify_jj(context: VcsExecutionContext, *, require_new: bool = True) -> VcsOutcome:
    if (
        context.jj_base_sha is None
        or context.jj_ancestor_sha is None
        or context.jj_remote_url is None
    ):
        raise VcsVerificationError("discovery", "Agent declared jj but jj metadata is unavailable")
    commit = await _required_stdout(
        "commit",
        "jj",
        "log",
        "-r",
        context.branch,
        "--no-graph",
        "-T",
        "commit_id ++ \"\\n\"",
        cwd=context.repo_path,
    )
    if require_new:
        _require_new_commit(commit, context.jj_base_sha)
    await _require_jj_descendant(context.repo_path, context.jj_ancestor_sha, commit)
    await _require_clean("jj", "diff", "--summary", cwd=context.repo_path)
    current_url = await _jj_remote_url(context.repo_path, context.remote)
    _require_unchanged_remote(current_url, context.jj_remote_url)
    remote = await _remote_sha(context.jj_remote_url, context.branch, context.push_context)
    return _outcome(VcsKind.JJ, context.branch, commit, remote)


async def prepare_revision_context(
    repo_path: Path, branch: str, expected_sha: str,
    push_context: CodeAgentContext | None = None,
) -> VcsExecutionContext:
    """Require the existing clean Flow repo to match the frozen remote head; never reset it."""
    if not repo_path.is_dir():
        raise VcsVerificationError("binding", "existing Flow repository is missing")
    context = await prepare_vcs_context(repo_path, branch)
    # A colocated jj working-copy commit may differ from its pushed bookmark. The expected
    # delivery commit, not that mutable working-copy commit, is the revision ancestry anchor.
    context = replace(context, git_base_sha=expected_sha if context.git_base_sha else None,
                      jj_ancestor_sha=expected_sha if context.jj_base_sha else None,
                      push_context=push_context)
    backend = VcsKind.JJ if context.jj_base_sha else VcsKind.GIT
    outcome = await verify_revision_outcome(context, backend)
    if outcome.commit_sha != expected_sha:
        raise VcsVerificationError("baseline", "local/remote head differs from frozen delivery")
    return context


async def verify_revision_outcome(
    context: VcsExecutionContext, backend: VcsKind,
) -> VcsOutcome:
    """Verify a revision's descendant or explicit no-op without demanding a new commit."""
    if backend is VcsKind.GIT:
        return await _verify_git(context, require_new=False)
    outcome = await _verify_jj(context, require_new=False)
    working = await _required_stdout(
        "branch", "jj", "log", "-r", "@", "--no-graph", "-T", "commit_id",
        cwd=context.repo_path,
    )
    parents = await _required_stdout(
        "branch", "jj", "log", "-r", "parents(@)", "--no-graph", "-T", 'commit_id ++ "\\n"',
        cwd=context.repo_path,
    )
    if working != outcome.commit_sha and parents != outcome.commit_sha:
        raise VcsVerificationError("branch", "jj working copy is not at the delivered bookmark")
    return outcome


def _require_new_commit(commit: str, base: str) -> None:
    if commit == base:
        raise VcsVerificationError("commit", "target branch has no new commit")


async def _require_git_descendant(repo_path: Path, base: str, commit: str) -> None:
    _require_new_commit(commit, base)
    code, _, _ = await _run(
        *_git_args("merge-base", "--is-ancestor", base, commit), cwd=repo_path, stage="commit"
    )
    if code != 0:
        raise VcsVerificationError("commit", "target commit does not descend from the captured base")


async def _require_jj_descendant(repo_path: Path, base: str, commit: str) -> None:
    ancestor = await _required_stdout(
        "commit",
        "jj",
        "log",
        "-r",
        f"{base} & ancestors({commit})",
        "--no-graph",
        "-T",
        'commit_id ++ "\\n"',
        cwd=repo_path,
    )
    if ancestor != base:
        raise VcsVerificationError("commit", "target commit does not descend from the captured base")


async def _require_clean(*args: str, cwd: Path) -> None:
    changes = await _required_stdout("workspace", *args, cwd=cwd)
    if changes:
        raise VcsVerificationError("workspace", "repository has uncommitted changes")


async def _jj_remote_url(repo_path: Path, remote: str) -> str:
    remotes = await _required_stdout("push", "jj", "git", "remote", "list", cwd=repo_path)
    for line in remotes.splitlines():
        name, separator, url = line.partition(" ")
        if name == remote and separator and url.strip():
            return _canonical_remote_url(url.strip(), repo_path)
    raise VcsVerificationError("push", f"jj remote {remote!r} is not configured")


async def _git_remote_url(repo_path: Path, remote: str) -> str:
    url = await _required_stdout(
        "push", "git", "remote", "get-url", "--push", remote, cwd=repo_path
    )
    return _canonical_remote_url(url, repo_path)


def _canonical_remote_url(url: str, repo_path: Path) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "https":
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise VcsVerificationError("push", "remote URL must not contain embedded credentials")
        return url
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            raise VcsVerificationError("push", "file remote must be local")
        return str(Path(parsed.path).resolve())
    if not parsed.scheme and ":" not in url:
        path = Path(url)
        return str((repo_path / path).resolve() if not path.is_absolute() else path.resolve())
    raise VcsVerificationError("push", "remote must use HTTPS or a local path")


def _require_unchanged_remote(current: str, captured: str) -> None:
    if current != captured:
        raise VcsVerificationError("push", "configured remote changed during CodeAgent execution")


async def _remote_sha(
    remote: str, branch: str, push_context: CodeAgentContext | None = None,
) -> str:
    # Run outside the agent-controlled repository so local config cannot rewrite the fixed URL.
    with tempfile.TemporaryDirectory(prefix="flowtool-vcs-verify-") as directory:
        environment = _vcs_environment()
        if push_context is not None:
            environment.update(git_credential_environment(push_context))
        code, output, _ = await _run(
            "git",
            "ls-remote",
            "--heads",
            remote,
            f"refs/heads/{branch}",
            cwd=Path(directory),
            stage="push",
            environment=environment,
        )
        if code != 0:
            raise VcsVerificationError("push", f"remote-ref query failed with exit {code}")
    lines = output.splitlines()
    if len(lines) != 1:
        raise VcsVerificationError("push", f"remote branch {branch!r} is missing or ambiguous")
    return lines[0].split(maxsplit=1)[0]


def _outcome(backend: VcsKind, branch: str, commit: str, remote: str) -> VcsOutcome:
    if commit != remote:
        raise VcsVerificationError("push", "local commit does not match the remote branch")
    return VcsOutcome(
        declared_backend=backend,
        verified_backend=backend,
        branch=branch,
        commit_sha=commit,
        remote_sha=remote,
    )


async def _optional_stdout(*args: str, cwd: Path) -> str | None:
    try:
        command = _git_args(*args[1:]) if args and args[0] == "git" else args
        code, stdout, _ = await _run(*command, cwd=cwd)
    except FileNotFoundError:
        return None
    return stdout.strip() if code == 0 and stdout.strip() else None


async def _required_stdout(stage: str, *args: str, cwd: Path) -> str:
    try:
        command = _git_args(*args[1:]) if args and args[0] == "git" else args
        code, stdout, _ = await _run(*command, cwd=cwd, stage=stage)
    except FileNotFoundError as error:
        raise VcsVerificationError(stage, f"required executable is unavailable: {args[0]}") from error
    if code != 0:
        raise VcsVerificationError(stage, f"{args[0]} command failed with exit {code}")
    return stdout.strip()


def _git_args(*args: str) -> tuple[str, ...]:
    """Disable repository-controlled Git hooks, helpers, and external protocols."""

    return (
        "git",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.ext.allow=never",
        *args,
    )
