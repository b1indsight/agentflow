"""Revision delivery must be proven even when the model exits successfully."""

import asyncio
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr, ValidationError

from app.contracts.protocol import (
    AssignmentArguments,
    AssignmentSecret,
    PushSecret,
    RevisionExecutionRecord,
    RevisionTask,
    RevisionTaskResult,
)
from app.contracts.result_models import ResolveEffect
from app.controler.review.resolution import plan_resolution
from app.domain.models import (
    AddressedComment,
    CodingExecutionContext,
    CodingExecutionResult,
    DeferredComment,
    JobKind,
    JobState,
    ReviewItemKind,
    RevisionCodingOutput,
    SessionUnavailable,
    TriageVerdict,
    VcsKind,
    Verdict,
)
from app.domain.review_models import FeedbackUpdate
from app.github import (
    Batch,
    PullRequest,
    RemoteRef,
    Repository,
    ReviewComment,
    ReviewThread,
    ReviewThreads,
)
from app.worker.execution import vcs
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.vcs import (
    VcsVerificationError,
    local_commit,
    prepare_revision_context,
    verify_revision_outcome,
)
from app.worker.execution.workspaces import RepositoryWorkspaces
from app.worker.state.execution import ExecutionState
from tests.github import NATIVE_PULL_REQUEST_REFETCH
from tests.review_memory import judge_decision
from tests.test_coordinator import apply_result
from tests.test_github_client import AsyncEndpoint, install_client
from tests.test_review_context import pr_data
from tests.test_review_resolution import feedback, human, snapshot
from tests.test_vcs import _git, _git_fixture, _jj
from tests.test_worker_executor import RecordingToolCaller


def revision_output(**updates):
    return RevisionCodingOutput.model_validate({
        "summary": "Already satisfied", "vcs_backend_used": VcsKind.GIT,
        "addressed": [], "deferred": [], "needs_clarification": [], **updates,
    })


@pytest.fixture
async def revision_workspace(tmp_path):
    root = tmp_path / "worker"
    seed, _ = _git_fixture(tmp_path)
    managed_seed = root / "repos/owner/repo"
    managed_seed.parent.mkdir(parents=True)
    shutil.move(seed, managed_seed)
    pool = RepositoryWorkspaces(root, "w", limit=2)
    location = (await pool.provision("owner/repo"))[1]
    binding = await pool.claim("owner/repo", 1, location.workspace, location.generation,
                               base_branch="main", remote="origin", push_token=None)
    target = pool.path(binding)
    _git("checkout", "-b", "flow/1", cwd=target)
    (target / "initial.txt").write_text("published by the first agent\n")
    _git("add", "initial.txt", cwd=target)
    _git("commit", "-m", "Initial delivery", cwd=target)
    _git("push", "origin", "flow/1", cwd=target)
    sha = (await asyncio.to_thread(
        subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=target, text=True,
    )).strip()
    task = RevisionTask(
        flow_id=1, attempt_id=1, batch_id=1, repo="owner/repo", repo_identity="w:owner/repo",
        branch="flow/1", base_branch="main", pr_number=17, expected_head_sha=sha, previous_head_sha=sha,
        session_id="session-1", session_generation=0, instruction="Fix the feedback", feedback=[],
        task_content="Original task\n\nOriginal requirements",
        previous_summary="", open_points=[],
    )
    secret = AssignmentSecret(push=PushSecret(
        repository="owner/repo", token=SecretStr("fresh-token"),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        bot_login="flowtool[bot]", bot_user_id=7654321,
    ))
    journal = ExecutionState(root, 1)
    journal.record_received(
        AssignmentArguments(job_id=1, flow_id=1, assignment_token="token", task=task, secret=secret,
                            workspace=binding.slot_id, generation=binding.generation)
    )
    journal.record_started()
    return root, target, task, secret, pool


@pytest.fixture
def revision_collaborator(revision_workspace, tmp_path):
    _, repo, task, _, _ = revision_workspace
    remote = subprocess.check_output(
        ["git", "remote", "get-url", "origin"], cwd=repo, text=True,
    ).strip()
    collaborator = tmp_path / "collaborator"
    _git("clone", "--branch", task.branch, remote, str(collaborator), cwd=tmp_path)
    _git("config", "user.name", "Collaborator", cwd=collaborator)
    _git("config", "user.email", "collaborator@example.test", cwd=collaborator)
    return collaborator


def publish_external(collaborator, task, *, rewrite=False):
    if rewrite:
        _git("reset", "--hard", "origin/main", cwd=collaborator)
    (collaborator / "external.txt").write_text("external contents\n")
    _git("add", "external.txt", cwd=collaborator)
    _git("commit", "-m", "External update", cwd=collaborator)
    _git("push", "--force" if rewrite else "--no-force", "origin", task.branch, cwd=collaborator)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=collaborator, text=True).strip()


@pytest.fixture(params=[VcsKind.GIT, VcsKind.JJ])
async def revision_backend(revision_workspace, request):
    _, repo, task, _, _ = revision_workspace
    if request.param is VcsKind.JJ:
        if shutil.which("jj") is None:
            pytest.skip("jj is not installed")
        env = os.environ | {"JJ_USER": "FlowTool Test", "JJ_EMAIL": "flowtool@example.test"}
        await _jj("git", "init", "--colocate", cwd=repo, env=env)
        await _jj("bookmark", "track", task.branch, "--remote=origin", cwd=repo, env=env)
    return request.param


class RevisionAgent:
    def __init__(self, repo: Path, behavior="noop"):
        self.repo, self.behavior = repo, behavior
        self.calls: list[tuple[str, CodingExecutionContext | None, str]] = []

    async def resume(self, session_id, prompt, *, cwd, output_schema=None, context=None):
        assert cwd == self.repo
        self.calls.append(("resume", context, prompt))
        if self.behavior == "missing":
            raise SessionUnavailable("session not found before execution")
        if self.behavior == "timeout":
            return CodingExecutionResult(session_id, "", 124)
        if self.behavior in {"unpushed", "dirty", "push"}:
            (self.repo / "change.txt").write_text("revision\n")
            if self.behavior != "dirty":
                _git("add", ".", cwd=self.repo)
                _git("commit", "-m", "revision", cwd=self.repo)
                if self.behavior == "push":
                    _git("push", "origin", "flow/1", cwd=self.repo)
        return CodingExecutionResult(session_id,
                        "" if self.behavior == "empty" else revision_output().model_dump_json(), 0)

    async def start(self, prompt, cwd, *, output_schema=None, context=None):
        assert cwd == self.repo
        self.calls.append(("start", context, prompt))
        return CodingExecutionResult("new-session", revision_output().model_dump_json(), 0)


@pytest.mark.parametrize("rewrite", [False, True])
@pytest.mark.parametrize("noop", [False, True])
async def test_revision_runs_on_fetched_external_contents(
    revision_workspace, revision_collaborator, revision_backend, rewrite, noop,
):
    # The PR read and fetch can observe different heads, including a rewritten history.
    root, repo, task, secret, pool = revision_workspace
    backend = revision_backend
    env = os.environ | {"JJ_USER": "FlowTool Test", "JJ_EMAIL": "flowtool@example.test"}
    observed = publish_external(revision_collaborator, task, rewrite=rewrite)
    task = task.model_copy(update={"expected_head_sha": observed})
    (revision_collaborator / "external.txt").write_text("newer external contents\n")
    _git("commit", "-am", "Update after PR observation", cwd=revision_collaborator)
    _git("push", "origin", task.branch, cwd=revision_collaborator)
    baseline = (await asyncio.to_thread(
        subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=revision_collaborator, text=True,
    )).strip()

    class ExternalAgent(RevisionAgent):
        async def resume(self, session_id, prompt, *, cwd, output_schema=None, context=None):
            self.calls.append(("resume", context, prompt))
            assert (cwd / "external.txt").read_text() == "newer external contents\n"
            assert f"PR observed head: {observed}" in prompt
            assert f"Fetched coding baseline: {baseline}" in prompt
            if not noop:
                (cwd / "agent.txt").write_text("agent change after external update\n")
                if backend is VcsKind.JJ:
                    await _jj("commit", "-m", "Agent revision", cwd=cwd, env=env)
                    await _jj("bookmark", "set", task.branch, "-r", "@-", cwd=cwd, env=env)
                    await _jj("git", "push", "-b", task.branch, cwd=cwd, env=env)
                else:
                    _git("add", "agent.txt", cwd=cwd)
                    _git("commit", "-m", "Agent revision", cwd=cwd)
                    _git("push", "origin", task.branch, cwd=cwd)
            return CodingExecutionResult(
                session_id, revision_output(vcs_backend_used=backend).model_dump_json(), 0,
            )

    agent = ExternalAgent(repo)
    result = await WorkerExecutor(root, agent, state=ExecutionState(root, 1), worker_id="w",
                                  workspaces=pool).execute(task, secret, tool_caller=RecordingToolCaller())
    assert result.exit_code == 0, result.failure_detail
    assert len(agent.calls) == 1
    assert result.baseline_sha == baseline != task.expected_head_sha
    assert result.commit_sha == result.remote_sha
    assert (result.commit_sha == baseline) is noop
    _git("merge-base", "--is-ancestor", baseline, result.commit_sha, cwd=repo)
    if rewrite:
        assert (await asyncio.to_thread(subprocess.run,
            ["git", "merge-base", "--is-ancestor", task.previous_head_sha, result.commit_sha],
            cwd=repo, capture_output=True, check=False,
        )).returncode == 1


async def test_fetch_snapshot_remains_the_baseline_when_remote_changes_before_agent(
    revision_workspace, revision_collaborator, revision_backend, monkeypatch,
):
    # Advance the independent remote immediately after fetch, before any checkout/model call.
    root, repo, task, secret, pool = revision_workspace
    baseline = publish_external(revision_collaborator, task, rewrite=True)
    task = task.model_copy(update={"expected_head_sha": baseline})
    monkeypatch.setenv("FLOWTOOL_WORKER_TOKEN", "worker-secret")
    monkeypatch.setenv("FLOWTOOL_PUSH_TOKEN", "stale-secret")
    run = vcs._run

    async def advance_after_fetch(*args, **kwargs):
        outcome = await run(*args, **kwargs)
        if "fetch" in args and f"refs/heads/{task.branch}" in args:
            assert outcome[0] == 0
            assert kwargs["cwd"] != repo
            assert kwargs["environment"]["FLOWTOOL_PUSH_TOKEN"] == "fresh-token"
            assert "FLOWTOOL_WORKER_TOKEN" not in kwargs["environment"]
            assert all("fresh-token" not in arg and "stale-secret" not in arg for arg in args)
            (revision_collaborator / "external.txt").write_text("later remote contents\n")
            _git("commit", "-am", "Race after fetch", cwd=revision_collaborator)
            _git("push", "origin", task.branch, cwd=revision_collaborator)
        return outcome

    monkeypatch.setattr(vcs, "_run", advance_after_fetch)

    class SnapshotAgent(RevisionAgent):
        async def resume(self, session_id, prompt, *, cwd, output_schema=None, context=None):
            self.calls.append(("resume", context, prompt))
            assert (cwd / "external.txt").read_text() == "external contents\n"
            return CodingExecutionResult(
                session_id, revision_output(vcs_backend_used=revision_backend).model_dump_json(), 0,
            )

    agent = SnapshotAgent(repo)
    result = await WorkerExecutor(root, agent, state=ExecutionState(root, 1), worker_id="w",
                                  workspaces=pool).execute(task, secret, tool_caller=RecordingToolCaller())
    assert len(agent.calls) == 1 and result.baseline_sha == baseline
    assert result.exit_code != 0 and result.failure_stage == "push"
    assert (repo / "external.txt").read_text() == "external contents\n"


async def test_revision_accepts_local_work_published_only_in_new_history(
    revision_workspace, revision_collaborator, revision_backend,
):
    # A local tip need not descend from the historical delivery if the new remote publishes it.
    _, repo, task, _, _ = revision_workspace
    remote = publish_external(revision_collaborator, task, rewrite=True)
    if revision_backend is VcsKind.JJ:
        env = os.environ | {"JJ_USER": "FlowTool Test", "JJ_EMAIL": "flowtool@example.test"}
        await _jj("git", "fetch", "--remote", "origin", "-b", task.branch, cwd=repo, env=env)
        await _jj("new", task.branch, cwd=repo, env=env)
    else:
        _git("fetch", "origin", task.branch, cwd=repo)
        _git("checkout", "-B", task.branch, "FETCH_HEAD", cwd=repo)
    context = await prepare_revision_context(repo, task.branch, task.previous_head_sha)
    outcome = await verify_revision_outcome(context, revision_backend)
    assert context.git_base_sha == outcome.commit_sha == remote


async def test_answer_does_not_synchronize_external_updates(
    revision_workspace, revision_collaborator, revision_backend,
):
    root, repo, task, secret, pool = revision_workspace
    remote = publish_external(revision_collaborator, task, rewrite=True)
    task = task.model_copy(update={"purpose": "answer", "expected_head_sha": remote})
    before = await local_commit(repo)
    agent = RevisionAgent(repo)
    result = await WorkerExecutor(root, agent, state=ExecutionState(root, 1), worker_id="w",
                                  workspaces=pool).execute(task, secret, tool_caller=RecordingToolCaller())
    assert result.exit_code == 0
    assert result.baseline_sha is None
    assert result.commit_sha == before == await local_commit(repo)
    assert (repo / "initial.txt").exists() and not (repo / "external.txt").exists()
    assert "Fetched coding baseline" not in agent.calls[0][2]


@pytest.mark.parametrize("behavior,success", [
    ("noop", True), ("push", True), ("unpushed", False), ("dirty", False), ("empty", False),
])
async def test_revision_verifies_real_git_delivery(revision_workspace, monkeypatch, behavior, success):
    # A successful process can leave unpushed or dirty work; neither closes review feedback.
    root, repo, task, secret, pool = revision_workspace
    binding = pool.lookup(task.repo, task.flow_id)
    before = pool.manifest_path.read_bytes()
    agent = RevisionAgent(repo, behavior)
    if behavior == "noop":
        resume = agent.resume

        async def long_resume(*args, **kwargs):
            # An active agent owns its deadline; an executor-level 1200s timer would cancel it.
            loop = asyncio.get_running_loop()
            real_time = loop.time
            with monkeypatch.context() as clock:
                clock.setattr(loop, "time", lambda: real_time() + 2400)
                # An overdue outer timer fires before this later timer, without scheduler spins.
                await asyncio.sleep(0.01)
            return await resume(*args, **kwargs)

        monkeypatch.setattr(agent, "resume", long_resume)
    result = await WorkerExecutor(root, agent, state=ExecutionState(root, 1), worker_id="w",
                                   workspaces=pool).execute(task, secret, tool_caller=RecordingToolCaller())
    assert (result.exit_code == 0) is success
    assert pool.manifest_path.read_bytes() == before
    assert ExecutionState(root, 1).load().workspace == binding
    assert agent.calls[0][1].github.token.get_secret_value() == "fresh-token"
    assert "flow.open_pr" not in agent.calls[0][2]
    if success:
        assert result.clean_worktree and result.commit_sha == result.remote_sha
        assert (result.commit_sha == task.expected_head_sha) is (behavior == "noop")
        if behavior == "push":
            commit = await asyncio.to_thread(subprocess.check_output,
                ["git", "log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%B"], cwd=repo, text=True,
            )
            assert commit.splitlines()[:2] == [
                "flowtool[bot] <7654321+flowtool[bot]@users.noreply.github.com>",
            ] * 2
            assert "Co-authored-by: FlowTool Test <flowtool@example.test>" in commit


@pytest.mark.parametrize("behavior,success", [
    ("noop", True), ("dirty", True), ("unpushed", False), ("push", False),
])
async def test_answer_checks_only_local_commit(revision_workspace, behavior, success):
    # A clean, committed change must fail; an uncommitted edit is outside this check's scope.
    root, repo, task, secret, pool = revision_workspace
    task = task.model_copy(update={"purpose": "answer"})
    before_config = (repo / ".git/config").read_bytes()
    agent = RevisionAgent(repo, behavior)
    result = await WorkerExecutor(root, agent, state=ExecutionState(root, 1), worker_id="w",
                                  workspaces=pool).execute(task, secret, tool_caller=RecordingToolCaller())
    assert (result.exit_code == 0) is success
    assert "Do not modify code" in agent.calls[0][2]
    assert "Modify, test, commit" not in agent.calls[0][2]
    assert (repo / ".git/config").read_bytes() == before_config
    assert not (repo / ".git/config.worktree").exists()
    if not success:
        assert result.failure_stage == "commit"
        head = await asyncio.to_thread(subprocess.check_output, ["git", "rev-parse", "HEAD"],
                                       cwd=repo, text=True)
        assert head.strip() != task.expected_head_sha
    if behavior == "dirty":
        assert (repo / "change.txt").read_text() == "revision\n"


async def test_historical_revision_uses_expected_head_as_publication_proof(revision_workspace):
    root, repo, task, secret, pool = revision_workspace
    task = task.model_copy(update={"previous_head_sha": None})
    result = await WorkerExecutor(root, RevisionAgent(repo), state=ExecutionState(root, 1),
                                  worker_id="w", workspaces=pool).execute(
        task, secret, tool_caller=RecordingToolCaller())
    assert result.exit_code == 0
    assert result.baseline_sha == result.commit_sha == task.expected_head_sha


@pytest.mark.parametrize("purpose", ["answer", "revise"])
@pytest.mark.parametrize("session,behavior,expected", [
    (None, "noop", ["start"]), ("session-1", "missing", ["resume", "start"]),
    ("session-1", "timeout", ["resume"]),
])
async def test_fallback_only_when_execution_is_known_not_to_have_started(
    revision_workspace, session, behavior, expected, purpose,
):
    root, repo, task, secret, pool = revision_workspace
    # A missing conversation must rebuild from the full direct text, not a generated display title.
    task_content = " \nPreserve these original requirements.\n\nKeep every detail.\t\n "
    task = task.model_copy(update={"session_id": session, "task_content": task_content, "purpose": purpose})
    agent = RevisionAgent(repo, behavior)
    result = await WorkerExecutor(root, agent, state=ExecutionState(root, 1), worker_id="w",
                                   workspaces=pool).execute(task, secret, tool_caller=RecordingToolCaller())
    assert [call[0] for call in agent.calls] == expected
    assert all(call[1].mcp_url is None and call[1].mcp_token is None for call in agent.calls)
    assert all(task_content in call[2] for call in agent.calls)
    if expected[-1] == "start":
        assert result.session_id == "new-session"
        assert result.session_generation == 1
        assert result.mode == "fallback"


@pytest.mark.parametrize("problem", [
    "unpublished", "tracked", "untracked", "cache-file", "cache-directory", "cache-parent",
    "cache-symlink", "branch",
])
async def test_revision_preflight_rejects_before_model_execution(
    revision_workspace, revision_collaborator, revision_backend, problem,
):
    # External synchronization must not erase work, including ignored paths newly tracked upstream.
    root, repo, task, secret, pool = revision_workspace
    env = os.environ | {"JJ_USER": "FlowTool Test", "JJ_EMAIL": "flowtool@example.test"}
    remote = publish_external(revision_collaborator, task, rewrite=True)
    preserved = repo / "README.md"
    if problem == "unpublished":
        preserved.write_text("unpublished work")
        if revision_backend is VcsKind.JJ:
            await _jj("commit", "-m", "Unpublished change", cwd=repo, env=env)
            await _jj("bookmark", "set", task.branch, "-r", "@-", cwd=repo, env=env)
        else:
            _git("commit", "-am", "Unpublished change", cwd=repo)
    elif problem == "tracked":
        preserved.write_text("tracked edits")
    elif problem == "untracked":
        preserved = repo / "uncommitted"
        preserved.write_text("unknown work")
    elif problem.startswith("cache-"):
        # Keep the ignore rule local so the collaborator can start tracking this path.
        with (repo / ".git/info/exclude").open("a") as excluded:
            excluded.write("\n/cache\n")
        if problem in {"cache-file", "cache-directory"}:
            (repo / "cache").mkdir()
            preserved = repo / "cache/retained"
        else:
            preserved = repo / "cache"
        if problem == "cache-symlink":
            preserved.symlink_to("README.md")
        else:
            preserved.write_text("retained cache")
        if problem == "cache-directory":
            (revision_collaborator / "cache").write_text("upstream tracked contents")
        else:
            (revision_collaborator / "cache").mkdir()
            (revision_collaborator / "cache/retained").write_text("upstream tracked contents")
        _git("add", "cache", cwd=revision_collaborator)
        _git("commit", "-m", "Track the retained cache path", cwd=revision_collaborator)
        _git("push", "origin", task.branch, cwd=revision_collaborator)
        remote = (await asyncio.to_thread(
            subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=revision_collaborator, text=True,
        )).strip()
    elif revision_backend is VcsKind.JJ:
        await _jj("new", "root()", cwd=repo, env=env)
    else:
        _git("checkout", "-b", "wrong-branch", cwd=repo)
    before_contents = preserved.read_bytes() if preserved.exists() else None
    before_head = await local_commit(repo)
    task = task.model_copy(update={"expected_head_sha": remote})
    agent = RevisionAgent(repo)
    result = await WorkerExecutor(root, agent, state=ExecutionState(root, 1), worker_id="w",
                                   workspaces=pool).execute(task, secret, tool_caller=RecordingToolCaller())
    assert result.exit_code != 0
    assert agent.calls == []
    assert (preserved.read_bytes() if preserved.exists() else None) == before_contents
    assert await local_commit(repo) == before_head
    if problem == "cache-symlink":
        assert preserved.is_symlink()


def test_revision_output_requires_every_field_and_rejects_extra_data():
    with pytest.raises(ValidationError):
        RevisionCodingOutput.model_validate({"summary": "done"})
    with pytest.raises(ValidationError):
        RevisionCodingOutput.model_validate({**revision_output().model_dump(), "pushed": True})


@pytest.fixture
def revision_feedback():
    return [FeedbackUpdate(
        kind=ReviewItemKind.INLINE, github_id=500, author="reviewer", body="Please fix it.",
        updated_at=datetime.now(UTC), signal="request_changes", submitted=True,
    )]


@pytest.fixture
async def revision_control(store, completed_open_initial, revision_feedback, request):
    """Continue an actual delivered INITIAL through feedback ingestion and revision dispatch."""
    flow, subject, github, _, _ = completed_open_initial
    purpose = getattr(request, "param", "revise")
    for index, update in enumerate(revision_feedback):
        if purpose == "answer":
            update = update.model_copy(update={"signal": "discussion"})
        assert store.reviews.ingest(
            flow.id, f"review-{index}", "pull_request_review_comment", {}, update,
            settle_seconds=0, submit_grace_seconds=0,
        )
    batch, = store.reviews.claim_due()
    item = next(f for f in batch.feedback if f.kind is ReviewItemKind.INLINE)
    github.threads = [ReviewThread(
        thread_id="T1", repo=flow.repo, pr_number=17, path="app.py", resolved=False,
        outdated=False, complete=True, comments=Batch[ReviewComment](items=(ReviewComment(
            github_id=item.github_id, node_id="C500", author=item.author, is_bot=False,
            body=item.body, updated_at=item.updated_at,
        ),)),
    )]
    if purpose == "answer":
        judge = SimpleNamespace(triage=AsyncMock(side_effect=lambda context: judge_decision(
            TriageVerdict(verdict=Verdict.ANSWER, instruction="Explain the existing bound"), context)))
        await subject.triage(batch, judge)
        batch = store.reviews.get_batch(batch.id)
    assert batch.job_id is not None
    job = store.workers.assign_job("worker-a", "revision-token", "epoch")
    assert job is not None and job.id == batch.job_id and job.kind is JobKind.REVISION
    task = await subject.prepare(job)
    assert isinstance(task, RevisionTask)
    assert task.purpose == purpose
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "revision-token", "epoch")
    subject.mark_started(job, task, "worker-a")
    github.comments.clear()
    github.comment_bodies.clear()
    github.review_requests.clear()
    github.labels.clear()
    github.resource_calls.clear()
    return store, github, store.jobs.get_job(job.id), subject, task


def verified_result(task, **updates):
    values = {
        "flow_id": task.flow_id, "attempt_id": task.attempt_id, "batch_id": task.batch_id,
        "feedback_refs": list(task.feedback), "previous_session_id": task.session_id,
        "session_id": task.session_id, "session_generation": task.session_generation,
        "mode": "resume",
        "invocations": [RevisionExecutionRecord(mode="resume", session_id=task.session_id, exit_code=0)],
        "last_message": "done", "exit_code": 0, "output": revision_output(),
        "vcs_backend": VcsKind.GIT, "branch": task.branch, "commit_sha": task.expected_head_sha,
        "remote_sha": task.expected_head_sha, "clean_worktree": True,
        "baseline_sha": task.expected_head_sha if task.purpose == "revise" else None,
    }
    values.update(updates)
    return RevisionTaskResult(**values)


@pytest.mark.parametrize("revision_control", ["answer"], indirect=True)
@pytest.mark.parametrize("change", ["metadata", "head", "closed", "late-head"])
async def test_answer_publication_checks_pr_head_not_worker_delivery(revision_control, monkeypatch, change):
    store, github, job, subject, task = revision_control
    before = store.deliveries.get_delivery(job.flow_id)
    original = PullRequest.refetch
    reads = 0

    async def changed_pr(pr):
        nonlocal reads
        reads += 1
        return (await original(pr)).model_copy(update={
            "title": "Edited PR title", "body": "Updated description",
            "head_sha": "new" if change == "head" or (change == "late-head" and reads > 1) else task.expected_head_sha,
            "state": "closed" if change == "closed" else "open",
        })

    monkeypatch.setattr(PullRequest, "refetch", changed_pr)
    # No ref fetch, clean-tree claim or worker SHA/remote SHA equality is required for answer.
    monkeypatch.setattr(Repository, "fetch_ref", AsyncMock(side_effect=AssertionError("answer read a ref")))
    item = task.feedback[0]
    result = verified_result(task, commit_sha="local-jj-working-commit", remote_sha=None,
                             branch=None, clean_worktree=False, output=revision_output(addressed=[
        AddressedComment(item_id=item.item_id, version=item.version, comment_id=item.github_id,
                         note="The bound is read in app.py:12."),
    ]))
    state = await apply_result(subject, job, result)
    await subject.results.apply(job.id)
    assert store.deliveries.get_delivery(job.flow_id) == before
    assert github.review_requests == []
    if change == "metadata":
        assert state is JobState.DONE
        assert store.reviews.pending(job.flow_id) == []
        assert len(github.thread_replies) == 1
        assert "app.py:12" in github.thread_replies[0]
        assert github.comments == ["问题答复：Already satisfied"]
    else:
        assert state is (JobState.DONE if change == "late-head" else JobState.FAILED)
        assert store.reviews.pending(job.flow_id)
        assert github.comments == github.thread_replies == []


async def test_resolve_payloads_only_carry_their_threads_current_and_historical_feedback(revision_control):
    store, _, job, _, task = revision_control
    threads, current, historical = [], [], []
    # Many long review threads must not duplicate the entire PR's history in every effect.
    for number in range(20):
        root = human(number * 2 + 1, body="Earlier request " + "x" * 1000)
        reply = human(number * 2 + 2, reply_to_id=root.github_id, body="New request " + "y" * 1000)
        threads.append(snapshot(root, reply).model_copy(update={"thread_id": f"T{number}"}))
        historical.extend([feedback(root), feedback(root, kind=ReviewItemKind.REVIEW_BODY)])
        current.append(feedback(reply))
    flow = store.flows.get_flow(job.flow_id)
    remote = ReviewThreads(repo=flow.repo, pr_number=17, fetched_at=datetime.now(UTC), threads=threads)
    batch = store.reviews.for_job(job).model_copy(update={"feedback": current, "threads": remote})
    result = verified_result(task.model_copy(update={"feedback": current}), output=revision_output(
        addressed=[AddressedComment(item_id=f.item_id, version=f.version, comment_id=f.github_id,
                                    note="Fixed") for f in current]))
    plan = plan_resolution(flow, 17, batch, result, historical)
    resolves = [effect for effect in plan.effects if isinstance(effect, ResolveEffect)]
    assert len(resolves) == len(threads) and all(effect.allow_resolve for effect in resolves)
    assert all({f.github_id for f in effect.covered} == {c.github_id for c in effect.snapshot.comments.items}
               and all(f.kind is ReviewItemKind.INLINE for f in effect.covered) for effect in resolves)
    assert sum(len(effect.model_dump_json()) for effect in resolves) < 3 * len(remote.model_dump_json())


async def test_control_preserves_omitted_feedback_and_does_not_rereview_noop(revision_control):
    store, github, job, subject, task = revision_control
    result = verified_result(task)
    await apply_result(subject, job, result)
    await subject.results.apply(job.id)
    assert [(f.item_id, f.version) for f in store.reviews.pending(job.flow_id)] == [
        (f.item_id, f.version) for f in task.feedback
    ]
    assert store.reviews.for_job(job).state.value == "waiting"
    assert github.review_requests == []
    assert len(github.comments) == 1
    assert "已按 review 更新" not in github.comments[0]


async def test_failed_revision_keeps_diagnostics_local(revision_control):
    # A failed execution must preserve feedback and binding without notifying PR participants.
    store, github, job, subject, task = revision_control
    result = verified_result(task, exit_code=65, output=None, failure_stage="workspace",
                             failure_detail="unregistered workspace unexpectedly exists",
                             invocations=[])
    assert await apply_result(subject, job, result) is JobState.FAILED
    assert "unregistered workspace unexpectedly exists" in store.jobs.get_job(job.id).last_error
    assert store.results.list_effects(job.id) == []
    assert github.comments == github.review_requests == []
    assert [(f.item_id, f.version) for f in store.reviews.pending(job.flow_id)] == [
        (f.item_id, f.version) for f in task.feedback
    ]
    assert store.workers.get_flow_execution(job.flow_id).released_at is None


@pytest.mark.parametrize("problem", ["incomplete", "missing_root"])
async def test_thread_mapping_errors_stay_local_without_consuming_feedback(revision_control, problem):
    # Internal snapshot errors leave feedback pending; they are not PR conversation content.
    store, github, job, subject, task = revision_control
    batch = store.reviews.for_job(job)
    threads = batch.threads
    if problem == "incomplete":
        threads = threads.model_copy(update={"limitations": ("page missing",)})
    else:
        thread = threads.threads[0]
        comments = thread.comments.model_copy(update={
            "items": tuple(c.model_copy(update={"reply_to_id": 999}) for c in thread.comments.items),
        })
        threads = threads.model_copy(update={
            "threads": (thread.model_copy(update={"comments": comments}),),
        })
    with store.connection() as connection:
        connection.execute("UPDATE review_batches SET threads=%s WHERE id=%s",
                           (Jsonb(threads.model_dump(mode="json")), batch.id))
    item = task.feedback[0]
    result = verified_result(task, output=revision_output(addressed=[AddressedComment(
        item_id=item.item_id, version=item.version, comment_id=item.github_id, note="Fixed",
    )]))
    assert await apply_result(subject, job, result) is JobState.DONE
    assert [(f.item_id, f.version) for f in store.reviews.pending(job.flow_id)] == [
        (f.item_id, f.version) for f in task.feedback
    ]
    assert store.reviews.for_job(job).open_points
    assert store.reviews.for_job(job).state.value == "waiting"
    assert github.comments == ["修订交付（无新提交）：Already satisfied"]
    assert github.thread_replies == github.review_requests == []


@pytest.mark.parametrize("change", ["closed", "base", "head", "renamed-repository"])
async def test_control_checks_actual_pr_and_lineage(revision_control, monkeypatch, change):
    store, github, job, subject, task = revision_control
    original = PullRequest.refetch

    async def changed_pr(pr):
        pr = await original(pr)
        return pr.model_copy(update={
            "state": "closed" if change == "closed" else "open",
            "base_branch": "other" if change == "base" else task.base_branch,
            "head_sha": "new" if change == "head" else task.expected_head_sha,
        })

    if change == "renamed-repository":
        data = pr_data(task.expected_head_sha)
        data["head"]["ref"], data["base"]["ref"] = task.branch, task.base_branch
        data["head"]["repo"]["full_name"] = data["base"]["repo"]["full_name"] = "owner/renamed"
        endpoint = AsyncEndpoint(SimpleNamespace(raw_response=httpx.Response(200, json=data)))
        install_client(monkeypatch, lambda *args, **kwargs: SimpleNamespace(
            rest=SimpleNamespace(pulls=SimpleNamespace(async_get=endpoint)),
        ))
        monkeypatch.setattr(PullRequest, "refetch", NATIVE_PULL_REQUEST_REFETCH)
    else:
        monkeypatch.setattr(PullRequest, "refetch", changed_pr)
    result = verified_result(task)
    assert (await apply_result(subject, job, result)).value == "failed"
    assert store.deliveries.get_delivery(job.flow_id).head_sha == task.expected_head_sha
    assert [(f.item_id, f.version) for f in store.reviews.pending(job.flow_id)] == [
        (f.item_id, f.version) for f in task.feedback
    ]
    assert not github.review_requests
    assert github.comments == []
    if change == "renamed-repository":
        assert endpoint.calls == [(("owner", "repo", task.pr_number), {})]


@pytest.mark.parametrize("pending", ["deferred", "clarification", "none"])
async def test_verified_partial_delivery_waits_without_rereview(revision_control, monkeypatch, pending):
    store, github, job, subject, task = revision_control
    original = PullRequest.refetch

    async def current_pr(pr):
        return (await original(pr)).model_copy(update={"head_sha": "new"})

    async def ref(repo, branch, *, retry_transient=False):
        return RemoteRef(branch=task.branch, sha="new")

    async def compare(repo, base, head):
        return True

    monkeypatch.setattr(PullRequest, "refetch", current_pr)
    monkeypatch.setattr(Repository, "fetch_ref", ref)
    monkeypatch.setattr(Repository, "compare_commits", compare)
    item = task.feedback[0]
    identity = {"item_id": item.item_id, "version": item.version, "comment_id": item.github_id}
    output = revision_output(
        addressed=[AddressedComment(**identity, note="Fixed")] if pending != "deferred" else [],
        deferred=[DeferredComment(**identity, reason="Need a decision")] if pending == "deferred" else [],
        needs_clarification=["What timeout?"] if pending == "clarification" else [],
    )
    result = verified_result(task, commit_sha="new", remote_sha="new", output=output)
    await apply_result(subject, job, result)
    await subject.results.apply(job.id)
    assert store.deliveries.get_delivery(job.flow_id).head_sha == "new"
    assert bool(github.review_requests) is (pending == "none")
    assert store.reviews.for_job(job).state.value == ("completed" if pending == "none" else "waiting")
    assert [(f.item_id, f.version) for f in store.reviews.pending(job.flow_id)] == (
        [(item.item_id, item.version)] if pending == "deferred" else []
    )
    assert len(github.review_requests) <= 1


@pytest.mark.parametrize("kind", ["unknown", "conflict", "wrong_github_id"])
async def test_revision_rejects_fabricated_or_conflicting_feedback(revision_control, kind):
    store, github, job, subject, task = revision_control
    item = task.feedback[0]
    addressed = AddressedComment(item_id=item.item_id, version=item.version,
                                  comment_id=item.github_id, note="Fixed")
    if kind == "unknown":
        addressed = addressed.model_copy(update={"item_id": 999})
    if kind == "wrong_github_id":
        addressed = addressed.model_copy(update={"comment_id": 999})
    output = revision_output(
        addressed=[addressed],
        deferred=[DeferredComment(item_id=item.item_id, version=item.version,
                                  comment_id=item.github_id, reason="Later")] if kind == "conflict" else [],
    )
    assert (await apply_result(subject, job, verified_result(task, output=output))).value == "failed"
    assert [(f.item_id, f.version) for f in store.reviews.pending(job.flow_id)] == [
        (f.item_id, f.version) for f in task.feedback
    ]
    assert not github.review_requests
    assert github.comments == []


@pytest.mark.parametrize("behavior", ["noop", "push", "unpushed", "dirty", "wrong_branch"])
async def test_revision_checks_real_jj_repository(revision_workspace, behavior):
    if shutil.which("jj") is None:
        pytest.skip("jj is not installed")
    _, repo, task, _, _ = revision_workspace
    env = os.environ | {"JJ_USER": "FlowTool Test", "JJ_EMAIL": "flowtool@example.test"}
    await _jj("git", "init", "--colocate", cwd=repo, env=env)
    await _jj("bookmark", "track", task.branch, "--remote=origin", cwd=repo, env=env)
    context = await prepare_revision_context(repo, task.branch, task.expected_head_sha)
    if behavior == "wrong_branch":
        await _jj("new", "root()", cwd=repo, env=env)
    elif behavior != "noop":
        (repo / "README.md").write_text("revised by jj\n")
        if behavior != "dirty":
            await _jj("commit", "-m", "revision", cwd=repo, env=env)
            await _jj("bookmark", "set", task.branch, "-r", "@-", cwd=repo, env=env)
            if behavior == "push":
                await _jj("git", "push", "-b", task.branch, cwd=repo, env=env)
    if behavior in {"noop", "push"}:
        outcome = await verify_revision_outcome(context, VcsKind.JJ)
        assert outcome.commit_sha == outcome.remote_sha
        assert (outcome.commit_sha == task.expected_head_sha) is (behavior == "noop")
    else:
        with pytest.raises(VcsVerificationError):
            await verify_revision_outcome(context, VcsKind.JJ)


@pytest.mark.parametrize("changed_commit", [False, True])
async def test_answer_checks_real_jj_working_commit(revision_workspace, changed_commit):
    if shutil.which("jj") is None:
        pytest.skip("jj is not installed")
    root, repo, task, secret, pool = revision_workspace
    env = os.environ | {"JJ_USER": "FlowTool Test", "JJ_EMAIL": "flowtool@example.test"}
    await _jj("git", "init", "--colocate", cwd=repo, env=env)
    # jj's working commit differs from the PR head even before a read-only task runs.
    task = task.model_copy(update={"purpose": "answer"})

    class AnswerAgent(RevisionAgent):
        async def resume(self, session_id, prompt, *, cwd, output_schema=None, context=None):
            if changed_commit:
                await _jj("new", "-m", "Unexpected new commit", cwd=repo, env=env)
            return CodingExecutionResult(session_id, revision_output(vcs_backend_used=VcsKind.JJ).model_dump_json(), 0)

    result = await WorkerExecutor(root, AnswerAgent(repo), state=ExecutionState(root, 1),
                                  worker_id="w", workspaces=pool).execute(
        task, secret, tool_caller=RecordingToolCaller())
    assert (result.exit_code == 0) is not changed_commit
    if not changed_commit:
        assert result.commit_sha != task.expected_head_sha


@pytest.mark.parametrize("mutation", ["dirty", "head", "branch", "remote"])
async def test_changed_repository_after_session_rejection_does_not_fallback(
    revision_workspace, revision_backend, mutation,
):
    root, repo, task, secret, pool = revision_workspace
    env = os.environ | {"JJ_USER": "FlowTool Test", "JJ_EMAIL": "flowtool@example.test"}

    class ChangedRejection(RevisionAgent):
        async def resume(self, *args, **kwargs):
            self.calls.append(("resume", kwargs["context"], args[1]))
            if mutation == "dirty":
                (repo / "uncertain-work").write_text("preserve")
            elif mutation == "remote":
                _git("remote", "set-url", "origin", str(repo / "other.git"), cwd=repo)
            elif revision_backend is VcsKind.JJ:
                if mutation == "head":
                    await _jj("new", task.expected_head_sha, cwd=repo, env=env)
                else:
                    await _jj("bookmark", "set", task.branch, "-r", "@",
                              cwd=repo, env=env)
            elif mutation == "head":
                # Even a published commit made during a rejected resume cannot be erased/retried.
                _git("commit", "--allow-empty", "-m", "Uncertain execution", cwd=repo)
                _git("push", "origin", task.branch, cwd=repo)
            else:
                _git("checkout", "-b", "uncertain-branch", cwd=repo)
            self.after = await local_commit(repo)
            raise SessionUnavailable("rejected")

    agent = ChangedRejection(repo)
    result = await WorkerExecutor(root, agent, state=ExecutionState(root, 1), worker_id="w",
                                   workspaces=pool).execute(task, secret, tool_caller=RecordingToolCaller())
    assert result.exit_code != 0
    assert [call[0] for call in agent.calls] == ["resume"]
    assert await local_commit(repo) == agent.after
    assert result.baseline_sha == task.expected_head_sha
    assert [invocation.failure for invocation in result.invocations] == ["session_unavailable"]
    assert not ExecutionState(root, 1).load().fallback_consumed
    if mutation == "dirty":
        assert (repo / "uncertain-work").read_text() == "preserve"


async def test_session_fallback_keeps_fetched_snapshot_when_remote_advances(
    revision_workspace, revision_collaborator, revision_backend,
):
    # A rejected session does not grant another fetch or discard the synchronized run baseline.
    root, repo, task, secret, pool = revision_workspace
    baseline = publish_external(revision_collaborator, task, rewrite=True)
    task = task.model_copy(update={"expected_head_sha": baseline})

    class RemoteAdvance(RevisionAgent):
        async def resume(self, session_id, prompt, *, cwd, output_schema=None, context=None):
            self.calls.append(("resume", context, prompt))
            (revision_collaborator / "external.txt").write_text("concurrent update\n")
            _git("commit", "-am", "Update during rejected resume", cwd=revision_collaborator)
            _git("push", "origin", task.branch, cwd=revision_collaborator)
            raise SessionUnavailable("rejected")

        async def start(self, prompt, cwd, *, output_schema=None, context=None):
            self.calls.append(("start", context, prompt))
            assert (cwd / "external.txt").read_text() == "external contents\n"
            assert f"Fetched coding baseline: {baseline}" in prompt
            return CodingExecutionResult(
                "new-session", revision_output(vcs_backend_used=revision_backend).model_dump_json(), 0,
            )

    agent = RemoteAdvance(repo)
    result = await WorkerExecutor(root, agent, state=ExecutionState(root, 1), worker_id="w",
                                  workspaces=pool).execute(task, secret, tool_caller=RecordingToolCaller())
    assert [call[0] for call in agent.calls] == ["resume", "start"]
    assert [invocation.mode for invocation in result.invocations] == ["resume", "fallback"]
    assert result.session_generation == 1 and result.baseline_sha == baseline
    assert result.exit_code != 0 and result.failure_stage == "push"
    assert (repo / "external.txt").read_text() == "external contents\n"


async def test_jj_sync_preserves_other_unpublished_changes(revision_workspace, revision_collaborator):
    if shutil.which("jj") is None:
        pytest.skip("jj is not installed")
    _, repo, task, _, _ = revision_workspace
    env = os.environ | {"JJ_USER": "FlowTool Test", "JJ_EMAIL": "flowtool@example.test"}
    await _jj("git", "init", "--colocate", cwd=repo, env=env)
    await _jj("bookmark", "track", task.branch, "--remote=origin", cwd=repo, env=env)
    (repo / "other-work").write_text("unpublished side change")
    await _jj("describe", "-m", "Keep this other change", cwd=repo, env=env)
    other = await local_commit(repo)
    await _jj("new", task.branch, cwd=repo, env=env)
    remote = publish_external(revision_collaborator, task, rewrite=True)
    context = await prepare_revision_context(repo, task.branch, task.previous_head_sha)
    assert (await verify_revision_outcome(context, VcsKind.JJ)).commit_sha == remote
    visible = (await asyncio.to_thread(
        subprocess.check_output,
        ["jj", "--ignore-working-copy", "log", "-r", f"{other} & ancestors(visible_heads())",
         "--no-graph", "-T", "commit_id"], cwd=repo, env=env, text=True,
    )).strip()
    assert visible == other
    assert await asyncio.to_thread(
        subprocess.check_output, ["jj", "file", "show", "-r", other, "other-work"],
        cwd=repo, env=env, text=True,
    ) == "unpublished side change"


@pytest.mark.parametrize("revision_feedback", [
    [FeedbackUpdate(
        kind=ReviewItemKind.INLINE, github_id=500, author="reviewer", body="Please fix it.",
        updated_at=datetime.now(UTC), signal="request_changes", submitted=True,
    ), FeedbackUpdate(
        kind=ReviewItemKind.REVIEW_BODY, github_id=501, author="reviewer", body="",
        updated_at=datetime.now(UTC), signal=signal, submitted=True,
    )] for signal in ("request_changes", "discussion")
])
async def test_empty_review_body_is_not_an_unresolved_obligation(revision_control, revision_feedback):
    store, _github, job, subject, task = revision_control
    item = next(f for f in task.feedback if f.kind is ReviewItemKind.INLINE)
    assert len(task.feedback) == len(revision_feedback) == 2
    result = verified_result(
        task,
        output=RevisionCodingOutput(summary="Fixed", vcs_backend_used=VcsKind.GIT,
                          deferred=[], needs_clarification=[], addressed=[AddressedComment(
            item_id=item.item_id, version=item.version, comment_id=item.github_id, note="Fixed",
        )]),
    )
    await apply_result(subject, job, result)
    assert store.reviews.for_job(job).state.value == "completed"
    assert store.reviews.pending(job.flow_id) == []
    with store.connection() as connection:
        consumed = connection.execute(
            "SELECT item_id,version FROM review_versions WHERE consumed_at IS NOT NULL"
        ).fetchall()
    assert {(r["item_id"], r["version"]) for r in consumed} == {
        (f.item_id, f.version) for f in task.feedback
    }
