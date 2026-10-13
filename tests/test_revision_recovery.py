
from app.persistence.jobs import JobStore
from tests.test_review_batches import settle

"""PostgreSQL crash and replay boundaries for verified revisions."""

from datetime import UTC, datetime

import pytest

from app.contracts.protocol import (
    PROTOCOL_VERSION,
    RevisionExecutionRecord,
    RevisionTask,
    Succeeded,
    terminal_digest,
)
from app.contracts.result_models import EffectState, ResultPending
from app.controler.workflow.service import ControlService
from app.domain.models import (
    AddressedComment,
    DeliveryState,
    FlowState,
    JobKind,
    JobState,
    ReviewItemKind,
)
from app.domain.review_models import FeedbackUpdate
from app.github import GitHubError, GitHubErrorKind, Issue, PullRequest, RemoteRef, Repository
from tests.github import RecordingGitHub, record_call
from tests.test_coordinator import coordinator
from tests.test_merge import route_merge
from tests.test_revision_delivery import revision_output, verified_result


class RevisionGitHub(RecordingGitHub):
    def __init__(self):
        super().__init__()
        self.sha = "baseline"
        self.reads = 0

@pytest.fixture
def revision_resources(recording_resources, monkeypatch):
    async def refetch(pr):
        state = record_call(pr.repository, "fetch_pull_request", number=pr.number)
        state.reads += 1
        return PullRequest(pr.repository, pr.number, merged=False, state="open",
                           head_branch="flow/1", head_sha=state.sha,
                           head_repository=pr.repository.full_name, base_branch="main",
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
async def pending_revision(store, revision_resources):
    flow = store.flows.create_flow("owner/repo", 8, 42)
    attempt = store.flows.create_attempt(flow.id)
    attempt.codex_session_id = "old-session"
    store.flows.update_attempt(attempt)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
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
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "epoch")
    job = store.workers.assign_job("worker-a", "token", "epoch")
    github = RevisionGitHub()
    subject = coordinator(store, github)
    task = await subject.prepare(job)
    assert isinstance(task, RevisionTask)
    return flow, job, task, github, subject


def accept(store, job, result):
    terminal = Succeeded(job_id=job.id, assignment_token="token", connection_epoch="epoch", result=result)
    return store.workers.record_assignment_result(job.id, "worker-a", "token", "epoch",
                                           terminal.model_dump(mode="json"), terminal_digest(terminal))


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
    terminal = Succeeded.model_validate(persisted.result_payload)
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
            deliveries=store.deliveries,
            flows=store.flows,
            jobs=store.jobs,
            result_store=store.results,
            reviews=store.reviews,
            stops=store.stops,
            webhook_store=store.webhooks,
            workers=store.workers,
        ).cancel(flow)
        return await original(repo, branch, retry_transient=retry_transient)

    monkeypatch.setattr(Repository, "fetch_ref", cancel)
    await subject.results.apply(job.id)
    assert store.flows.get_flow(flow.id).state is FlowState.CANCELLED
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
    binding = store.workers.get_flow_execution(flow.id)
    assert binding.released_at is None
    ControlService(
        database=store.database,
        deliveries=store.deliveries,
        flows=store.flows,
        jobs=store.jobs,
        result_store=store.results,
        reviews=store.reviews,
        stops=store.stops,
        webhook_store=store.webhooks,
        workers=store.workers,
    ).retry(store.flows.get_flow(flow.id))
    replacement = store.workers.assign_job("worker-a", "new-token", "epoch")
    assert replacement.id != job.id
    prepared = await subject.prepare(replacement)
    assert prepared.repo_identity == task.repo_identity
    assert prepared.expected_head_sha == task.expected_head_sha
    assert store.workers.start_assignment(replacement.id, task.attempt_id, "worker-a", "new-token", "epoch")
    assert store.flows.get_attempt(task.attempt_id).revision_cycles == 2
    assert store.workers.get_flow_execution(flow.id).worker_id == binding.worker_id


async def test_failed_revision_retry_after_merge_keeps_flow_terminal(store, pending_revision):
    flow, job, task, _, subject = pending_revision
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    failed = verified_result(task, exit_code=65, failure_stage="push", failure_detail="not pushed")
    assert accept(store, job, failed) == "pending"
    assert await subject.results.apply(job.id) is JobState.FAILED
    await route_merge(store, flow)
    jobs = store.jobs.list_jobs(flow.id)
    binding = store.workers.get_flow_execution(flow.id)
    delivery = store.deliveries.get_delivery(flow.id)
    assert delivery.state is DeliveryState.MERGED

    with pytest.raises(ValueError, match="merged PR"):
        ControlService(
            database=store.database,
            deliveries=store.deliveries,
            flows=store.flows,
            jobs=store.jobs,
            result_store=store.results,
            reviews=store.reviews,
            stops=store.stops,
            webhook_store=store.webhooks,
            workers=store.workers,
        ).retry(flow)

    assert store.flows.get_flow(flow.id).state is FlowState.FAILED
    assert store.flows.get_active_flow(flow.repo, flow.issue_number) is None
    assert store.jobs.list_jobs(flow.id) == jobs
    assert store.workers.get_flow_execution(flow.id) == binding
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
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
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
        deliveries=store.deliveries,
        flows=store.flows,
        jobs=store.jobs,
        result_store=store.results,
        reviews=store.reviews,
        stops=store.stops,
        webhook_store=store.webhooks,
        workers=store.workers,
    )
    if transition == "merge":
        await route_merge(store, flow)
        assert await restarted.results.apply(job.id) is JobState.DONE
        assert github.comments == comments and github.review_requests == []
        assert store.deliveries.get_delivery(flow.id).state is DeliveryState.MERGED
        assert store.deliveries.get_delivery(flow.id).head_sha == "baseline"
        assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
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
    assert store.flows.get_flow(flow.id).state is FlowState.FAILED
    assert store.flows.get_attempt(task.attempt_id).revision_cycles == 0
    ControlService(
        database=store.database,
        deliveries=store.deliveries,
        flows=store.flows,
        jobs=store.jobs,
        result_store=store.results,
        reviews=store.reviews,
        stops=store.stops,
        webhook_store=store.webhooks,
        workers=store.workers,
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
            deliveries=store.deliveries,
            flows=store.flows,
            jobs=store.jobs,
            result_store=store.results,
            reviews=store.reviews,
            stops=store.stops,
            webhook_store=store.webhooks,
            workers=store.workers,
        ).retry(flow)
    assert store.jobs.get_job(job.id).result_applied_at is None
