"""Failure injection at the remote-write and final-commit boundaries without network."""

import asyncio
import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.contracts.protocol import (
    FlowToolRequest,
    InitialTask,
    InitialTaskResult,
    Succeeded,
    call_header,
    terminal_digest,
)
from app.contracts.receipts import ResultInbox
from app.contracts.result_models import (
    CommentEffect,
    EffectState,
    LabelEffect,
    ResultPending,
    ResultPlan,
)
from app.controler.workflow.service import ControlService
from app.domain.delivery_models import OpenedDelivery
from app.domain.models import (
    ArchiveReason,
    DeliveryState,
    FlowState,
    JobKind,
    JobState,
)
from app.github import GitHubError, GitHubErrorKind, Issue, PullRequest, Repository
from app.persistence.jobs import JobStore
from tests.test_coordinator import accept_result, apply_result, coordinator
from tests.test_merge import route_merge
from tests.test_revision_delivery import revision_control as revision_control  # noqa: PLC0414
from tests.test_revision_delivery import revision_feedback as revision_feedback  # noqa: PLC0414
from tests.test_revision_delivery import verified_result
from tests.workspace_fixtures import register_worker, report_settled


@pytest.fixture
def result_clock(store, monkeypatch):
    clock = SimpleNamespace(now=store.database.now())
    monkeypatch.setattr(store.database, "_clock", lambda: clock.now)
    return clock


@pytest.fixture
def scenario(revision_control, result_clock):
    store, github, job, _subject, task = revision_control
    return store, github, job, verified_result(task)


@pytest.fixture
def initial_scenario(store, prepared_initial, result_clock):
    _flow, _subject, github, request, result = prepared_initial
    job = store.jobs.get_job(request.arguments.job_id)
    return store, github, job, result


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_merged_initial_retry_keeps_execution_facts_without_reopening(
    store,
    completed_open_initial,
    exit_code,
):
    # Worker loss can block a completed INITIAL in the same round while its PR stays open.
    flow, subject, github, initial_request, initial_result = completed_open_initial
    service = ControlService(
        database=store.database,
    )
    flow.state = FlowState.BLOCKED
    store.flows.update_flow(flow)
    assert store.workers.release_flow_execution(flow.id)
    service.retry(flow)
    # Fresh worker-b starts from seed A, then checks out existing H where H descends A and H != A.
    register_worker(store, "worker-b", repo=flow.repo, epoch="epoch-b")
    job = store.workers.assign_job("worker-b", "retry-token", "epoch-b")
    assert job is not None and job.kind is JobKind.INITIAL
    task = await subject.prepare(job)
    assert isinstance(task, InitialTask) and task.attempt_id == flow.current_attempt_id
    assert task.branch == initial_request.arguments.branch
    assert task.repo_identity == "worker-b:owner/repo"
    assert store.workers.start_assignment(
        job.id, task.attempt_id, "worker-b", "retry-token", "epoch-b"
    )
    retry = FlowToolRequest(
        **call_header(),
        method=initial_request.method,
        arguments=initial_request.arguments.model_copy(
            update={
                "job_id": job.id,
                "assignment_token": "retry-token",
                "call_id": "retry-open",
            }
        ),
    )
    tool = await subject.tools.execute(retry, worker_id="worker-b", connection_epoch="epoch-b")
    assert isinstance(tool.result, OpenedDelivery)
    call = store.deliveries.get_tool_call("retry-open")
    assert call is not None and call.job_id == job.id and call.response == tool.result
    await route_merge(store, flow)
    merged = store.deliveries.get_delivery(flow.id)
    assert merged is not None and merged.state is DeliveryState.MERGED
    archived = store.flows.get_flow(flow.id)
    assert (archived.state, archived.archive_reason) == (FlowState.ARCHIVED, ArchiveReason.SUCCEEDED)
    stop = store.stops.pending("worker-b")
    binding = store.workers.get_flow_execution(flow.id)
    if exit_code == 0:
        result = initial_result.model_copy(
            update={
                "session_id": "retry-session",
                "open_pr_call_id": "retry-open",
                "delivery_receipt": tool.result,
            }
        )
    else:
        result = InitialTaskResult(
            session_id="retry-session", last_message="failed", exit_code=exit_code
        )
    terminal = Succeeded(job_id=job.id, assignment_token="retry-token", result=result)
    digest = terminal_digest(terminal)
    assert (
        store.workers.record_assignment_result(
            job.id, "worker-b", "retry-token", "epoch-b", terminal
        )
        == "pending"
    )
    expected = JobState.DONE if exit_code == 0 else JobState.FAILED
    assert await subject.results.apply(job.id) is expected
    assert store.deliveries.get_delivery(flow.id) == merged
    assert store.flows.get_flow(flow.id) == archived
    assert store.stops.pending("worker-b") == stop and stop.job_id == job.id
    assert store.workers.get_flow_execution(flow.id) == binding
    report_settled(store, job.id, epoch="epoch-b")
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert store.workers.assign_job("worker-b", "late-coding", "epoch-b") is None
    assert store.flows.get_attempt(task.attempt_id).codex_session_id == "initial-session"
    accepted = store.jobs.get_job(job.id)
    assert accepted.result_digest == digest and accepted.result_applied_at is not None
    assert accepted.result_payload == ResultInbox(source="worker", terminal=terminal).model_dump(
        mode="json"
    )
    assert github.opened == [task.branch]


async def test_cancel_before_terminal_keeps_executor_slot_until_result_ack(scenario):
    # Cancel is not a remote kill: leave the running assignment receivable until its result arrives.
    store, github, job, result = scenario
    ControlService(database=store.database).cancel(store.flows.get_flow(job.flow_id))
    assert store.jobs.get_job(job.id).state is JobState.RUNNING
    assert store.stops.has_pending(job.flow_id)
    assert await apply_result(coordinator(store, github), job, result) is JobState.DONE
    assert store.flows.get_flow(job.flow_id).state is FlowState.FINALIZING
    assert store.jobs.get_job(job.id).result_applied_at is not None
    assert store.workers.get_flow_execution(job.flow_id).released_at is None
    assert store.stops.has_pending(job.flow_id)
    assert not github.comments
    assert [j.kind for j in store.jobs.list_jobs(job.flow_id)] == [JobKind.INITIAL, JobKind.INFERENCE, JobKind.REVISION]


async def test_review_label_preserves_user_labels_on_delivery_and_replay(initial_scenario):
    # Human labels may predate delivery; replaying a frozen status intent must not erase them.
    store, github, job, result = initial_scenario
    github.labels = [["bug", "user-owned"]]
    subject = coordinator(store, github)
    await apply_result(subject, job, result)
    await apply_result(subject, job, result)
    assert github.labels[-1] == ["bug", "user-owned", "flow:in-review"]
    assert len(github.labels) == 2
    assert [call for call in github.resource_calls if call["operation"] == "add_label"] == [
        {
            "operation": "add_label",
            "installation_id": 42,
            "repo": "owner/repo",
            "number": 17,
        }
    ]


async def test_initial_delivery_commits_only_after_all_required_effects(
    initial_scenario, result_clock, monkeypatch
) -> None:
    store, github, job, result = initial_scenario
    original = PullRequest.request_reviewers
    limited = [True]

    async def request_reviewers(pr, reviewers):
        if limited:
            limited.pop()
            raise GitHubError(
                GitHubErrorKind.RATE_LIMITED,
                "request_reviewers",
                status_code=429,
                request_rejected=True,
            )
        await original(pr, reviewers)

    monkeypatch.setattr(PullRequest, "request_reviewers", request_reviewers)
    with pytest.raises(ResultPending):
        await apply_result(coordinator(store, github), job, result)
    assert store.deliveries.get_delivery(job.flow_id).state is DeliveryState.OPENING
    assert store.jobs.get_job(job.id).result_applied_at is None
    accepted = store.jobs.get_job(job.id)
    result_clock.now += timedelta(seconds=10)
    await apply_result(coordinator(store, github), job, result)
    await apply_result(coordinator(store, github), job, result)
    assert store.deliveries.get_delivery(job.flow_id).state is DeliveryState.OPEN
    assert len(github.opened) == 1
    persisted = store.jobs.get_job(job.id)
    assert persisted.result_payload == accepted.result_payload
    assert persisted.result_digest == accepted.result_digest
    assert persisted.result_received_at == accepted.result_received_at
    assert store.jobs.get_job(job.id).result_applied_at is not None


@pytest.mark.parametrize(
    "operation,kind,status",
    [
        ("fetch_labels", GitHubErrorKind.PERMISSION, 403),
        ("add_label", GitHubErrorKind.NOT_FOUND, 404),
        ("add_label", GitHubErrorKind.RATE_LIMITED, 429),
        ("add_label", GitHubErrorKind.TRANSIENT, 503),
    ],
)
async def test_status_label_failure_does_not_block_initial_delivery(
    initial_scenario,
    monkeypatch,
    operation,
    kind,
    status,
):
    # The PR already exists when its display label fails; do not lose its association.
    store, github, job, result = initial_scenario
    calls = []

    async def unavailable(*args):
        calls.append(operation)
        raise GitHubError(kind, operation, status_code=status, request_rejected=status < 500)

    monkeypatch.setattr(Issue, operation, unavailable)
    assert await apply_result(coordinator(store, github), job, result) is JobState.DONE
    assert store.flows.get_flow(job.flow_id).state is FlowState.REVIEW
    delivery = store.deliveries.get_delivery(job.flow_id)
    assert delivery.state is DeliveryState.OPEN and delivery.pr_number == 17
    assert store.jobs.get_job(job.id).result_applied_at is not None
    record = next(
        r for r in store.results.list_effects(job.id) if isinstance(r.payload, LabelEffect)
    )
    assert record.state is EffectState.SKIPPED
    assert record.receipt is None
    assert record.last_error and str(status) in record.last_error
    assert (record.dispatched_at is not None) is (status == 503)
    assert github.review_requests == [["carol"]]
    assert await apply_result(coordinator(store, github), job, result) is JobState.DONE
    assert len(github.opened) == 1 and calls == [operation]
    assert [j.kind for j in store.jobs.list_jobs(job.flow_id)] == [JobKind.INITIAL, JobKind.INFERENCE]


async def test_closed_pr_after_lost_creation_response_finishes_without_new_writes(
    initial_scenario, result_clock, monkeypatch
):
    store, github, job, result = initial_scenario
    create = Repository.open_pull_request

    async def create_then_lose_response(repo, head, base, title, body):
        await create(repo, head, base, title, body)
        raise GitHubError(GitHubErrorKind.TRANSIENT, "open_pull_request")

    monkeypatch.setattr(Repository, "open_pull_request", create_then_lose_response)
    subject = coordinator(store, github)
    with pytest.raises(ResultPending):
        await apply_result(subject, job, result)
    assert store.jobs.get_job(job.id).result_applied_at is None
    assert github.pull_request is not None
    github.pull_request = github.pull_request.model_copy(update={"state": "closed"})
    result_clock.now += timedelta(seconds=10)
    assert await apply_result(subject, job, result) is JobState.DONE
    assert store.deliveries.get_delivery(job.flow_id).state is DeliveryState.CLOSED
    assert store.jobs.get_job(job.id).result_applied_at is not None
    assert store.flows.get_flow(job.flow_id).state is FlowState.FINALIZING
    assert store.stops.has_pending(job.flow_id)
    assert store.workers.get_flow_execution(job.flow_id).released_at is None
    assert len(github.opened) == 1
    assert github.labels == github.review_requests == []


@pytest.mark.parametrize("prior", ["pending", "rejected", "accepted", "unknown"])
async def test_legacy_failure_comment_is_never_sent_on_recovery(scenario, prior):
    # A frozen pre-upgrade warning must not be sent; an uncertain old POST remains auditable.
    store, github, job, result = scenario
    result = result.model_copy(update={"exit_code": 65, "output": None})
    accept_result(coordinator(store, github), job, result)
    accepted = store.jobs.get_job(job.id)
    reason = "unregistered workspace unexpectedly exists"
    plan = ResultPlan(job_id=job.id, flow_id=job.flow_id, digest=accepted.result_digest,
                      attempt_id=store.flows.get_flow(job.flow_id).current_attempt_id, state=JobState.FAILED,
                      flow_state=FlowState.BLOCKED, error=reason,
                      effects=[CommentEffect(key="failure", installation_id=42, repo="owner/repo",
                                             number=17, body=f"⚠️ {reason}")])
    store.results.prepare(plan)
    if prior != "pending":
        record = store.results.claim(job.id, "old-process")
        store.results.mark_dispatched(record)
        if prior == "accepted":
            digest = hashlib.sha256(f"{job.id}:{plan.digest}:failure".encode()).hexdigest()
            github.comment_bodies.append(f"⚠️ {reason}\n\n<!-- flowtool:effect:{digest} -->")
        store.results.finish(record, EffectState.RETRY if prior == "rejected" else EffectState.UNCERTAIN,
                             definitely_unsent=prior == "rejected")
    subject = coordinator(store, github)
    if prior == "unknown":
        with pytest.raises(ResultPending):
            await subject.results.apply(job.id)
        assert store.jobs.get_job(job.id).result_applied_at is None
        assert store.results.list_effects(job.id)[0].dispatched_at is not None
    else:
        assert await subject.results.apply(job.id) is JobState.FAILED
        assert store.jobs.get_job(job.id).last_error == reason
        assert store.flows.get_flow(job.flow_id).state is FlowState.BLOCKED
    assert github.comments == []


async def test_remote_comment_is_recovered_after_response_loss(scenario, result_clock, monkeypatch) -> None:
    # GitHub accepted the POST, but the control process never received its response.
    store, github, job, result = scenario
    original = Issue.comment

    async def lose_response(issue, body):
        await original(issue, body)
        raise GitHubError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(Issue, "comment", lose_response)
    with pytest.raises(ResultPending):
        await apply_result(coordinator(store, github), job, result)
    assert [j.kind for j in store.jobs.list_jobs(job.flow_id)] == [JobKind.INITIAL, JobKind.INFERENCE, JobKind.REVISION]
    comment = next(r for r in store.results.list_effects(job.id) if isinstance(r.payload, CommentEffect))
    assert comment.state is EffectState.UNCERTAIN
    result_clock.now += timedelta(seconds=10)
    state = await apply_result(coordinator(store, github), job, result)
    assert state is JobState.DONE
    assert len(github.comments) == 1
    assert [j.kind for j in store.jobs.list_jobs(job.flow_id)] == [JobKind.INITIAL, JobKind.INFERENCE, JobKind.REVISION]
    assert store.jobs.get_job(job.id).result_applied_at is not None
    assert [
        (call["operation"], call["installation_id"], call["repo"], call["number"])
        for call in github.resource_calls
        if call["operation"] in {"comment", "find_comment"}
    ] == [
        ("find_comment", 42, "owner/repo", 17),
        ("comment", 42, "owner/repo", 17),
        ("find_comment", 42, "owner/repo", 17),
    ]


async def test_merge_during_comment_lookup_prevents_dispatch(scenario, result_clock, monkeypatch):
    # The merge commits while an old result is awaiting a GitHub read, just before POST.
    store, github, job, result = scenario
    original = Issue.find_comment

    async def merge_during_lookup(issue, marker):
        await route_merge(store, store.flows.get_flow(job.flow_id))
        return await original(issue, marker)

    monkeypatch.setattr(Issue, "find_comment", merge_during_lookup)
    subject = coordinator(store, github)
    try:
        await apply_result(subject, job, result)
    except ResultPending:
        result_clock.now += timedelta(seconds=30)
        await apply_result(subject, job, result)
    assert store.jobs.get_job(job.id).result_applied_at is not None
    assert store.flows.get_flow(job.flow_id).state is FlowState.ARCHIVED
    assert store.flows.get_flow(job.flow_id).archive_reason is ArchiveReason.SUCCEEDED
    assert store.workers.get_flow_execution(job.flow_id).released_at is None
    assert github.comments == []
    assert [j.kind for j in store.jobs.list_jobs(job.flow_id)] == [
        JobKind.INITIAL, JobKind.INFERENCE, JobKind.REVISION, JobKind.ARCHIVE,
    ]


@pytest.mark.parametrize("remote_receipt", [True, False])
@pytest.mark.parametrize("transition", ["merge", "cancel"])
async def test_retired_result_reconciles_dispatched_comment_without_resending(
    scenario,
    result_clock,
    monkeypatch,
    remote_receipt,
    transition,
):
    # Merge must not discard an uncertain POST or let its recovered result enqueue REVISION.
    store, github, job, result = scenario
    original = Issue.comment
    calls = []

    async def lose_response(issue, body):
        calls.append((issue.number, body))
        if remote_receipt:
            await original(issue, body)
        raise GitHubError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(Issue, "comment", lose_response)
    subject = coordinator(store, github)
    with pytest.raises(ResultPending):
        await apply_result(subject, job, result)
    assert store.jobs.get_job(job.id).result_applied_at is None
    flow = store.flows.get_flow(job.flow_id)
    if transition == "merge":
        await route_merge(store, flow)
    else:
        ControlService(database=store.database).cancel(flow)
        assert store.stops.has_pending(job.flow_id)
    # Result recovery may finish, but the stop or merged cleanup remains outstanding.
    result_clock.now += timedelta(seconds=30)
    if remote_receipt:
        assert await apply_result(subject, job, result) is JobState.DONE
        comment = next(r for r in store.results.list_effects(job.id) if isinstance(r.payload, CommentEffect))
        assert comment.state is EffectState.SUCCEEDED
        assert store.jobs.get_job(job.id).result_applied_at is not None
    else:
        with pytest.raises(ResultPending):
            await apply_result(subject, job, result)
        assert store.jobs.get_job(job.id).result_applied_at is None
    assert len(calls) == 1
    assert [j.kind for j in store.jobs.list_jobs(job.flow_id)] == (
        [JobKind.INITIAL, JobKind.INFERENCE, JobKind.REVISION, JobKind.ARCHIVE] if transition == "merge"
        else [JobKind.INITIAL, JobKind.INFERENCE, JobKind.REVISION]
    )
    assert store.flows.get_flow(job.flow_id).state is (
        FlowState.ARCHIVED if transition == "merge" else FlowState.FINALIZING
    )
    assert store.workers.get_flow_execution(job.flow_id).released_at is None


async def test_unknown_comment_is_not_resubmitted_and_keeps_result_pending(scenario, result_clock, monkeypatch) -> None:
    store, github, job, result = scenario
    sent = []

    async def ambiguous_post(issue, body):
        sent.append((issue.number, body))
        raise GitHubError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(Issue, "comment", ambiguous_post)
    for _ in range(3):
        with pytest.raises(ResultPending):
            await apply_result(coordinator(store, github), job, result)
        result_clock.now += timedelta(seconds=30)
    assert len(sent) == 1
    assert store.jobs.get_job(job.id).result_applied_at is None
    assert store.jobs.has_pending_result(job.flow_id)


async def test_final_commit_failure_rolls_back_completion_but_keeps_effect_receipt(
    scenario, monkeypatch,
) -> None:
    # Even a failure after setting the result marker must roll back the whole database unit.
    store, github, job, result = scenario

    original = JobStore.complete_persisted_result
    before = store.reviews.for_job(job)

    def interrupted_finalization(repository, job_id, state):
        assert original(repository, job_id, state)
        assert repository.get_job(job_id).result_applied_at is not None
        raise RuntimeError("commit interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(JobStore, "complete_persisted_result", interrupted_finalization)
        with pytest.raises(RuntimeError, match="commit interrupted"):
            await apply_result(coordinator(store, github), job, result)
    assert [j.kind for j in store.jobs.list_jobs(job.flow_id)] == [JobKind.INITIAL, JobKind.INFERENCE, JobKind.REVISION]
    assert store.jobs.get_job(job.id).result_applied_at is None
    assert store.reviews.for_job(job) == before
    comment = next(r for r in store.results.list_effects(job.id) if isinstance(r.payload, CommentEffect))
    assert comment.state is EffectState.SUCCEEDED and comment.receipt is not None
    await apply_result(coordinator(store, github), job, result)
    assert store.jobs.get_job(job.id).result_applied_at is not None
    assert len(github.comments) == 1
    assert [j.kind for j in store.jobs.list_jobs(job.flow_id)] == [JobKind.INITIAL, JobKind.INFERENCE, JobKind.REVISION]


async def test_cancel_during_remote_lookup_does_not_authorize_a_comment(
    scenario, monkeypatch
) -> None:
    store, github, job, result = scenario

    async def cancel_during_lookup(issue, marker):
        ControlService(database=store.database).cancel(store.flows.get_flow(job.flow_id))

    monkeypatch.setattr(Issue, "find_comment", cancel_during_lookup)
    with pytest.raises(ResultPending):
        await apply_result(coordinator(store, github), job, result)
    assert store.stops.has_pending(job.flow_id)
    await apply_result(coordinator(store, github), job, result)
    assert store.flows.get_flow(job.flow_id).state is FlowState.FINALIZING
    assert github.comments == []
    assert store.jobs.get_job(job.id).result_applied_at is not None
    assert store.stops.has_pending(job.flow_id)
    assert store.workers.get_flow_execution(job.flow_id).released_at is None
    assert [j.kind for j in store.jobs.list_jobs(job.flow_id)] == [JobKind.INITIAL, JobKind.INFERENCE, JobKind.REVISION]


async def test_concurrent_application_has_one_remote_sender(scenario, monkeypatch) -> None:
    store, github, job, result = scenario
    started, release = asyncio.Event(), asyncio.Event()
    original = Issue.comment

    async def delayed_comment(issue, body):
        comment_id = await original(issue, body)
        started.set()
        await release.wait()
        return comment_id

    monkeypatch.setattr(Issue, "comment", delayed_comment)
    subject = coordinator(store, github)
    first = asyncio.create_task(apply_result(subject, job, result))
    second = None
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        second = asyncio.create_task(subject.results.apply(job.id))
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        release.set()
        await first
        if second is not None:
            await second
    assert len(github.comments) == 1
    assert [j.kind for j in store.jobs.list_jobs(job.flow_id)] == [JobKind.INITIAL, JobKind.INFERENCE, JobKind.REVISION]


async def test_process_interruption_recovers_a_running_effect(scenario, monkeypatch) -> None:
    store, github, job, result = scenario
    accepted = asyncio.Event()
    original = Issue.comment

    async def interrupted_comment(issue, body):
        await original(issue, body)
        accepted.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Issue, "comment", interrupted_comment)
    running = asyncio.create_task(apply_result(coordinator(store, github), job, result))
    await asyncio.wait_for(accepted.wait(), timeout=1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    comment = next(r for r in store.results.list_effects(job.id) if isinstance(r.payload, CommentEffect))
    assert comment.state is EffectState.RUNNING
    store.results.recover_claims()
    await apply_result(coordinator(store, github), job, result)
    assert len(github.comments) == 1
    assert [j.kind for j in store.jobs.list_jobs(job.flow_id)] == [JobKind.INITIAL, JobKind.INFERENCE, JobKind.REVISION]


async def test_rate_limit_waits_then_retries_a_definitely_rejected_request(
    scenario, result_clock, monkeypatch
) -> None:
    store, github, job, result = scenario
    original = Issue.comment
    attempts = []

    async def limited_comment(issue, body):
        attempts.append((issue.number, body))
        if len(attempts) == 1:
            raise GitHubError(
                GitHubErrorKind.RATE_LIMITED,
                "comment",
                status_code=429,
                retry_after="60",
                request_rejected=True,
            )
        return await original(issue, body)

    monkeypatch.setattr(Issue, "comment", limited_comment)
    with pytest.raises(ResultPending):
        await apply_result(coordinator(store, github), job, result)
    record = next(r for r in store.results.list_effects(job.id) if isinstance(r.payload, CommentEffect))
    assert record.state is EffectState.RETRY and record.dispatched_at is None
    result_clock.now += timedelta(seconds=10)
    with pytest.raises(ResultPending):
        await apply_result(coordinator(store, github), job, result)
    assert len(attempts) == 1
    result_clock.now += timedelta(seconds=60)
    await apply_result(coordinator(store, github), job, result)
    assert len(attempts) == 2 and len(github.comments) == 1
