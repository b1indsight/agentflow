"""Revision delivery must be proven even when the model exits successfully."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.contracts.protocol import AssignmentSecret, PushSecret, RevisionTask
from app.domain.models import (
    CodingExecutionContext,
    CodingExecutionResult,
    RevisionCodingOutput,
    VcsKind,
)
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.vcs import flow_repo_path, prepare_flow_repo
from app.worker.state.session import WorkerSession
from tests.test_vcs import _git, _git_fixture
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
    repo = await prepare_flow_repo(seed, 1)
    target = flow_repo_path(root / "repos/owner/repo", 1)
    target.parent.mkdir(parents=True)
    repo.rename(target)
    _git("checkout", "-b", "flow/1", cwd=target)
    _git("push", "origin", "flow/1", cwd=target)
    import subprocess

    sha = (await asyncio.to_thread(
        subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=target, text=True,
    )).strip()
    task = RevisionTask(
        flow_id=1, attempt_id=1, batch_id=1, repo="owner/repo", repo_identity="w:owner/repo",
        branch="flow/1", base_branch="main", pr_number=17, expected_head_sha=sha,
        session_id="session-1", session_generation=0, instruction="Fix the feedback", feedback=[],
        issue_title="Original task", issue_body="Original requirements", previous_summary="",
        open_points=[],
    )
    secret = AssignmentSecret(push=PushSecret(
        repository="owner/repo", token=SecretStr("fresh-token"),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    ))
    from app.contracts.protocol import Assignment
    journal = WorkerSession(root)
    journal.record_received(Assignment(job_id=1, flow_id=1, assignment_token="token",
                                      connection_epoch="epoch", task=task, secret=secret))
    journal.record_started()
    return root, target, task, secret


class RevisionAgent:
    def __init__(self, repo: Path, behavior="noop"):
        self.repo, self.behavior = repo, behavior
        self.calls: list[tuple[str, CodingExecutionContext | None, str]] = []

    async def resume(self, session_id, prompt, *, cwd, output_schema=None, context=None):
        assert cwd == self.repo
        self.calls.append(("resume", context, prompt))
        if self.behavior == "missing":
            from app.domain.models import SessionUnavailable

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


@pytest.mark.parametrize("behavior,success", [
    ("noop", True), ("push", True), ("unpushed", False), ("dirty", False), ("empty", False),
])
async def test_revision_verifies_real_git_delivery(revision_workspace, behavior, success):
    # A successful process can leave unpushed or dirty work; neither closes review feedback.
    root, repo, task, secret = revision_workspace
    agent = RevisionAgent(repo, behavior)
    result = await WorkerExecutor(root, agent, session=WorkerSession(root), worker_id="w").execute(task, secret, tool_caller=RecordingToolCaller())
    assert (result.exit_code == 0) is success
    assert agent.calls[0][1].push_token.get_secret_value() == "fresh-token"
    assert "flow.open_pr" not in agent.calls[0][2]
    if success:
        assert result.clean_worktree and result.commit_sha == result.remote_sha
        assert (result.commit_sha == task.expected_head_sha) is (behavior == "noop")


@pytest.mark.parametrize("session,behavior,expected", [
    (None, "noop", ["start"]), ("session-1", "missing", ["resume", "start"]),
    ("session-1", "timeout", ["resume"]),
])
async def test_fallback_only_when_execution_is_known_not_to_have_started(
    revision_workspace, session, behavior, expected,
):
    root, repo, task, secret = revision_workspace
    task = task.model_copy(update={"session_id": session})
    agent = RevisionAgent(repo, behavior)
    result = await WorkerExecutor(root, agent, session=WorkerSession(root), worker_id="w").execute(task, secret, tool_caller=RecordingToolCaller())
    assert [call[0] for call in agent.calls] == expected
    assert all(call[1].mcp_url is None and call[1].mcp_token is None for call in agent.calls)
    if expected[-1] == "start":
        assert result.session_id == "new-session"
        assert result.session_generation == 1
        assert result.mode == "fallback"


@pytest.mark.parametrize("problem", ["secret", "binding", "baseline", "dirty"])
async def test_revision_preflight_rejects_before_model_execution(revision_workspace, problem):
    root, repo, task, secret = revision_workspace
    if problem == "secret":
        secret = None
    elif problem == "binding":
        task = task.model_copy(update={"repo_identity": "other:owner/repo"})
    elif problem == "baseline":
        task = task.model_copy(update={"expected_head_sha": "0" * 40})
    else:
        (repo / "uncommitted").write_text("keep me")
    agent = RevisionAgent(repo)
    result = await WorkerExecutor(root, agent, session=WorkerSession(root), worker_id="w").execute(task, secret, tool_caller=RecordingToolCaller())
    assert result.exit_code != 0
    assert agent.calls == []


def test_revision_output_requires_every_field_and_rejects_extra_data():
    with pytest.raises(ValidationError):
        RevisionCodingOutput.model_validate({"summary": "done"})
    with pytest.raises(ValidationError):
        RevisionCodingOutput.model_validate({**revision_output().model_dump(), "pushed": True})


@pytest.fixture
async def revision_control():
    from app.domain.models import Attempt, DeliveryState, FlowDelivery, Job, JobKind, JobState
    from tests.test_coordinator import MemoryStore, RecordingGitHub, coordinator, prepare

    store, github = MemoryStore(), RecordingGitHub()
    store.attempt = Attempt(id=1, flow_id=1, codex_session_id="session-1")
    store.flow.current_attempt_id = 1
    store.delivery = FlowDelivery(flow_id=1, branch="flow/1", base_branch="trunk",
                                  state=DeliveryState.OPEN, pr_number=17, head_sha="abc123")
    job = Job(id=21, flow_id=1, kind=JobKind.REVISION, state=JobState.RUNNING)
    from app.domain.github_models import ReviewComment, ReviewThread
    item = store.reviews.batch.feedback[0]
    github.threads = [ReviewThread(thread_id="T1", repo="owner/repo", pr_number=17,
        path="app.py", resolved=False, outdated=False, comments=[ReviewComment(
            github_id=item.github_id, node_id="C500", author=item.author, is_bot=False,
            body=item.body, updated_at=item.updated_at)])]
    subject = coordinator(store, github)
    from tests.review_memory import review_context

    async def current_pr(*args):
        return review_context().pull_request.model_copy(update={
            "head_branch": "flow/1", "head_sha": "abc123", "base_branch": "trunk",
        })

    github.get_pull_request = current_pr
    task = await prepare(subject, job)
    return store, github, job, subject, task


def verified_result(task, **updates):
    from app.contracts.protocol import RevisionExecutionRecord, RevisionTaskResult

    values = {
        "flow_id": task.flow_id, "attempt_id": task.attempt_id, "batch_id": task.batch_id,
        "feedback_refs": list(task.feedback), "previous_session_id": task.session_id,
        "session_id": task.session_id, "session_generation": task.session_generation,
        "mode": "resume",
        "invocations": [RevisionExecutionRecord(mode="resume", session_id=task.session_id, exit_code=0)],
        "last_message": "done", "exit_code": 0, "output": revision_output(),
        "vcs_backend": VcsKind.GIT, "branch": task.branch, "commit_sha": task.expected_head_sha,
        "remote_sha": task.expected_head_sha, "clean_worktree": True,
    }
    values.update(updates)
    return RevisionTaskResult(**values)


async def test_resolve_payloads_only_carry_their_threads_current_and_historical_feedback(revision_control):
    from datetime import UTC, datetime

    from app.contracts.result_models import ResolveEffect
    from app.control.resolution import plan_resolution
    from app.domain.github_models import ReviewThreads
    from app.domain.models import AddressedComment, ReviewItemKind
    from tests.test_review_resolution import feedback, human, snapshot

    store, _, _, _, task = revision_control
    threads, current, historical = [], [], []
    # Many long review threads must not duplicate the entire PR's history in every effect.
    for number in range(20):
        root = human(number * 2 + 1, body="Earlier request " + "x" * 1000)
        reply = human(number * 2 + 2, reply_to_id=root.github_id, body="New request " + "y" * 1000)
        threads.append(snapshot(root, reply).model_copy(update={"thread_id": f"T{number}"}))
        historical.extend([feedback(root), feedback(root, kind=ReviewItemKind.REVIEW_BODY)])
        current.append(feedback(reply))
    remote = ReviewThreads(repo=store.flow.repo, pr_number=17, fetched_at=datetime.now(UTC), threads=threads)
    batch = store.reviews.batch.model_copy(update={"feedback": current, "threads": remote})
    result = verified_result(task.model_copy(update={"feedback": current}), output=revision_output(
        addressed=[AddressedComment(item_id=f.item_id, version=f.version, comment_id=f.github_id,
                                    note="Fixed") for f in current]))
    plan = plan_resolution(store.flow, 17, batch, result, historical)
    resolves = [effect for effect in plan.effects if isinstance(effect, ResolveEffect)]
    assert len(resolves) == len(threads) and all(effect.allow_resolve for effect in resolves)
    assert all({f.github_id for f in effect.covered} == {c.github_id for c in effect.snapshot.comments}
               and all(f.kind is ReviewItemKind.INLINE for f in effect.covered) for effect in resolves)
    assert sum(len(effect.model_dump_json()) for effect in resolves) < 3 * len(remote.model_dump_json())


async def test_control_preserves_omitted_feedback_and_does_not_rereview_noop(revision_control):
    from tests.test_coordinator import apply_result

    store, github, job, subject, task = revision_control
    result = verified_result(task)
    await apply_result(subject, job, result)
    await subject.results.apply(job.id)
    assert store.reviews.consumed == []
    assert store.reviews.batch.state.value == "waiting"
    assert github.review_requests == []
    assert len(github.comments) == 1
    assert "已按 review 更新" not in github.comments[0]


@pytest.mark.parametrize("changes", [
    {"clean_worktree": False}, {"branch": "another"}, {"remote_sha": "drift"},
    {"flow_id": 2}, {"attempt_id": 2}, {"session_generation": 3}, {"output": None},
])
async def test_control_refuses_unproven_revision_results(revision_control, changes):
    from tests.test_coordinator import apply_result

    store, github, job, subject, task = revision_control
    state = await apply_result(subject, job, verified_result(task, **changes))
    assert state.value == "failed"
    assert store.reviews.consumed == []
    assert store.delivery.head_sha == "abc123"
    assert github.review_requests == []


async def test_revision_relay_is_idempotent(revision_control):
    store, _, job, subject, task = revision_control
    subject.mark_started(job, task, "worker-1")
    subject.mark_started(job, task, "worker-1")
    assert store.reviews.batch.state.value == "processing"


@pytest.mark.parametrize("change", ["closed", "base", "head", "lineage", "fork"])
async def test_control_checks_actual_pr_and_lineage(revision_control, monkeypatch, change):
    from tests.test_coordinator import apply_result

    store, github, job, subject, task = revision_control
    original = github.get_pull_request

    async def changed_pr(*args):
        pr = await original(*args)
        return pr.model_copy(update={
            "state": "closed" if change == "closed" else "open",
            "base_branch": "other" if change == "base" else task.base_branch,
            "head_sha": "new" if change in {"head", "lineage"} else task.expected_head_sha,
            "head_repository": "fork/repo" if change == "fork" else task.repo,
        })

    async def ref(*args):
        from app.domain.github_models import RemoteRef

        return RemoteRef(branch=task.branch, sha="new")

    async def compare(*args):
        return False

    monkeypatch.setattr(github, "get_pull_request", changed_pr)
    if change == "lineage":
        monkeypatch.setattr(github, "get_ref", ref)
        monkeypatch.setattr(github, "compare_commits", compare, raising=False)
    result = verified_result(task, **(
        {"commit_sha": "new", "remote_sha": "new"} if change == "lineage" else {}
    ))
    assert (await apply_result(subject, job, result)).value == "failed"
    assert store.delivery.head_sha == task.expected_head_sha
    assert not store.reviews.consumed and not github.review_requests


@pytest.mark.parametrize("pending", ["deferred", "clarification", "none"])
async def test_verified_partial_delivery_waits_without_rereview(revision_control, monkeypatch, pending):
    from app.domain.github_models import RemoteRef
    from app.domain.models import AddressedComment, DeferredComment
    from tests.test_coordinator import apply_result

    store, github, job, subject, task = revision_control
    original = github.get_pull_request

    async def current_pr(*args):
        return (await original(*args)).model_copy(update={"head_sha": "new"})

    async def ref(*args):
        return RemoteRef(branch=task.branch, sha="new")

    async def compare(*args):
        return True

    monkeypatch.setattr(github, "get_pull_request", current_pr)
    monkeypatch.setattr(github, "get_ref", ref)
    monkeypatch.setattr(github, "compare_commits", compare, raising=False)
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
    assert store.delivery.head_sha == "new"
    assert bool(github.review_requests) is (pending == "none")
    assert store.reviews.batch.state.value == ("completed" if pending == "none" else "waiting")
    assert len(github.review_requests) <= 1


@pytest.mark.parametrize("kind", ["unknown", "duplicate", "conflict", "wrong_github_id"])
async def test_revision_rejects_fabricated_or_conflicting_feedback(revision_control, kind):
    from app.domain.models import AddressedComment, DeferredComment
    from tests.test_coordinator import apply_result

    store, github, job, subject, task = revision_control
    item = task.feedback[0]
    addressed = AddressedComment(item_id=item.item_id, version=item.version,
                                  comment_id=item.github_id, note="Fixed")
    if kind == "unknown":
        addressed = addressed.model_copy(update={"item_id": 999})
    if kind == "wrong_github_id":
        addressed = addressed.model_copy(update={"comment_id": 999})
    output = revision_output(
        addressed=[addressed, addressed] if kind == "duplicate" else [addressed],
        deferred=[DeferredComment(item_id=item.item_id, version=item.version,
                                  comment_id=item.github_id, reason="Later")] if kind == "conflict" else [],
    )
    assert (await apply_result(subject, job, verified_result(task, output=output))).value == "failed"
    assert not store.reviews.consumed and not github.review_requests


@pytest.mark.parametrize("behavior", ["noop", "push", "unpushed", "dirty", "wrong_branch"])
async def test_revision_checks_real_jj_repository(revision_workspace, behavior):
    import os
    import shutil

    from app.worker.execution.vcs import (
        VcsVerificationError,
        prepare_revision_context,
        verify_revision_outcome,
    )
    from tests.test_vcs import _jj

    if shutil.which("jj") is None:
        pytest.skip("jj is not installed")
    _, repo, task, _ = revision_workspace
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


@pytest.mark.parametrize("phase", ["resume", "fallback"])
async def test_journal_prevents_restarting_an_interrupted_invocation(revision_workspace, phase):
    from app.contracts.protocol import Assignment, RevisionExecutionRecord
    from app.worker.state.session import SessionError, WorkerSession

    root, repo, task, secret = revision_workspace
    root.mkdir(exist_ok=True)
    journal = WorkerSession(root)
    journal.record_received(Assignment(job_id=1, flow_id=1, assignment_token="token",
                                        connection_epoch="epoch", task=task, secret=secret))
    journal.record_started()
    journal.begin_revision("resume")
    if phase == "fallback":
        journal.finish_invocation(RevisionExecutionRecord(mode="resume", session_id=task.session_id,
                                                      exit_code=1, failure="session_unavailable"))
        journal.begin_revision("fallback")
    agent = RevisionAgent(repo, "missing")
    restarted = WorkerSession(root)
    with pytest.raises(SessionError, match="restarted|already been attempted"):
        await WorkerExecutor(root, agent, worker_id="w", session=restarted).execute(task, secret, tool_caller=RecordingToolCaller())
    assert agent.calls == []
    assert "fresh-token" not in journal.path.read_text()
    assert restarted.load().revision_phase == phase


async def test_dirty_repository_after_session_rejection_does_not_fallback(revision_workspace):
    from app.domain.models import SessionUnavailable

    root, repo, task, secret = revision_workspace

    class DirtyRejection(RevisionAgent):
        async def resume(self, *args, **kwargs):
            (self.repo / "uncertain-work").write_text("preserve")
            raise SessionUnavailable("rejected")

    agent = DirtyRejection(repo)
    result = await WorkerExecutor(root, agent, session=WorkerSession(root), worker_id="w").execute(task, secret, tool_caller=RecordingToolCaller())
    assert result.exit_code != 0
    assert agent.calls == []
    assert (repo / "uncertain-work").read_text() == "preserve"


@pytest.mark.parametrize("signal_kind", ["request_changes", "discussion"])
async def test_empty_review_body_is_not_an_unresolved_obligation(revision_control, signal_kind):
    from app.domain.models import AddressedComment, ReviewItemKind
    from tests.test_coordinator import apply_result

    store, _github, job, subject, task = revision_control
    item = store.reviews.batch.feedback[0]
    signal = item.model_copy(update={"item_id": 102, "github_id": 501, "body": "",
                                     "signal": signal_kind, "kind": ReviewItemKind.REVIEW_BODY})
    store.reviews.batch.feedback.append(signal)
    task = task.model_copy(update={"feedback": [item, signal]})
    store.jobs[job.id].payload["revision"] = task.model_dump(mode="json")
    result = verified_result(
        task, feedback_refs=[item, signal],
        output=RevisionCodingOutput(summary="Fixed", vcs_backend_used=VcsKind.GIT,
                          deferred=[], needs_clarification=[], addressed=[AddressedComment(
            item_id=item.item_id, version=item.version, comment_id=item.github_id, note="Fixed",
        )]),
    )
    await apply_result(subject, job, result)
    assert store.reviews.batch.state.value == "completed"
    assert {f.item_id for f in store.reviews.consumed} == {101, 102}
