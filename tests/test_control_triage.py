"""Review decisions gate coding work, including durable inference failure and replay."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.testclient import TestClient

from app.contracts.protocol import InferenceResult, result_header
from app.controler.api import create_app
from app.controler.review.judge import ReviewJudge
from app.controler.workflow.inference import InferencePending, JobInference
from app.controler.workflow.service import ControlService
from app.domain.inference_models import InferenceCapability, InferenceFailure, InferenceOutput
from app.domain.models import JobKind, JobState, TriageVerdict, Verdict
from app.domain.review_models import BatchState
from app.github import GitHubError, GitHubErrorKind, PullRequest
from app.persistence.inference_notices import InferenceNoticeStore
from tests.github import RecordingGitHub
from tests.review_memory import judge_decision
from tests.test_console_api import console_runtime as console_runtime  # noqa: PLC0414
from tests.test_merge import route_merge
from tests.test_review_batches import claim, feedback, ingest
from tests.test_review_batches import review_flow as review_flow  # noqa: PLC0414
from tests.workspace_fixtures import register_worker


@pytest.fixture
def triage_runtime(store, review_flow, recording_resources, monkeypatch, server_runtime):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = claim(store, clock)[0]
    runtime = server_runtime(clock=lambda: clock.now)
    judge = SimpleNamespace(triage=AsyncMock(side_effect=lambda context: judge_decision(
        TriageVerdict(verdict=Verdict.REVISE, confidence=1, instruction="Fix the bound"), context)))
    monkeypatch.setattr("app.controler.runtime.ReviewJudge", lambda *args, **kwargs: judge)
    return runtime, batch, judge


async def test_only_revision_enters_job_queue(store, triage_runtime, review_flow):
    runtime, batch, judge = triage_runtime
    flow, clock = review_flow
    assert store.jobs.list_jobs(batch.flow_id) == []
    await runtime._run_triage()
    await runtime._run_triage()
    jobs = store.jobs.list_jobs(batch.flow_id)
    assert len(jobs) == 1 and jobs[0].kind is JobKind.REVISION
    assert store.reviews.get_batch(batch.id).job_id == jobs[0].id
    assert store.reviews.for_job(jobs[0]).id == batch.id
    assert runtime.coordinator.github.comments == []
    assert judge.triage.await_count == 1
    ingest(store, flow, feedback(clock, 102), "next")
    assert claim(store, clock) == []
    await runtime._run_triage()
    assert store.jobs.list_jobs(flow.id) == jobs
    assert judge.triage.await_count == 1
    assert runtime.coordinator.github.comments == []


@pytest.mark.parametrize("action", ["cancel", "merge"])
async def test_late_decision_cannot_dispatch_retired_batch(store, triage_runtime, action):
    runtime, batch, judge = triage_runtime
    decide = judge.triage.side_effect

    async def intervening(context):
        flow = store.flows.get_flow(batch.flow_id)
        if action == "cancel":
            ControlService(
                database=store.database,
            ).cancel(flow)
        else:
            await route_merge(store, flow)
        return decide(context)

    judge.triage.side_effect = intervening
    await runtime._run_triage()
    assert not any(j.kind is JobKind.REVISION for j in store.jobs.list_jobs(batch.flow_id))


async def test_interrupted_judge_retries_unfinished_batch(store, triage_runtime):
    runtime, batch, judge = triage_runtime
    decide = judge.triage.side_effect
    judge.triage.side_effect = RuntimeError("interrupted")
    await runtime._run_triage()
    assert store.jobs.list_jobs(batch.flow_id) == []
    judge.triage.side_effect = decide
    await runtime._run_triage()
    assert len(store.jobs.list_jobs(batch.flow_id)) == 1


async def test_answer_definite_rejection_can_retry_saved_decision(store, triage_runtime, monkeypatch):
    # An explicit rejection proves the POST was not accepted; it must not leave
    # the batch permanently uncertain like a lost response does.
    runtime, batch, judge = triage_runtime
    judge.triage.side_effect = lambda context: judge_decision(
        TriageVerdict(verdict=Verdict.ANSWER, answer="Ten."), context)
    github = runtime.coordinator.github
    comment = PullRequest.comment

    async def rejected_comment(pr, body):
        raise GitHubError(GitHubErrorKind.RATE_LIMITED, "comment", status_code=429,
                          request_rejected=True)

    monkeypatch.setattr(PullRequest, "comment", rejected_comment)
    with pytest.raises(GitHubError):
        await runtime.coordinator.triage(batch, judge)
    assert not store.reviews.get_batch(batch.id).answer_dispatched
    monkeypatch.setattr(PullRequest, "comment", comment)
    await runtime.coordinator.triage(store.reviews.get_batch(batch.id), judge)
    assert len(github.comments) == 1
    assert judge.triage.await_count == 1


async def test_slow_judge_keeps_settle_available_without_a_second_execution(triage_runtime):
    runtime, _, judge = triage_runtime
    entered, release = asyncio.Event(), asyncio.Event()
    decide = judge.triage.side_effect

    async def slow(context):
        entered.set()
        await release.wait()
        return decide(context)

    judge.triage.side_effect = slow
    runtime._settle_reviews()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        running = runtime._triage_task
        runtime._settle_reviews()
        assert runtime._triage_task is running and not running.done()
    finally:
        release.set()
        await runtime._triage_task
    assert judge.triage.await_count == 1


async def finish_inference(store, subject, reply: str | InferenceFailure):
    job = store.workers.assign_job("plain", "token", "epoch", slot="infer")
    task = job.inference.current_task
    response = InferenceResult(
        **result_header(task.request_id), method="worker.infer",
        job_id=job.id, assignment_token=job.assignment_token,
        output=InferenceOutput(request_id=task.request_id, request_digest=task.digest,
            text=reply, adapter_version="codex/v1") if isinstance(reply, str) else None,
        failure=reply if isinstance(reply, InferenceFailure) else None,
    )
    assert store.workers.record_assignment_result(
        job.id, "plain", job.assignment_token, "epoch", response,
    ) == "pending"
    await subject.results.apply(job.id)


async def test_review_recomputes_after_pr_push_without_replaying_old_answer(
    store, review_flow, recording_resources, server_runtime,
):
    # A push between inference completion and comment delivery makes the saved answer stale.
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = claim(store, clock)[0]
    github = RecordingGitHub()
    runtime = server_runtime(github=github, clock=lambda: clock.now)
    subject = runtime.coordinator
    judge = ReviewJudge(JobInference(
        runtime.database, runtime.gateway.wake_all, flow_id=flow.id, batch_id=batch.id,
    ))
    register_worker(store, "plain", epoch="epoch")
    store.workers.worker_ready("plain", "epoch", InferenceCapability(adapter_version="codex/v1"))

    with pytest.raises(InferencePending) as first:
        await subject.triage(batch, judge)
    old_context = store.reviews.get_batch(batch.id).context
    await finish_inference(store, subject, '{"verdict":"answer","answer":"Old snapshot result"}')
    github.pull_request = old_context.pull_request.model_copy(update={"head_sha": "changed"})
    await subject.triage(store.reviews.get_batch(batch.id), judge)
    assert store.reviews.get_batch(batch.id).context is None
    assert github.comments == []

    with pytest.raises(InferencePending) as replacement:
        await subject.triage(store.reviews.get_batch(batch.id), judge)
    assert replacement.value.job_id != first.value.job_id
    fresh = store.reviews.get_batch(batch.id)
    job = store.jobs.get_job(replacement.value.job_id)
    assert job.inference.context_digest == fresh.context.digest != old_context.digest
    assert job.attempt == 0
    assert github.comments == []

    await finish_inference(store, subject, '{"verdict":"answer","answer":"New snapshot result"}')
    await subject.triage(store.reviews.get_batch(batch.id), judge)
    assert len(github.comments) == 1 and github.comments[0].startswith("New snapshot result\n")
    assert all(job.kind is JobKind.INFERENCE for job in store.jobs.list_jobs(flow.id))
    assert store.jobs.get_job(first.value.job_id).state is JobState.DONE


async def test_exhausted_inference_recovers_notice_without_a_fourth_attempt(
    store, review_flow, recording_resources, console_runtime, monkeypatch, server_runtime,
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = claim(store, clock)[0]
    runtime = server_runtime(settings={"judge_timeout_seconds": 5}, clock=lambda: clock.now)
    register_worker(store, "plain", epoch="epoch")
    store.workers.worker_ready("plain", "epoch", InferenceCapability(adapter_version="codex/v1"))
    await runtime._run_triage()
    timeout = InferenceFailure(code="execution_timeout")
    for _ in range(2):
        await finish_inference(store, runtime.coordinator, timeout)

    # A crash after the last receipt must recover that receipt, not call the provider again.
    with monkeypatch.context() as patch:
        patch.setattr(InferenceNoticeStore, "append", Mock(side_effect=RuntimeError("database unavailable")))
        with pytest.raises(RuntimeError, match="database unavailable"):
            await finish_inference(store, runtime.coordinator, timeout)
        await runtime._run_triage()
        job, = store.jobs.list_jobs(flow.id)
        assert job.attempt == 3 and job.state is JobState.RUNNING
        assert job.result_received_at is not None and job.result_applied_at is None

    runtime = server_runtime(
        github=runtime.coordinator.github,
        settings={"judge_timeout_seconds": 5}, clock=lambda: clock.now,
    )
    await runtime.results.recover_pending_results()
    await runtime.results.recover_pending_results()
    await runtime._run_triage()
    job, = store.jobs.list_jobs(flow.id)
    assert job.kind is JobKind.INFERENCE and job.state is JobState.FAILED and job.attempt == 3
    assert store.reviews.get_batch(batch.id).state is BatchState.READY
    assert not runtime.coordinator.github.comments
    with TestClient(create_app(console_runtime)) as client:
        response = client.get(f"/api/flows/{flow.id}/inference-notices",
                              headers=console_runtime.issuer.headers())
        assert response.status_code == 200
        notice, = response.json()["items"]
        assert notice["key"] == f"inference-exhausted:{batch.id}"
        assert notice["attempts"] == 3 and notice["last_failure_code"] == "execution_timeout"
