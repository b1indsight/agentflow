"""Controller inference uses durable jobs; pending calls never block later batches."""

from unittest.mock import Mock

import pytest

from app.contracts.protocol import InferenceResult, result_header
from app.controler.workflow.inference import InferencePending, JobInference
from app.controler.workflow.results import ResultApplication
from app.domain.inference_models import (
    InferenceCapability,
    InferenceMessage,
    InferenceOutput,
    InferenceTask,
)
from app.domain.models import JobKind, JobState


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
