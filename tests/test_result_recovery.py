"""Failure injection at the remote-write and final-commit boundaries, without network or sleeps."""

import asyncio
from datetime import timedelta
from typing import cast

import pytest

from app.control.service import ControlService
from app.github_models import GitHubAdapterError, GitHubErrorKind
from app.models import (
    Attempt,
    DeliveryState,
    FeedbackRef,
    FlowDelivery,
    FlowState,
    FlowToolCall,
    Job,
    JobKind,
    JobState,
    TriageVerdict,
    VcsKind,
    Verdict,
)
from app.protocol import InitialResult, TriageResult
from app.result_models import EffectState, ResultPending
from app.store import Store
from tests.review_memory import judge_decision
from tests.test_coordinator import MemoryStore, RecordingGitHub, apply_result, coordinator


@pytest.fixture
def scenario():
    store = MemoryStore()
    store.attempt = Attempt(id=1, flow_id=1, codex_session_id="session-1")
    store.flow.current_attempt_id = 1
    store.delivery = FlowDelivery(
        flow_id=1,
        branch="flow/1",
        base_branch="trunk",
        state=DeliveryState.OPEN,
        pr_number=17,
    )
    github = RecordingGitHub()
    job = Job(id=6, flow_id=1, kind=JobKind.TRIAGE, state=JobState.RUNNING)
    result = TriageResult(batch_id=1, feedback_refs=[FeedbackRef(item_id=101, version=1)],
        decision=judge_decision(TriageVerdict(
            verdict=Verdict.REVISE,
            confidence=0.9,
            instruction="Fix it",
        ))
    )
    return store, github, job, result


@pytest.mark.parametrize("violation", ["digest", "incomplete-context"])
async def test_control_rejects_decisions_without_matching_complete_evidence(scenario, violation):
    store, github, job, result = scenario
    if violation == "digest":
        result.decision.context_digest = "0" * 64
    else:
        store.reviews.batch.context.limitations.append("files:truncated")
        result.decision.context_digest = store.reviews.batch.context.digest
    await apply_result(coordinator(store, github), job, result)
    assert github.comments == []
    assert store.reviews.consumed == []
    assert not store.sources


async def test_old_attempt_failure_does_not_publish_or_fail_current_attempt(scenario):
    store, github, job, _ = scenario
    store.jobs[job.id] = job
    store.attempt = Attempt(id=2, flow_id=1, codex_session_id="new-session")
    store.flow.current_attempt_id = 2
    await coordinator(store, github).fail(job, "Old worker failed")
    assert github.comments == []
    assert store.flow.state is FlowState.ACTIVE




@pytest.fixture
def initial_scenario(scenario):
    store, github, job, _ = scenario
    job.kind = JobKind.INITIAL
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
    result = InitialResult(
        session_id="session-1",
        last_message="done",
        exit_code=0,
        vcs_backend=VcsKind.GIT,
        branch="flow/1",
        commit_sha="abc123",
        remote_sha="abc123",
        pushed=True,
        open_pr_call_id="call-1",
        pull_request=prepared,
    )
    return store, github, job, result


async def test_cancel_before_terminal_keeps_executor_slot_until_result_ack(scenario):
    # Cancel is not a remote kill: leave the running assignment receivable until its result arrives.
    store, github, job, result = scenario
    store.jobs[job.id] = job
    ControlService(cast(Store, store)).cancel(store.flow)
    assert store.jobs[job.id].state is JobState.RUNNING
    assert await apply_result(coordinator(store, github), job, result) is JobState.DONE
    assert store.flow.state is FlowState.CANCELLED
    assert store.jobs[job.id].result_applied_at is not None
    assert not github.comments and not store.enqueued


async def test_initial_delivery_commits_only_after_all_required_effects(
    initial_scenario, monkeypatch
) -> None:
    store, github, job, result = initial_scenario
    original = github.set_labels
    limited = [True]

    async def set_labels(*args):
        if limited:
            limited.pop()
            raise GitHubAdapterError(GitHubErrorKind.RATE_LIMITED, "set_labels", status_code=429)
        await original(*args)

    monkeypatch.setattr(github, "set_labels", set_labels)
    with pytest.raises(ResultPending):
        await apply_result(coordinator(store, github), job, result)
    assert store.delivery.state is DeliveryState.OPENING
    assert store.jobs[job.id].result_applied_at is None
    assert store.runs == []
    store.results.clock += timedelta(seconds=10)
    await apply_result(coordinator(store, github), job, result)
    await apply_result(coordinator(store, github), job, result)
    assert store.delivery.state is DeliveryState.OPEN
    assert len(github.opened) == len(store.runs) == 1
    assert store.jobs[job.id].result_applied_at is not None


async def test_initial_delivery_records_closed_pr_without_reopening(initial_scenario) -> None:
    from app.github_models import PullRequestSnapshot

    store, github, job, result = initial_scenario
    github.pull_request = PullRequestSnapshot(
        number=17,
        state="closed",
        head_branch="flow/1",
        head_sha="abc123",
        base_branch="trunk",
    )
    state = await apply_result(coordinator(store, github), job, result)
    assert state is JobState.DONE
    assert store.flow.state is FlowState.ACTIVE
    assert store.delivery.state is DeliveryState.CLOSED and store.delivery.pr_number == 17
    assert github.opened == github.labels == github.review_requests == []


async def test_closed_pr_after_lost_creation_response_finishes_without_new_writes(
    initial_scenario, monkeypatch
):
    store, github, job, result = initial_scenario
    create = github.open_pull_request

    async def create_then_lose_response(*args, **kwargs):
        await create(*args, **kwargs)
        raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "open_pull_request")

    monkeypatch.setattr(github, "open_pull_request", create_then_lose_response)
    subject = coordinator(store, github)
    with pytest.raises(ResultPending):
        await apply_result(subject, job, result)
    assert github.pull_request is not None
    github.pull_request = github.pull_request.model_copy(update={"state": "closed"})
    store.results.clock += timedelta(seconds=10)
    assert await apply_result(subject, job, result) is JobState.DONE
    assert store.delivery.state is DeliveryState.CLOSED
    assert store.jobs[job.id].result_applied_at is not None
    assert len(github.opened) == 1
    assert github.labels == github.review_requests == []


async def test_remote_comment_is_recovered_after_response_loss(scenario, monkeypatch) -> None:
    # GitHub accepted the POST, but the control process never received its response.
    store, github, job, result = scenario
    original = github.comment

    async def lose_response(*args):
        await original(*args)
        raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(github, "comment", lose_response)
    with pytest.raises(ResultPending):
        await apply_result(coordinator(store, github), job, result)
    assert store.enqueued == []
    assert store.results.list_effects(job.id)[0].state is EffectState.UNCERTAIN
    store.results.clock += timedelta(seconds=10)
    state = await apply_result(coordinator(store, github), job, result)
    assert state is JobState.DONE
    assert len(github.comments) == len(store.enqueued) == 1
    assert store.jobs[job.id].result_applied_at is not None


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_merged_initial_result_keeps_execution_facts_without_reopening(initial_scenario, exit_code):
    # The human merged the PR opened by flow.open_pr before INITIAL returned its final result.
    store, github, job, result = initial_scenario
    store.delivery.state = DeliveryState.MERGED
    store.delivery.pr_number = 17
    store.delivery.version += 1
    result = result.model_copy(update={"exit_code": exit_code})
    subject = coordinator(store, github)
    await apply_result(subject, job, result)
    await apply_result(subject, job, result)
    assert store.flow.state is FlowState.ACTIVE
    assert store.delivery.state is DeliveryState.MERGED
    assert store.execution.released_at is None
    assert len(store.runs) == 1
    assert github.comments == []
    assert store.results.list_effects(job.id) == []


async def test_merge_during_comment_lookup_prevents_dispatch(scenario, monkeypatch):
    # The merge commits while an old result is awaiting a GitHub read, just before POST.
    store, github, job, result = scenario
    original = github.find_comment

    async def merge_during_lookup(*args):
        store.delivery.state = DeliveryState.MERGED
        store.delivery.version += 1
        store.reviews.cancel(store.flow.id)
        return await original(*args)

    monkeypatch.setattr(github, "find_comment", merge_during_lookup)
    subject = coordinator(store, github)
    try:
        await apply_result(subject, job, result)
    except ResultPending:
        store.results.clock += timedelta(seconds=30)
        await apply_result(subject, job, result)
    assert store.jobs[job.id].result_applied_at is not None
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
    original = github.comment
    calls = []

    async def lose_response(*args):
        calls.append(args)
        if remote_receipt:
            await original(*args)
        raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(github, "comment", lose_response)
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
        assert await apply_result(subject, job, result) is JobState.DONE
        assert store.results.list_effects(job.id)[0].state is EffectState.SUCCEEDED
    else:
        with pytest.raises(ResultPending):
            await apply_result(subject, job, result)
        assert store.jobs[job.id].result_applied_at is None
    assert len(calls) == 1
    assert store.enqueued == []
    assert store.flow.state is (FlowState.ACTIVE if transition == "merge" else FlowState.CANCELLED)


async def test_unknown_comment_is_not_resubmitted_and_blocks_retry(scenario, monkeypatch) -> None:
    store, github, job, result = scenario
    sent = []

    async def ambiguous_post(*args):
        sent.append(args)
        raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(github, "comment", ambiguous_post)
    for _ in range(3):
        with pytest.raises(ResultPending):
            await apply_result(coordinator(store, github), job, result)
        store.results.clock += timedelta(seconds=30)
    assert len(sent) == 1
    assert store.jobs[job.id].result_applied_at is None
    with pytest.raises(ValueError, match="pending result"):
        ControlService(cast(Store, store)).retry(store.flow)


async def test_final_commit_failure_rolls_back_derived_job_but_keeps_effect_receipt(
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
    assert store.jobs[job.id].result_applied_at is None
    assert store.results.list_effects(job.id)[0].state is EffectState.SUCCEEDED
    await apply_result(coordinator(store, github), job, result)
    assert len(store.enqueued) == len(github.comments) == 1


async def test_cancel_during_remote_lookup_does_not_authorize_a_comment(
    scenario, monkeypatch
) -> None:
    store, github, job, result = scenario

    async def cancel_during_lookup(*args):
        ControlService(cast(Store, store)).cancel(store.flow)

    monkeypatch.setattr(github, "find_comment", cancel_during_lookup)
    with pytest.raises(ResultPending):
        await apply_result(coordinator(store, github), job, result)
    await apply_result(coordinator(store, github), job, result)
    assert store.flow.state is FlowState.CANCELLED
    assert github.comments == store.enqueued == []
    assert store.jobs[job.id].result_applied_at is not None


async def test_concurrent_application_has_one_remote_sender(scenario, monkeypatch) -> None:
    store, github, job, result = scenario
    started, release = asyncio.Event(), asyncio.Event()
    original = github.comment

    async def delayed_comment(*args):
        comment_id = await original(*args)
        started.set()
        await release.wait()
        return comment_id

    monkeypatch.setattr(github, "comment", delayed_comment)
    first = asyncio.create_task(apply_result(coordinator(store, github), job, result))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        with pytest.raises(ResultPending):
            await apply_result(coordinator(store, github), job, result)
    finally:
        release.set()
        await first
    assert len(github.comments) == len(store.enqueued) == 1


async def test_process_interruption_recovers_a_running_effect(scenario, monkeypatch) -> None:
    store, github, job, result = scenario
    accepted = asyncio.Event()
    original = github.comment

    async def interrupted_comment(*args):
        await original(*args)
        accepted.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(github, "comment", interrupted_comment)
    running = asyncio.create_task(apply_result(coordinator(store, github), job, result))
    await asyncio.wait_for(accepted.wait(), timeout=1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert store.results.list_effects(job.id)[0].state is EffectState.RUNNING
    store.results.recover_claims()
    await apply_result(coordinator(store, github), job, result)
    assert len(github.comments) == len(store.enqueued) == 1


async def test_rate_limit_waits_then_retries_a_definitely_rejected_request(
    scenario, monkeypatch
) -> None:
    store, github, job, result = scenario
    original = github.comment
    attempts = []

    async def limited_comment(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise GitHubAdapterError(
                GitHubErrorKind.RATE_LIMITED,
                "comment",
                status_code=429,
                retry_after="60",
            )
        return await original(*args)

    monkeypatch.setattr(github, "comment", limited_comment)
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


async def test_legacy_result_is_not_blindly_replayed(scenario) -> None:
    store, github, job, result = scenario
    store.accept(job, result)
    store.jobs[job.id].result_recovery_version = 0
    with pytest.raises(ResultPending, match="pre-upgrade"):
        await apply_result(coordinator(store, github), job, result)
    assert "manual reconciliation" in store.jobs[job.id].last_error
    assert github.comments == store.enqueued == []
