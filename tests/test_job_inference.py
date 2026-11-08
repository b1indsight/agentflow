"""Controller inference uses durable jobs; pending calls never block later batches."""

from unittest.mock import Mock

import pytest

from app.contracts.protocol import InferenceResult, result_header
from app.controler.review.judge import ReviewJudge
from app.controler.workflow.inference import InferencePending, JobInference
from app.controler.workflow.results import ResultApplication
from app.domain.inference_models import (
    InferenceCapability,
    InferenceMessage,
    InferenceOutput,
    InferenceTask,
)
from app.domain.models import JobKind, JobState, TriageVerdict, Verdict
from tests.github import RecordingGitHub
from tests.test_coordinator import coordinator
from tests.test_review_batches import claim, feedback, ingest
from tests.test_review_batches import review_flow as review_flow  # noqa: PLC0414


async def test_generic_inference_queues_without_repo_and_resumes_saved_answer(store):
    task = InferenceTask(messages=[InferenceMessage(role="user", content="Summarize these facts")])
    wake = Mock()
    inference = JobInference(store.database, wake)
    with pytest.raises(InferencePending) as pending:
        await inference.infer(task)
    job = store.jobs.get_job(pending.value.job_id)
    assert job.flow_id is None and job.kind is JobKind.INFERENCE and job.attempt == 0
    assert wake.call_count == 1
    store.workers.worker_connected("plain", 19, "epoch")
    store.workers.worker_ready("plain", "epoch", InferenceCapability(adapter_version="codex/v1"))
    job = store.workers.assign_job("plain", "token", "epoch", slot="infer")
    request = job.inference.current_task
    response = InferenceResult(**result_header(request.request_id), method="worker.infer",
        job_id=job.id, assignment_token="token", output=InferenceOutput(
            request_id=request.request_id, request_digest=request.digest,
            text="An answer", adapter_version="codex/v1"))
    store.workers.record_assignment_result(job.id, "plain", "token", "epoch", response)
    application = ResultApplication(Mock(), Mock(), Mock(), database=store.database, effects=Mock())
    # Recovery applies saved data with no live worker or GitHub verification.
    await application.recover_pending_results()
    assert store.jobs.get_job(job.id).state is JobState.DONE
    assert (await JobInference(store.database, wake).infer(task)).text == "An answer"
    assert store.workers.get_worker_execution("plain") is None
    assert not application.effects.run.called


async def test_invalid_review_result_cannot_complete_job(store):
    task = InferenceTask(messages=[InferenceMessage(role="user", content="Judge this snapshot")],
                         output_schema={"type": "object"})
    job = store.jobs.enqueue_inference(task, validation="review")
    store.workers.worker_connected("plain", 19, "epoch")
    store.workers.worker_ready("plain", "epoch", InferenceCapability(adapter_version="codex/v1"))
    job = store.workers.assign_job("plain", "token", "epoch", slot="infer")
    request = job.inference.current_task
    response = InferenceResult(**result_header(request.request_id), method="worker.infer",
        job_id=job.id, assignment_token="token", output=InferenceOutput(
            request_id=request.request_id, request_digest=request.digest,
            text='{"verdict":"answer","answer":""}', adapter_version="codex/v1"))
    store.workers.record_assignment_result(job.id, "plain", "token", "epoch", response)
    application = ResultApplication(Mock(), Mock(), Mock(), database=store.database, effects=Mock())
    assert await application.apply(job.id) is JobState.FAILED
    current = store.jobs.get_job(job.id)
    assert current.inference.summary.last_failure_code == "invalid_response"
    assert current.attempt == 1


@pytest.mark.parametrize("change", ["head_sha", "base_sha"])
@pytest.mark.parametrize("verdict", [Verdict.ANSWER, Verdict.REVISE])
async def test_review_recomputes_after_pr_changes_without_replaying_old_result(
    store, review_flow, recording_resources, change, verdict,
):
    # A push or target-branch update after inference finishes must not relabel the
    # saved answer/instruction with the replacement snapshot's digest.
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = claim(store, clock)[0]
    github = RecordingGitHub()
    subject = coordinator(store, github)
    judge = ReviewJudge(JobInference(store.database, Mock(), flow_id=flow.id, batch_id=batch.id))
    store.workers.worker_connected("plain", 19, "epoch")
    store.workers.worker_ready("plain", "epoch", InferenceCapability(adapter_version="codex/v1"))

    async def finish_review(job_id, text):
        job = store.workers.assign_job("plain", f"token-{job_id}", "epoch", slot="infer")
        assert job.id == job_id
        request = job.inference.current_task
        decision = TriageVerdict(
            verdict=verdict, confidence=0.9,
            answer=text if verdict is Verdict.ANSWER else "",
            instruction=text if verdict is Verdict.REVISE else "",
        )
        response = InferenceResult(
            **result_header(request.request_id), method="worker.infer",
            job_id=job.id, assignment_token=job.assignment_token,
            output=InferenceOutput(request_id=request.request_id, request_digest=request.digest,
                text=decision.model_dump_json(), adapter_version="codex/v1"),
        )
        assert store.workers.record_assignment_result(
            job.id, "plain", job.assignment_token, "epoch", response,
        ) == "pending"
        assert await subject.results.apply(job.id) is JobState.DONE
        return decision

    with pytest.raises(InferencePending) as first:
        await subject.triage(batch, judge)
    old_context = store.reviews.get_batch(batch.id).context
    old_decision = await finish_review(first.value.job_id, "Old snapshot result")
    # Recreating the caller with unchanged evidence still reuses the durable answer.
    judge = ReviewJudge(JobInference(store.database, Mock(), flow_id=flow.id, batch_id=batch.id))
    assert (await judge.triage(old_context)).verdict == old_decision
    assert len(store.jobs.list_jobs(flow.id)) == 1

    github.pull_request = old_context.pull_request.model_copy(update={change: "changed"})
    await subject.triage(store.reviews.get_batch(batch.id), judge)
    assert store.reviews.get_batch(batch.id).context is None
    assert github.comments == []
    assert all(job.kind is JobKind.INFERENCE for job in store.jobs.list_jobs(flow.id))

    with pytest.raises(InferencePending) as replacement:
        await subject.triage(store.reviews.get_batch(batch.id), judge)
    assert replacement.value.job_id != first.value.job_id
    fresh = store.reviews.get_batch(batch.id)
    job = store.jobs.get_job(replacement.value.job_id)
    assert job.inference.context_digest == fresh.context.digest != old_context.digest
    assert job.attempt == 0
    assert github.comments == []
    assert all(job.kind is JobKind.INFERENCE for job in store.jobs.list_jobs(flow.id))

    new_decision = await finish_review(job.id, "New snapshot result")
    await subject.triage(store.reviews.get_batch(batch.id), judge)
    assert store.reviews.get_batch(batch.id).decision.verdict == new_decision
    if verdict is Verdict.ANSWER:
        assert len(github.comments) == 1
        assert github.comments[0].startswith("New snapshot result\n")
    else:
        revision, = [job for job in store.jobs.list_jobs(flow.id) if job.kind is JobKind.REVISION]
        assert revision.payload["instruction"] == "New snapshot result"
    assert store.jobs.get_job(first.value.job_id).state is JobState.DONE
