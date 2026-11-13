
from app.persistence.jobs import JobStore
from tests.test_review_batches import settle

"""PostgreSQL crash and replay boundaries for verified revisions."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from psycopg.types.json import Jsonb

from app.contracts.protocol import (
    RevisionExecutionRecord,
    RevisionTask,
    Succeeded,
    terminal_digest,
)
from app.contracts.receipts import decode_result_inbox
from app.contracts.result_models import EffectState, ResultPending
from app.controler.review.context import build_review_context
from app.controler.workflow.lifecycle import prove_reopen
from app.controler.workflow.service import ControlService
from app.domain.models import (
    AddressedComment,
    ArchiveReason,
    DeliveryState,
    FlowState,
    JobKind,
    JobState,
    ReviewItemKind,
    TriageVerdict,
    Verdict,
)
from app.domain.review_models import FeedbackUpdate
from app.github import GitHubError, GitHubErrorKind, Issue, PullRequest, RemoteRef, Repository
from tests.github import RecordingGitHub, record_call
from tests.review_memory import judge_decision
from tests.test_coordinator import coordinator
from tests.test_merge import route_merge
from tests.test_revision_delivery import revision_output, verified_result
from tests.workspace_fixtures import register_worker, report_settled


class RevisionGitHub(RecordingGitHub):
    def __init__(self):
        super().__init__()
        self.sha = "baseline"
        self.base_branch = "main"
        self.reads = 0

@pytest.fixture
def revision_resources(recording_resources, monkeypatch):
    async def refetch(pr):
        state = record_call(pr.repository, "fetch_pull_request", number=pr.number)
        state.reads += 1
        return PullRequest(pr.repository, pr.number, merged=False, state="open",
                           head_branch="flow/1", head_sha=state.sha,
                           head_repository=pr.repository.full_name, base_branch=state.base_branch,
                           base_sha="base", title="Task", body="Requirements")

    async def fetch_ref(repo, branch, *, retry_transient=False):
        state = record_call(repo, "fetch_ref", branch=branch)
        return RemoteRef(branch=branch, sha=state.sha)

    async def compare_commits(repo, base, head):
        record_call(repo, "compare_commits", base=base, head=head)
        return (base, head) == ("baseline", "revision")

    monkeypatch.setattr(PullRequest, "refetch", refetch)
    monkeypatch.setattr(Repository, "fetch_ref", fetch_ref)
    monkeypatch.setattr(Repository, "compare_commits", compare_commits)


@pytest.fixture
async def pending_revision(store, revision_resources, request):
    flow = store.flows.create_flow("owner/repo", 8, 42)
    attempt = store.flows.create_attempt(flow.id)
    attempt.codex_session_id = "old-session"
    store.flows.update_attempt(attempt)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    register_worker(store, "worker-a", repo=flow.repo, epoch="epoch")
    initial = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("worker-a", "initial", "epoch") is not None
    assert store.workers.finish_assignment(initial.id, "worker-a", "initial", JobState.DONE, "epoch")
    report_settled(store, initial.id, epoch="epoch")
    delivery = store.deliveries.get_or_create_delivery(flow.id, "flow/1")
    delivery.pr_number, delivery.state = 17, DeliveryState.OPEN
    delivery.head_sha, delivery.base_branch = "baseline", "main"
    store.deliveries.update_delivery(delivery)
    update = FeedbackUpdate(kind=ReviewItemKind.REVIEW_BODY, github_id=101, author="alice",
                            body="Fix the bound", updated_at=datetime.now(UTC),
                            signal="request_changes", submitted=True)
    store.reviews.ingest(flow.id, "review", "pull_request_review", {}, update,
                         settle_seconds=0, submit_grace_seconds=0)
    queued = settle(store)[0]
    assert queued.kind is JobKind.REVISION
    job = store.workers.assign_job("worker-a", "token", "epoch")
    github = RevisionGitHub()
    updates = getattr(request, "param", {})
    github.sha = updates.get("head", github.sha)
    github.base_branch = updates.get("base", github.base_branch)
    if "purpose" in updates:
        job.payload = {**job.payload, "purpose": updates["purpose"]}
        with store.connection() as connection:
            connection.execute("UPDATE jobs SET payload=%s WHERE id=%s", (Jsonb(job.payload), job.id))
    subject = coordinator(store, github)
    task = await subject.prepare(job)
    assert isinstance(task, RevisionTask)
    return flow, job, task, github, subject


def accept(store, job, result):
    terminal = Succeeded(job_id=job.id, assignment_token="token", result=result)
    return store.workers.record_assignment_result(job.id, "worker-a", "token", "epoch", terminal)


@pytest.mark.parametrize("pending_revision", [{"head": "external", "base": "release"}], indirect=True)
@pytest.mark.parametrize("new_commit", [False, True])
async def test_external_updates_use_fetched_baseline_without_claiming_external_work(
    store, pending_revision, monkeypatch, new_commit,
):
    # The collaborator rewrites history before preparation, then pushes again before fetch.
    flow, job, task, github, subject = pending_revision
    assert task.previous_head_sha == "baseline"
    assert task.expected_head_sha == "external" and task.base_branch == "release"
    assert store.deliveries.get_delivery(flow.id).head_sha == "baseline"
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")

    async def compare(repo, base, head):
        return (base, head) == ("fetched", "revision")

    monkeypatch.setattr(Repository, "compare_commits", compare)
    github.sha = "revision" if new_commit else "fetched"
    item, = task.feedback
    result = verified_result(task, baseline_sha="fetched", commit_sha=github.sha,
        remote_sha=github.sha, output=revision_output(addressed=[AddressedComment(
            item_id=item.item_id, version=item.version, comment_id=item.github_id,
            note="Fixed" if new_commit else "Already fixed externally",
        )]))
    assert accept(store, job, result) == "pending"
    assert await subject.results.apply(job.id) is JobState.DONE
    assert await subject.results.apply(job.id) is JobState.DONE
    delivery = store.deliveries.get_delivery(flow.id)
    assert delivery.head_sha == github.sha and delivery.base_branch == "release"
    assert not store.reviews.pending(flow.id)
    assert bool(github.review_requests) is new_commit
    assert any(("已核验新提交" if new_commit else "无新提交") in body for body in github.comments)


@pytest.mark.parametrize("pending_revision", [
    {"head": "external", "base": "release", "purpose": "answer"},
], indirect=True)
async def test_answer_uses_fresh_pr_without_adopting_external_delivery(store, pending_revision):
    flow, job, task, github, subject = pending_revision
    before = store.deliveries.get_delivery(flow.id)
    assert task.purpose == "answer" and task.expected_head_sha == "external"
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    item, = task.feedback
    result = verified_result(task, commit_sha="local-working-copy", remote_sha=None, branch=None,
        clean_worktree=False, output=revision_output(addressed=[AddressedComment(
            item_id=item.item_id, version=item.version, comment_id=item.github_id,
            note="The implementation already has the requested bound.",
        )]))
    assert accept(store, job, result) == "pending"
    assert await subject.results.apply(job.id) is JobState.DONE
    assert store.deliveries.get_delivery(flow.id) == before
    assert not store.reviews.pending(flow.id) and not github.review_requests
    assert any(body.startswith("问题答复：") for body in github.comments)


async def test_changed_pr_rebuilds_judge_context_before_unstarted_revision(store, pending_revision):
    # A PR push after triage must not silently attach a saved instruction to different evidence.
    flow, job, task, github, subject = pending_revision
    batch = store.reviews.for_job(job)
    repo = github.installation(flow.installation_id).repository(flow.repo)
    context = await build_review_context(repo, flow, batch, task.pr_number)
    verdict = TriageVerdict(verdict=Verdict.REVISE, confidence=1, instruction="Fix it")
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.freeze_context(batch, context)
        tx.reviews.record_decision(batch, judge_decision(verdict, context))
    github.sha = "external"
    assert await subject.prepare_for_assignment(job, "worker-a", "token", "epoch") is JobState.DONE
    fresh = store.reviews.get_batch(batch.id)
    assert fresh.context is fresh.decision is fresh.inference_job_id is fresh.job_id is None
    assert fresh.feedback == batch.feedback and store.reviews.pending(flow.id)
    assert store.flows.get_attempt(task.attempt_id).revision_cycles == 0
    assert store.workers.finish_assignment(job.id, "worker-a", "token", JobState.DONE, "epoch")
    report_settled(store, job.id, epoch="epoch")
    judge = SimpleNamespace(triage=AsyncMock(side_effect=lambda new: judge_decision(verdict, new)))
    await subject.triage(store.reviews.pending_triage()[0], judge)
    replacement = store.workers.assign_job("worker-a", "replacement", "epoch")
    prepared = await subject.prepare(replacement)
    assert isinstance(prepared, RevisionTask) and prepared.expected_head_sha == "external"
    assert prepared.context.digest != context.digest
    assert store.reviews.get_batch(batch.id).decision.context_digest == prepared.context.digest


@pytest.mark.parametrize("baseline", [None, "unrelated"])
async def test_current_revision_requires_actual_baseline_evidence(store, pending_revision, baseline):
    flow, job, task, github, subject = pending_revision
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    github.sha = "revision"
    result = verified_result(task, baseline_sha=baseline, commit_sha=github.sha, remote_sha=github.sha)
    assert accept(store, job, result) == "pending"
    assert await subject.results.apply(job.id) is JobState.FAILED
    assert store.deliveries.get_delivery(flow.id).head_sha == "baseline"
    assert store.reviews.pending(flow.id)
    assert not github.comments and not github.review_requests


def test_started_and_revision_budget_commit_atomically(store, pending_revision):
    _, job, task, _, _ = pending_revision
    # A control crash between accepting Started and marking feedback relayed still consumes one run.
    with pytest.raises(RuntimeError), store.transaction() as tx:
        assert tx.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
        raise RuntimeError("crash before commit")
    assert store.flows.get_attempt(task.attempt_id).revision_cycles == 0
    assert store.jobs.get_job(job.id).started_at is None
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    assert not store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    assert store.flows.get_attempt(task.attempt_id).revision_cycles == 1


async def test_result_commit_failure_replays_facts_without_new_execution(store, pending_revision, monkeypatch):
    flow, job, task, github, subject = pending_revision
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    item = task.feedback[0]
    output = revision_output(addressed=[AddressedComment(item_id=item.item_id, version=item.version,
                                                         comment_id=item.github_id, note="Fixed")])
    result = verified_result(task, mode="fallback", session_id="new-session", session_generation=1,
                              commit_sha="revision", remote_sha="revision", output=output,
                              invocations=[
                                  RevisionExecutionRecord(mode="resume", session_id="old-session", exit_code=1,
                                                     failure="session_unavailable"),
                                  RevisionExecutionRecord(mode="fallback", session_id="new-session", exit_code=0),
                              ])
    github.sha = "revision"
    # Feedback arriving while the worker runs must not be consumed by this frozen result.
    store.reviews.ingest(flow.id, "new-review", "pull_request_review_comment", {},
                         FeedbackUpdate(kind=ReviewItemKind.INLINE, github_id=102, author="alice",
                                        body="Also fix another bound", updated_at=datetime.now(UTC)),
                         settle_seconds=0, submit_grace_seconds=0)
    assert accept(store, job, result) == "pending"
    accepted = store.jobs.get_job(job.id)
    original = JobStore.complete_persisted_result

    def crash(self, job_id, state):
        original(self, job_id, state)
        raise RuntimeError("crash after final SQL")

    with monkeypatch.context() as patch:
        patch.setattr(JobStore, "complete_persisted_result", crash)
        with pytest.raises(RuntimeError, match="final SQL"):
            await subject.results.apply(job.id)
    assert store.deliveries.get_delivery(flow.id).head_sha == "baseline"
    assert store.flows.get_attempt(task.attempt_id).codex_session_id == "old-session"
    assert len(store.reviews.pending(flow.id)) == 2
    assert store.jobs.get_job(job.id).result_applied_at is None
    reads, comments = github.reads, list(github.comments)
    assert accept(store, job, result) == "pending"
    assert await subject.results.apply(job.id) is JobState.DONE
    assert accept(store, job, result) == "applied"
    assert await subject.results.apply(job.id) is JobState.DONE
    assert github.reads == reads and github.comments == comments
    assert store.deliveries.get_delivery(flow.id).head_sha == "revision"
    attempt = store.flows.get_attempt(task.attempt_id)
    assert (attempt.codex_session_id, attempt.session_generation, attempt.revision_cycles) == (
        "new-session", 1, 1,
    )
    assert [item.github_id for item in store.reviews.pending(flow.id)] == [102]
    persisted = store.jobs.get_job(job.id)
    assert persisted.result_payload == accepted.result_payload
    assert persisted.result_digest == accepted.result_digest
    assert persisted.result_received_at == accepted.result_received_at
    assert persisted.result_applied_at >= persisted.result_received_at
    terminal = decode_result_inbox(
        persisted.result_payload,
        job_id=job.id,
        assignment_token=persisted.assignment_token,
        result_digest=persisted.result_digest,
    ).terminal
    assert isinstance(terminal, Succeeded)
    assert terminal.result.model_dump(mode="json") == result.model_dump(mode="json")
    assert terminal_digest(terminal) == persisted.result_digest


async def test_cancel_during_remote_verification_prevents_effects_and_commit(
    store, pending_revision, monkeypatch,
):
    flow, job, task, github, subject = pending_revision
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    assert accept(store, job, verified_result(task)) == "pending"
    original = Repository.fetch_ref

    async def cancel(repo, branch, *, retry_transient=False):
        ControlService(
            database=store.database,
        ).cancel(flow)
        return await original(repo, branch, retry_transient=retry_transient)

    monkeypatch.setattr(Repository, "fetch_ref", cancel)
    await subject.results.apply(job.id)
    assert store.flows.get_flow(flow.id).state is FlowState.FINALIZING
    assert github.comments == [] and github.review_requests == []
    assert store.deliveries.get_delivery(flow.id).head_sha == "baseline"


async def test_revision_limit_applies_to_strong_signals(store, pending_revision):
    _, job, task, _, subject = pending_revision
    attempt = store.flows.get_attempt(task.attempt_id)
    attempt.revision_cycles = 20
    store.flows.update_attempt(attempt)
    assert await subject.prepare_for_assignment(job, "worker-a", "token", "epoch") is JobState.FAILED
    assert store.flows.get_attempt(task.attempt_id).revision_cycles == 20
    assert store.reviews.pending(job.flow_id)


async def test_failed_revision_retry_retains_repo_and_consumes_a_new_cycle(store, pending_revision):
    flow, job, task, _, subject = pending_revision
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    failed = verified_result(task, exit_code=65, failure_stage="push", failure_detail="not pushed")
    assert accept(store, job, failed) == "pending"
    assert await subject.results.apply(job.id) is JobState.FAILED
    report_settled(store, job.id, epoch="epoch")
    binding = store.workers.get_flow_execution(flow.id)
    assert binding.released_at is None
    ControlService(
        database=store.database,
    ).retry(store.flows.get_flow(flow.id))
    replacement = store.workers.assign_job("worker-a", "new-token", "epoch")
    assert replacement.id != job.id
    prepared = await subject.prepare(replacement)
    assert prepared.repo_identity == task.repo_identity
    assert prepared.expected_head_sha == task.expected_head_sha
    assert store.workers.start_assignment(replacement.id, task.attempt_id, "worker-a", "new-token", "epoch")
    assert store.flows.get_attempt(task.attempt_id).revision_cycles == 2
    assert store.workers.get_flow_execution(flow.id).worker_id == binding.worker_id


async def test_failed_revision_cannot_retry_coding_after_merge(store, pending_revision):
    flow, job, task, _, subject = pending_revision
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    failed = verified_result(task, exit_code=65, failure_stage="push", failure_detail="not pushed")
    assert accept(store, job, failed) == "pending"
    assert await subject.results.apply(job.id) is JobState.FAILED
    report_settled(store, job.id, epoch="epoch")
    await route_merge(store, flow)
    jobs = store.jobs.list_jobs(flow.id)
    binding = store.workers.get_flow_execution(flow.id)
    delivery = store.deliveries.get_delivery(flow.id)
    assert delivery.state is DeliveryState.MERGED

    archived = store.flows.get_flow(flow.id)
    assert (archived.state, archived.archive_reason) == (FlowState.ARCHIVED, ArchiveReason.SUCCEEDED)
    notice = jobs[-1]
    assert notice.kind is JobKind.ARCHIVE and notice.state is JobState.RUNNING
    assert notice.assigned_worker_id is None and notice.assignment_token is None
    assert notice.result_payload["terminal"]["type"] == "merge_archive"
    stop = store.stops.pending("worker-a")
    assert stop.job_id == job.id and binding.released_at is None
    with pytest.raises(ValueError, match="archived Flow"):
        ControlService(database=store.database).retry(flow)
    assert store.flows.get_flow(flow.id) == archived
    assert store.flows.get_active_flow(flow.repo, flow.issue_number) is None
    assert store.jobs.list_jobs(flow.id) == jobs
    assert store.workers.get_flow_execution(flow.id) == binding
    assert store.stops.pending("worker-a") == stop
    assert store.workers.assign_job("worker-a", "after-merge", "epoch") is None
    assert store.deliveries.get_delivery(flow.id) == delivery


@pytest.mark.parametrize("transition", ["retry", "cancel", "merge"])
async def test_rejected_revision_notification_keeps_verified_result_recoverable(
    store, pending_revision, monkeypatch, transition,
):
    flow, job, task, github, subject = pending_revision
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    item = task.feedback[0]
    output = revision_output(addressed=[AddressedComment(item_id=item.item_id, version=item.version,
                                                         comment_id=item.github_id, note="Fixed")])
    result = verified_result(
        task, commit_sha="revision", remote_sha="revision", output=output,
        mode="fallback", session_id="new-session", session_generation=1,
        invocations=[
            RevisionExecutionRecord(mode="resume", session_id="old-session", exit_code=1,
                               failure="session_unavailable"),
            RevisionExecutionRecord(mode="fallback", session_id="new-session", exit_code=0),
        ],
    )
    github.sha = "revision"
    assert accept(store, job, result) == "pending"

    async def rejected(pr, reviewers):
        raise GitHubError(GitHubErrorKind.VALIDATION, "request_reviewers", status_code=422,
                          request_rejected=True)

    with monkeypatch.context() as patch:
        patch.setattr(PullRequest, "request_reviewers", rejected)
        with pytest.raises(ResultPending, match="422"):
            await subject.results.apply(job.id)
    assert store.jobs.get_job(job.id).result_applied_at is None
    assert "422" in store.jobs.get_job(job.id).last_error
    assert store.flows.get_flow(flow.id).state is FlowState.BLOCKED
    assert store.results.get_plan(job.id).revision.model_dump() == result.model_dump()
    assert store.results.list_effects(job.id)[-1].state is EffectState.FAILED
    comments, reads = list(github.comments), github.reads
    # Recovery must wait for an explicit retry, including after control restarts.
    restarted = coordinator(store, github)
    with pytest.raises(ResultPending):
        await restarted.results.apply(job.id)
    assert github.review_requests == []
    service = ControlService(
        database=store.database,
    )
    if transition == "merge":
        await route_merge(store, flow)
        assert await restarted.results.apply(job.id) is JobState.DONE
        report_settled(store, job.id, epoch="epoch")
        assert github.comments == comments and github.review_requests == []
        assert store.deliveries.get_delivery(flow.id).state is DeliveryState.MERGED
        assert store.deliveries.get_delivery(flow.id).head_sha == "baseline"
        archived = store.flows.get_flow(flow.id)
        assert (archived.state, archived.archive_reason) == (
            FlowState.ARCHIVED, ArchiveReason.SUCCEEDED,
        )
        jobs = store.jobs.list_jobs(flow.id)
        with pytest.raises(ValueError, match="archived Flow"):
            service.retry(flow)
        assert store.flows.get_flow(flow.id) == archived
        assert store.jobs.list_jobs(flow.id) == jobs
        assert store.workers.get_flow_execution(flow.id).released_at is None
        assert store.stops.pending("worker-a").job_id == job.id
        assert github.comments == comments and github.review_requests == []
        assert store.flows.get_attempt(task.attempt_id).codex_session_id == "old-session"
        return
    if transition == "cancel":
        service.cancel(flow)
        with pytest.raises(ValueError, match="pending result"):
            service.retry(flow)
        await restarted.results.apply(job.id)
        assert github.review_requests == []
        assert store.deliveries.get_delivery(flow.id).head_sha == "baseline"
        return
    service.retry(flow)
    assert await restarted.results.apply(job.id) is JobState.DONE
    assert accept(store, job, result) == "applied"
    assert github.comments == comments
    # A resumed reviewer request checks the current head before publishing.
    assert github.reads == reads + 1
    assert github.review_requests == [["carol"]]
    assert store.deliveries.get_delivery(flow.id).head_sha == "revision"
    assert store.flows.get_attempt(task.attempt_id).revision_cycles == 1
    assert store.flows.get_attempt(task.attempt_id).codex_session_id == "new-session"
    assert store.flows.get_attempt(task.attempt_id).session_generation == 1
    assert store.reviews.pending(flow.id) == []
    assert store.workers.assign_job("worker-a", "next-token", "epoch") is None


@pytest.mark.parametrize("merge_at", ["verification", "commit"])
async def test_merge_during_revision_application_preserves_delivery_and_session(
    store, pending_revision, monkeypatch, merge_at,
):
    # Remote verification and effect I/O can each overlap a human merging the current PR.
    flow, job, task, github, subject = pending_revision
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    result = verified_result(task, commit_sha="revision", remote_sha="revision")
    github.sha = "revision"
    assert accept(store, job, result) == "pending"
    accepted = store.jobs.get_job(job.id)
    owner = PullRequest if merge_at == "verification" else subject.results.effects
    method = "refetch" if merge_at == "verification" else "run"
    original = getattr(owner, method)

    async def merge_after_read(*args, **kwargs):
        value = await original(*args, **kwargs)
        await route_merge(store, flow)
        return value

    monkeypatch.setattr(owner, method, merge_after_read)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert await subject.results.apply(job.id) is JobState.DONE
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.MERGED
    assert store.deliveries.get_delivery(flow.id).head_sha == "baseline"
    archived = store.flows.get_flow(flow.id)
    assert (archived.state, archived.archive_reason) == (FlowState.ARCHIVED, ArchiveReason.SUCCEEDED)
    assert store.flows.get_attempt(task.attempt_id).codex_session_id == "old-session"
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert len(store.reviews.pending(flow.id)) == 1
    persisted = store.jobs.get_job(job.id)
    assert persisted.result_payload == accepted.result_payload
    assert persisted.result_digest == accepted.result_digest
    assert persisted.result_received_at == accepted.result_received_at
    assert persisted.result_applied_at is not None


async def test_revision_preparation_failure_is_persisted_and_explicitly_retryable(
    store, pending_revision, monkeypatch,
):
    flow, job, task, _github, subject = pending_revision

    async def unavailable(resource, *args, **kwargs):
        raise GitHubError(GitHubErrorKind.NOT_FOUND, "fetch_ref", status_code=404)

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "fetch_ref", unavailable)
        assert await subject.prepare_for_assignment(job, "worker-a", "token", "epoch") is JobState.FAILED
    failed = store.jobs.get_job(job.id)
    assert failed.state is JobState.FAILED and failed.result_applied_at is not None
    assert "fetch_ref" in failed.last_error
    assert store.flows.get_flow(flow.id).state is FlowState.BLOCKED
    assert store.flows.get_attempt(task.attempt_id).revision_cycles == 0
    ControlService(
        database=store.database,
    ).retry(flow)
    replacement = store.workers.assign_job("worker-a", "retry-token", "epoch")
    assert isinstance(await subject.prepare(replacement), RevisionTask)
    assert store.flows.get_attempt(task.attempt_id).revision_cycles == 0


async def test_uncertain_revision_notification_cannot_be_explicitly_resent(
    store, pending_revision, monkeypatch,
):
    flow, job, task, _github, subject = pending_revision
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    assert accept(store, job, verified_result(task)) == "pending"

    async def unknown(*args, **kwargs):
        raise GitHubError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(Issue, "comment", unknown)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    assert store.results.list_effects(job.id)[0].state is EffectState.UNCERTAIN
    with pytest.raises(ValueError, match="pending result"):
        ControlService(
            database=store.database,
        ).retry(flow)
    assert store.jobs.get_job(job.id).result_applied_at is None


@pytest.mark.parametrize("version", [1, 14, 15])
@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("external_update", [False, True])
async def test_durable_revision_recovery_and_reopen_keep_original_inbox(
    store, pending_revision, version, retired, external_update,
):
    # Both ordinary recovery and a closed PR's late result must read old stored wire receipts,
    # while retaining the exact payload/digest that existing plans and effects reference.
    flow, job, task, github, subject = pending_revision
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    # A real historical assignment/result has neither new field; its original digest must survive.
    task = task.model_copy(update={"previous_head_sha": None})
    payload = {**job.payload, "revision": task.model_dump(mode="json")}
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET payload=%s WHERE id=%s", (Jsonb(payload), job.id))
    assert "previous_head_sha" not in payload["revision"]
    github.sha = "revision" if retired else "baseline"
    result = verified_result(task, baseline_sha=None, commit_sha=github.sha, remote_sha=github.sha)
    assert "baseline_sha" not in result.model_dump(mode="json")
    terminal = Succeeded(job_id=job.id, assignment_token="token", result=result)
    assert (
        store.workers.record_assignment_result(job.id, "worker-a", "token", "epoch", terminal)
        == "pending"
    )
    if version != 1:
        payload = {
            "protocol_version": version,
            "connection_epoch": "epoch",
            **terminal.model_dump(mode="json"),
        }
        with store.connection() as connection:
            connection.execute(
                "UPDATE jobs SET result_payload=%s WHERE id=%s", (Jsonb(payload), job.id)
            )
    if retired:
        delivery = store.deliveries.get_delivery(flow.id)
        delivery.state = DeliveryState.CLOSED
        store.deliveries.update_delivery(delivery)
    accepted = store.jobs.get_job(job.id)
    await subject.results.recover_pending_results()
    persisted = store.jobs.get_job(job.id)
    assert persisted.state is JobState.DONE and persisted.result_applied_at is not None
    if external_update:
        github.sha, github.base_branch = "external-rewrite", "release"
    repo = github.installation(flow.installation_id).repository(flow.repo)
    remote = await repo.fetch_pull_request(task.pr_number)
    proof, error = await prove_reopen(
        repo,
        flow,
        store.deliveries.get_delivery(flow.id),
        remote,
        persisted,
        deliveries=store.deliveries,
        flows=store.flows,
    )
    assert error is None and proof is not None and proof.head_sha == result.commit_sha
    assert persisted.result_payload == accepted.result_payload
    assert persisted.result_digest == accepted.result_digest == terminal_digest(terminal)
    assert store.results.get_plan(job.id).digest == accepted.result_digest


@pytest.mark.parametrize("reader", ["revision", "prepare", "retired", "reopen"])
@pytest.mark.parametrize("corruption", ["missing_digest", "job", "token", "version"])
async def test_durable_result_readers_reject_corrupt_evidence_without_rewriting(
    store,
    pending_revision,
    reader,
    corruption,
):
    # Never use a result under a different row's authority or repair a missing digest on read.
    flow, job, task, github, subject = pending_revision
    assert accept(store, job, verified_result(task)) == "pending"
    persisted = store.jobs.get_job(job.id)
    payload = persisted.result_payload
    digest = persisted.result_digest
    if corruption == "missing_digest":
        digest = None
    elif corruption == "version":
        payload["version"] = 2
    else:
        payload["terminal"]["job_id" if corruption == "job" else "assignment_token"] = (
            job.id + 1 if corruption == "job" else "wrong"
        )
    with store.connection() as connection:
        connection.execute(
            "UPDATE jobs SET result_payload=%s,result_digest=%s WHERE id=%s",
            (Jsonb(payload), digest, job.id),
        )
    persisted = store.jobs.get_job(job.id)
    with pytest.raises(ValueError):
        if reader == "revision":
            await subject.results._check_revision(persisted)
        elif reader == "prepare":
            with store.transaction() as tx:
                locked = tx.lock_workflow(flow.id)
                subject.results._prepare(tx.database, locked, persisted)
        elif reader == "retired":
            subject.results._retired_plan(persisted, task.attempt_id)
        else:
            repo = github.installation(flow.installation_id).repository(flow.repo)
            remote = await repo.fetch_pull_request(task.pr_number)
            await prove_reopen(
                repo,
                flow,
                store.deliveries.get_delivery(flow.id),
                remote,
                persisted.model_copy(update={"result_applied_at": store.now()}),
                deliveries=store.deliveries,
                flows=store.flows,
            )
    after = store.jobs.get_job(job.id)
    assert after.result_payload == payload and after.result_digest == digest
    assert after.result_applied_at is None and store.results.get_plan(job.id) is None
