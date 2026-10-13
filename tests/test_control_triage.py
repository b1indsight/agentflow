"""Judge is a batch prerequisite; only coding work enters the worker queue."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.controler.runtime import ServerRuntime
from app.controler.workflow.service import ControlService
from app.domain.models import JobKind, TriageVerdict, Verdict
from app.github import GitHubError, GitHubErrorKind, PullRequest
from tests.github import RecordingGitHub
from tests.review_memory import judge_decision
from tests.test_coordinator import coordinator
from tests.test_merge import route_merge
from tests.test_review_batches import claim, feedback, ingest, review_flow  # noqa: F401


@pytest.fixture
def triage_runtime(store, review_flow, recording_resources):  # noqa: F811
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = claim(store, clock)[0]
    runtime = ServerRuntime.__new__(ServerRuntime)
    runtime.store = store
    runtime.coordinator = coordinator(store, RecordingGitHub())
    runtime.judge = SimpleNamespace(triage=AsyncMock(side_effect=lambda context: judge_decision(
        TriageVerdict(verdict=Verdict.REVISE, confidence=1, instruction="Fix the bound"), context)))
    runtime.gateway = Mock()
    runtime._triage_task = None
    return runtime, batch


async def test_only_revision_enters_job_queue(store, triage_runtime):
    runtime, batch = triage_runtime
    assert store.jobs.list_jobs(batch.flow_id) == []
    await runtime._run_triage()
    await runtime._run_triage()
    jobs = store.jobs.list_jobs(batch.flow_id)
    assert len(jobs) == 1 and jobs[0].kind is JobKind.REVISION
    assert store.reviews.get_batch(batch.id).job_id == jobs[0].id
    assert runtime.judge.triage.await_count == 1


@pytest.mark.parametrize("action", ["cancel", "merge"])
async def test_late_decision_cannot_dispatch_retired_batch(store, triage_runtime, action):
    runtime, batch = triage_runtime
    decide = runtime.judge.triage.side_effect

    async def intervening(context):
        flow = store.flows.get_flow(batch.flow_id)
        if action == "cancel":
            ControlService(store).cancel(flow)
        else:
            await route_merge(store, flow)
        return decide(context)

    runtime.judge.triage.side_effect = intervening
    await runtime._run_triage()
    assert not any(j.kind is JobKind.REVISION for j in store.jobs.list_jobs(batch.flow_id))


async def test_interrupted_judge_retries_unfinished_batch(store, triage_runtime):
    runtime, batch = triage_runtime
    decide = runtime.judge.triage.side_effect
    runtime.judge.triage.side_effect = RuntimeError("interrupted")
    await runtime._run_triage()
    assert store.jobs.list_jobs(batch.flow_id) == []
    runtime.judge.triage.side_effect = decide
    await runtime._run_triage()
    assert len(store.jobs.list_jobs(batch.flow_id)) == 1


async def test_answer_definite_rejection_can_retry_saved_decision(store, triage_runtime, monkeypatch):
    # An explicit rejection proves the POST was not accepted; it must not leave
    # the batch permanently uncertain like a lost response does.
    runtime, batch = triage_runtime
    runtime.judge.triage.side_effect = lambda context: judge_decision(
        TriageVerdict(verdict=Verdict.ANSWER, answer="Ten."), context)
    github = runtime.coordinator.github
    comment = PullRequest.comment

    async def rejected_comment(pr, body):
        raise GitHubError(GitHubErrorKind.RATE_LIMITED, "comment", status_code=429,
                          request_rejected=True)

    monkeypatch.setattr(PullRequest, "comment", rejected_comment)
    with pytest.raises(GitHubError):
        await runtime.coordinator.triage(batch, runtime.judge)
    assert not store.reviews.get_batch(batch.id).answer_dispatched
    monkeypatch.setattr(PullRequest, "comment", comment)
    await runtime.coordinator.triage(store.reviews.get_batch(batch.id), runtime.judge)
    assert len(github.comments) == 1
    assert runtime.judge.triage.await_count == 1


async def test_slow_judge_keeps_settle_available_without_a_second_execution(triage_runtime):
    runtime, _ = triage_runtime
    entered, release = asyncio.Event(), asyncio.Event()
    decide = runtime.judge.triage.side_effect

    async def slow(context):
        entered.set()
        await release.wait()
        return decide(context)

    runtime.judge.triage.side_effect = slow
    runtime._settle_reviews()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        running = runtime._triage_task
        runtime._settle_reviews()
        assert runtime._triage_task is running and not running.done()
    finally:
        release.set()
        await runtime._triage_task
    assert runtime.judge.triage.await_count == 1
