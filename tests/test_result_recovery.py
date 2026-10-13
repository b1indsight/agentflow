"""Failure injection at the remote-write and final-commit boundaries without network."""

import asyncio
from datetime import timedelta
from typing import cast

import pytest

from app.contracts.protocol import (
    PROTOCOL_VERSION,
    FlowToolRequest,
    InitialTask,
    InitialTaskResult,
    RevisionExecutionRecord,
    RevisionTaskResult,
    Succeeded,
    terminal_digest,
)
from app.contracts.result_models import EffectState, ResultPending
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.service import ControlService
from app.domain.delivery_models import OpenedDelivery
from app.domain.models import (
    DeliveryState,
    Flow,
    FlowDelivery,
    FlowState,
    FlowToolCall,
    JobKind,
    JobState,
    VcsKind,
)
from app.github import GitHubError, GitHubErrorKind, Issue, Repository
from app.persistence.store import Store
from tests.github import RecordingGitHub
from tests.test_coordinator import MemoryStore, apply_result, coordinator
from tests.test_merge import route_merge
from tests.test_revision_delivery import revision_control as revision_control  # noqa: PLC0414


@pytest.fixture
def scenario(revision_control):
    store, github, job, _subject, task = revision_control
    result = RevisionTaskResult(
        flow_id=task.flow_id, attempt_id=task.attempt_id, batch_id=task.batch_id,
        feedback_refs=list(task.feedback), previous_session_id=task.session_id,
        session_id=task.session_id, session_generation=task.session_generation,
        mode="resume", last_message="failed", exit_code=1,
        invocations=[RevisionExecutionRecord(mode="resume", session_id=task.session_id, exit_code=1)],
    )
    return store, github, job, result


@pytest.fixture
def initial_scenario(scenario):
    store, github, job, _ = scenario
    github.pull_request = None
    job.kind = JobKind.INITIAL
    store.job_records[job.id] = job.model_copy(deep=True)
    store.delivery = FlowDelivery(
        flow_id=1,
        branch="flow/1",
        base_branch="trunk",
        state=DeliveryState.OPENING,
        head_sha="abc123",
        title="Title",
        body="Body\n\nCloses #8",
    )
    prepared = {"status": "prepared", "head_sha": "abc123", "delivery_version": 0}
    store.tool_calls["call-1"] = FlowToolCall(
        call_id="call-1",
        flow_id=1,
        attempt_id=1,
        job_id=job.id,
        name="flow.open_pr",
        arguments={"title": "Title", "body": "Body"},
        response=prepared,
    )
    result = InitialTaskResult(
        session_id="session-1",
        last_message="done",
        exit_code=0,
        vcs_backend=VcsKind.GIT,
        branch="flow/1",
        commit_sha="abc123",
        remote_sha="abc123",
        pushed=True,
        open_pr_call_id="call-1",
        delivery_receipt=prepared,
    )
    return store, github, job, result


@pytest.fixture
async def completed_open_initial(
    store: Store, recording_resources: None,
) -> tuple[Flow, JobCoordinator, RecordingGitHub, FlowToolRequest, InitialTaskResult]:
    """Complete a real INITIAL assignment before testing retries of its existing PR."""
    flow = ControlService(store).on_trigger("owner/repo", 8, 42)
    assert flow is not None
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "epoch")
    job = store.workers.assign_job("worker-a", "token", "epoch")
    assert job is not None
    github = RecordingGitHub()
    subject = coordinator(store, github)
    task = await subject.prepare(job)
    assert isinstance(task, InitialTask)
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    request = FlowToolRequest(
        job_id=job.id, flow_id=flow.id, attempt_id=task.attempt_id,
        assignment_token="token", connection_epoch="epoch", call_id="initial-open",
        name="flow.open_pr", arguments={"title": "Title", "body": "Body"},
        vcs_backend=VcsKind.GIT, branch=task.branch, commit_sha="abc123", remote_sha="abc123",
    )
    tool = await subject.tools.execute(request, worker_id="worker-a")
    result = InitialTaskResult(
        session_id="initial-session", last_message="done", exit_code=0,
        vcs_backend=VcsKind.GIT, branch=task.branch, commit_sha="abc123", remote_sha="abc123",
        pushed=True, open_pr_call_id="initial-open", delivery_receipt=tool.result,
    )
    terminal = Succeeded(job_id=job.id, assignment_token="token", connection_epoch="epoch", result=result)
    assert store.workers.record_assignment_result(job.id, "worker-a", "token", "epoch",
        terminal.model_dump(mode="json"), terminal_digest(terminal)) == "pending"
    assert await subject.results.apply(job.id) is JobState.DONE
    completed = store.flows.get_flow(flow.id)
    assert completed is not None
    return completed, subject, github, request, result


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_merged_initial_retry_keeps_execution_facts_without_reopening(
    store, completed_open_initial, exit_code,
):
    # A cancelled completed INITIAL can retry against its still-open PR, which a human can merge.
    flow, subject, github, initial_request, initial_result = completed_open_initial
    service = ControlService(store)
    service.cancel(flow)
    service.retry(flow)
    # Fresh worker-b starts from seed A, then checks out existing H where H descends A and H != A.
    store.workers.worker_connected("worker-b", PROTOCOL_VERSION, "epoch-b")
    job = store.workers.assign_job("worker-b", "retry-token", "epoch-b")
    assert job is not None and job.kind is JobKind.INITIAL
    task = await subject.prepare(job)
    assert isinstance(task, InitialTask) and task.attempt_id == flow.current_attempt_id
    assert task.branch == initial_request.branch
    assert task.repo_identity == "worker-b:owner/repo"
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-b", "retry-token", "epoch-b")
    tool = await subject.tools.execute(initial_request.model_copy(update={
        "job_id": job.id, "assignment_token": "retry-token", "connection_epoch": "epoch-b",
        "call_id": "retry-open",
    }), worker_id="worker-b")
    assert isinstance(tool.result, OpenedDelivery)
    call = store.deliveries.get_tool_call("retry-open")
    assert call is not None and call.job_id == job.id and call.response == tool.result
    await route_merge(store, flow)
    merged = store.deliveries.get_delivery(flow.id)
    assert merged is not None and merged.state is DeliveryState.MERGED
    if exit_code == 0:
        result = initial_result.model_copy(update={
            "session_id": "retry-session", "open_pr_call_id": "retry-open", "delivery_receipt": tool.result,
        })
    else:
        result = InitialTaskResult(session_id="retry-session", last_message="failed", exit_code=exit_code)
    terminal = Succeeded(job_id=job.id, assignment_token="retry-token", connection_epoch="epoch-b", result=result)
    digest = terminal_digest(terminal)
    assert store.workers.record_assignment_result(job.id, "worker-b", "retry-token", "epoch-b",
        terminal.model_dump(mode="json"), digest) == "pending"
    expected = JobState.DONE if exit_code == 0 else JobState.FAILED
    assert await subject.results.apply(job.id) is expected
    assert store.deliveries.get_delivery(flow.id) == merged
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.flows.get_attempt(task.attempt_id).codex_session_id == "initial-session"
    accepted = store.jobs.get_job(job.id)
    assert accepted.result_digest == digest and accepted.result_applied_at is not None
    assert accepted.result_payload == terminal.model_dump(mode="json")
    assert github.opened == [task.branch]


async def test_cancel_before_terminal_keeps_executor_slot_until_result_ack(scenario):
    # Cancel is not a remote kill: leave the running assignment receivable until its result arrives.
    store, github, job, result = scenario
    store.job_records[job.id] = job
    ControlService(cast(Store, store)).cancel(store.flow)
    assert store.job_records[job.id].state is JobState.RUNNING
    assert await apply_result(coordinator(store, github), job, result) is JobState.DONE
    assert store.flow.state is FlowState.CANCELLED
    assert store.job_records[job.id].result_applied_at is not None
    assert not github.comments and not store.enqueued


async def test_review_label_preserves_user_labels_on_delivery_and_replay(initial_scenario):
    # Human labels may predate delivery; replaying a frozen status intent must not erase them.
    store, github, job, result = initial_scenario
    github.labels = [["bug", "user-owned"]]
    subject = coordinator(store, github)
    await apply_result(subject, job, result)
    await apply_result(subject, job, result)
    assert github.labels[-1] == ["bug", "user-owned", "flow:in-review"]
    assert len(github.labels) == 2
    assert [call for call in github.resource_calls if call["operation"] == "add_label"] == [{
        "operation": "add_label", "installation_id": store.flow.installation_id,
        "repo": store.flow.repo, "number": 17,
    }]


async def test_initial_delivery_commits_only_after_all_required_effects(
    initial_scenario, monkeypatch
) -> None:
    store, github, job, result = initial_scenario
    original = Issue.add_label
    limited = [True]

    async def add_label(issue, label):
        if limited:
            limited.pop()
            raise GitHubError(GitHubErrorKind.RATE_LIMITED, "add_label", status_code=429,
                              request_rejected=True)
        await original(issue, label)

    monkeypatch.setattr(Issue, "add_label", add_label)
    with pytest.raises(ResultPending):
        await apply_result(coordinator(store, github), job, result)
    assert store.delivery.state is DeliveryState.OPENING
    assert store.job_records[job.id].result_applied_at is None
    accepted = store.jobs.get_job(job.id)
    store.results.clock += timedelta(seconds=10)
    await apply_result(coordinator(store, github), job, result)
    await apply_result(coordinator(store, github), job, result)
    assert store.delivery.state is DeliveryState.OPEN
    assert len(github.opened) == 1
    persisted = store.jobs.get_job(job.id)
    assert persisted.result_payload == accepted.result_payload
    assert persisted.result_digest == accepted.result_digest
    assert persisted.result_received_at == accepted.result_received_at
    assert store.job_records[job.id].result_applied_at is not None


async def test_closed_pr_after_lost_creation_response_finishes_without_new_writes(
    initial_scenario, monkeypatch
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
    assert github.pull_request is not None
    github.pull_request = github.pull_request.model_copy(update={"state": "closed"})
    store.results.clock += timedelta(seconds=10)
    assert await apply_result(subject, job, result) is JobState.DONE
    assert store.delivery.state is DeliveryState.CLOSED
    assert store.job_records[job.id].result_applied_at is not None
    assert len(github.opened) == 1
    assert github.labels == github.review_requests == []


async def test_remote_comment_is_recovered_after_response_loss(scenario, monkeypatch) -> None:
    # GitHub accepted the POST, but the control process never received its response.
    store, github, job, result = scenario
    original = Issue.comment

    async def lose_response(issue, body):
        await original(issue, body)
        raise GitHubError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(Issue, "comment", lose_response)
    with pytest.raises(ResultPending):
        await apply_result(coordinator(store, github), job, result)
    assert store.enqueued == []
    assert store.results.list_effects(job.id)[0].state is EffectState.UNCERTAIN
    store.results.clock += timedelta(seconds=10)
    state = await apply_result(coordinator(store, github), job, result)
    assert state is JobState.FAILED
    assert len(github.comments) == 1 and store.enqueued == []
    assert store.job_records[job.id].result_applied_at is not None
    assert [(call["operation"], call["installation_id"], call["repo"], call["number"])
            for call in github.resource_calls if call["operation"] in {"comment", "find_comment"}] == [
        ("find_comment", 42, "owner/repo", 17), ("comment", 42, "owner/repo", 17),
        ("find_comment", 42, "owner/repo", 17),
    ]


async def test_merge_during_comment_lookup_prevents_dispatch(scenario, monkeypatch):
    # The merge commits while an old result is awaiting a GitHub read, just before POST.
    store, github, job, result = scenario
    original = Issue.find_comment

    async def merge_during_lookup(issue, marker):
        store.delivery.state = DeliveryState.MERGED
        store.delivery.version += 1
        store.reviews.cancel(store.flow.id)
        return await original(issue, marker)

    monkeypatch.setattr(Issue, "find_comment", merge_during_lookup)
    subject = coordinator(store, github)
    try:
        await apply_result(subject, job, result)
    except ResultPending:
        store.results.clock += timedelta(seconds=30)
        await apply_result(subject, job, result)
    assert store.job_records[job.id].result_applied_at is not None
    assert store.flow.state is FlowState.ACTIVE
    assert github.comments == []
    assert store.enqueued == []


@pytest.mark.parametrize("remote_receipt", [True, False])
@pytest.mark.parametrize("transition", ["merge", "cancel"])
async def test_retired_result_reconciles_dispatched_comment_without_resending(
    scenario, monkeypatch, remote_receipt, transition,
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
    if transition == "merge":
        store.delivery.state = DeliveryState.MERGED
        store.delivery.version += 1
        store.reviews.cancel(store.flow.id)
    else:
        ControlService(cast(Store, store)).cancel(store.flow)
    store.results.clock += timedelta(seconds=30)
    if remote_receipt:
        assert await apply_result(subject, job, result) is JobState.FAILED
        assert store.results.list_effects(job.id)[0].state is EffectState.SUCCEEDED
    else:
        with pytest.raises(ResultPending):
            await apply_result(subject, job, result)
        assert store.job_records[job.id].result_applied_at is None
    assert len(calls) == 1
    assert store.enqueued == []
    assert store.flow.state is (FlowState.ACTIVE if transition == "merge" else FlowState.CANCELLED)


async def test_unknown_comment_is_not_resubmitted_and_blocks_retry(scenario, monkeypatch) -> None:
    store, github, job, result = scenario
    sent = []

    async def ambiguous_post(issue, body):
        sent.append((issue.number, body))
        raise GitHubError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(Issue, "comment", ambiguous_post)
    for _ in range(3):
        with pytest.raises(ResultPending):
            await apply_result(coordinator(store, github), job, result)
        store.results.clock += timedelta(seconds=30)
    assert len(sent) == 1
    assert store.job_records[job.id].result_applied_at is None
    with pytest.raises(ValueError, match="pending result"):
        ControlService(cast(Store, store)).retry(store.flow)


async def test_final_commit_failure_rolls_back_completion_but_keeps_effect_receipt(
    scenario,
    monkeypatch,
) -> None:
    # Even a failure after setting the result marker must roll back the whole database unit.
    store, github, job, result = scenario
    complete = MemoryStore.complete_persisted_result
    failures = [True]

    def fail_once(self, job_id, state):
        applied = complete(self, job_id, state)
        if failures:
            failures.pop()
            raise RuntimeError("commit interrupted")
        return applied

    monkeypatch.setattr(MemoryStore, "complete_persisted_result", fail_once)
    with pytest.raises(RuntimeError, match="commit interrupted"):
        await apply_result(coordinator(store, github), job, result)
    assert store.enqueued == []
    assert store.job_records[job.id].result_applied_at is None
    assert store.results.list_effects(job.id)[0].state is EffectState.SUCCEEDED
    await apply_result(coordinator(store, github), job, result)
    assert len(github.comments) == 1 and store.enqueued == []


async def test_cancel_during_remote_lookup_does_not_authorize_a_comment(
    scenario, monkeypatch
) -> None:
    store, github, job, result = scenario

    async def cancel_during_lookup(issue, marker):
        ControlService(cast(Store, store)).cancel(store.flow)

    monkeypatch.setattr(Issue, "find_comment", cancel_during_lookup)
    with pytest.raises(ResultPending):
        await apply_result(coordinator(store, github), job, result)
    await apply_result(coordinator(store, github), job, result)
    assert store.flow.state is FlowState.CANCELLED
    assert github.comments == store.enqueued == []
    assert store.job_records[job.id].result_applied_at is not None


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
    assert len(github.comments) == 1 and store.enqueued == []


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
    assert store.results.list_effects(job.id)[0].state is EffectState.RUNNING
    store.results.recover_claims()
    await apply_result(coordinator(store, github), job, result)
    assert len(github.comments) == 1 and store.enqueued == []


async def test_rate_limit_waits_then_retries_a_definitely_rejected_request(
    scenario, monkeypatch
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
    record = store.results.list_effects(job.id)[0]
    assert record.state is EffectState.RETRY and record.dispatched_at is None
    store.results.clock += timedelta(seconds=10)
    with pytest.raises(ResultPending):
        await apply_result(coordinator(store, github), job, result)
    assert len(attempts) == 1
    store.results.clock += timedelta(seconds=60)
    await apply_result(coordinator(store, github), job, result)
    assert len(attempts) == 2 and len(github.comments) == 1
