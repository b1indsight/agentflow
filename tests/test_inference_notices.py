"""Inference jobs, review state, PostgreSQL notices and the owner-only detail API."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from app.contracts.console import ConsoleFailure
from app.contracts.protocol import InferenceResult, result_header
from app.controler.api import create_app
from app.controler.runtime import ServerRuntime
from app.domain.inference_models import (
    InferenceCapability,
    InferenceFailure,
    InferenceOutput,
    InferenceSummary,
)
from app.domain.models import JobKind, JobState, utcnow
from app.domain.review_models import BatchState
from app.persistence.activity import ActivityStore
from app.persistence.inference_notices import InferenceNoticeStore
from tests.github import RecordingGitHub
from tests.test_console_api import (
    console_runtime as console_runtime,  # noqa: PLC0414 -- pytest fixture
)
from tests.test_coordinator import coordinator
from tests.test_review_batches import claim, feedback, ingest
from tests.test_review_batches import review_flow as review_flow  # noqa: PLC0414 -- pytest fixture


def runtime_for(store):
    runtime = ServerRuntime.__new__(ServerRuntime)
    runtime.settings = SimpleNamespace(judge_timeout_seconds=5)
    runtime.database, runtime.reviews = store.database, store.reviews
    runtime.coordinator = coordinator(store, RecordingGitHub())
    runtime.results = runtime.coordinator.results
    runtime.gateway = Mock()
    store.workers.worker_connected("worker-a", 19, "epoch")
    store.workers.worker_ready("worker-a", "epoch", InferenceCapability(adapter_version="amp/v1"))
    return runtime


async def finish_attempt(store, runtime, failure=None):
    job = store.workers.assign_job("worker-a", uuid4().hex, "epoch", slot="infer")
    assert job is not None
    task = job.inference.current_task
    output = None if failure and failure != "invalid_verdict" else InferenceOutput(
        request_id=task.request_id, request_digest=task.digest,
        text='{"verdict":"answer","answer":""}' if failure else '{"verdict":"ignore"}',
        adapter_version="amp/v1")
    response = InferenceResult(**result_header(task.request_id), method="worker.infer",
        job_id=job.id, assignment_token=job.assignment_token, output=output,
        failure=InferenceFailure(code=failure) if output is None else None)
    store.workers.record_assignment_result(job.id, "worker-a", job.assignment_token, "epoch", response)
    await runtime.results.apply(job.id)
    return job.id


@pytest.mark.parametrize("last,state", [("execution_timeout", BatchState.READY),
                                        ("authentication", BatchState.WAITING),
                                        ("invalid_verdict", BatchState.WAITING)])
async def test_exhaustion_persists_a_notice_without_coding_or_github_effects(
    store, review_flow, recording_resources, console_runtime, last, state,
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = claim(store, clock)[0]
    runtime = runtime_for(store)
    await runtime._run_triage()
    for failure in ["execution_timeout", "execution_timeout", last]:
        await finish_attempt(store, runtime, failure)
    await runtime._run_triage()
    assert store.reviews.get_batch(batch.id).state == state
    job, = store.jobs.list_jobs(flow.id)
    assert job.kind is JobKind.INFERENCE and job.attempt == 3
    assert not runtime.coordinator.github.comments
    # A new reader represents restart/refresh; no ephemeral runtime state is needed.
    with TestClient(create_app(console_runtime)) as client:
        path = f"/api/flows/{flow.id}/inference-notices"
        assert client.get(path).status_code == 401
        response = client.get(path, headers=console_runtime.issuer.headers())
        assert response.status_code == 200
        notice, = response.json()["items"]
        assert notice["key"] == f"inference-exhausted:{batch.id}"
        assert notice["attempts"] == 3
        assert notice["last_failure_code"] == ("invalid_response" if last == "invalid_verdict" else last)
        assert not notice["historical"]
        assert ("后续扫描" if state == BatchState.READY else "新的有效 PR 反馈") in notice["next_condition"]
        assert "logical_call_id" not in notice and "messages" not in response.text
    assert not ActivityStore(store.database).page(flow.id).items
    if state == BatchState.READY:
        await runtime._run_triage()  # A later sweep creates a new logical call, not attempt four.
        await finish_attempt(store, runtime)
        await runtime._run_triage()
        notice, = InferenceNoticeStore(store.database).page(flow.id).items
        assert notice.historical and store.reviews.get_batch(batch.id).state == BatchState.COMPLETED
        assert [j.attempt for j in store.jobs.list_jobs(flow.id)] == [3, 1]


@pytest.mark.parametrize("outcome", ["success", "unavailable", "cancelled"])
async def test_no_false_notice_for_success_unavailable_or_cancellation(
    store, review_flow, recording_resources, outcome,
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    claim(store, clock)
    runtime = runtime_for(store)
    await runtime._run_triage()
    if outcome != "unavailable":
        await finish_attempt(store, runtime, None if outcome == "success" else outcome)
        await runtime._run_triage()
    assert InferenceNoticeStore(store.database).page(flow.id).items == []


async def test_notice_write_failure_does_not_restart_inference(
    store, review_flow, recording_resources, monkeypatch,
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    claim(store, clock)
    runtime = runtime_for(store)
    await runtime._run_triage()
    await finish_attempt(store, runtime, "execution_timeout")
    await finish_attempt(store, runtime, "execution_timeout")
    with monkeypatch.context() as patch:
        patch.setattr(InferenceNoticeStore, "append", Mock(side_effect=RuntimeError("database unavailable")))
        with pytest.raises(RuntimeError, match="database unavailable"):
            await finish_attempt(store, runtime, "execution_timeout")
        await runtime._run_triage()
        job, = store.jobs.list_jobs(flow.id)
        assert job.attempt == 3 and job.state is JobState.RUNNING
        assert job.result_received_at is not None and job.result_applied_at is None
    await runtime.results.recover_pending_results()
    await runtime._run_triage()
    assert len(store.jobs.list_jobs(flow.id)) == 1
    assert len(InferenceNoticeStore(store.database).page(flow.id).items) == 1


def test_notice_idempotency_snapshot_paging_and_flow_scope(store, console_runtime):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    other = store.flows.create_flow("owner/other", 8, 42)
    notices = InferenceNoticeStore(store.database)
    first = InferenceSummary(logical_call_id=uuid4().hex, attempts=3, stop_reason="max_attempts",
                             last_failure_code="execution_timeout", finished_at=utcnow(), elapsed_ms=5)
    # Concurrent persistence of the same finished call must publish only one version.
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: notices.append(flow.id, 1, first), range(2))) == 1
    second = first.model_copy(update={"logical_call_id": uuid4().hex,
                                     "finished_at": first.finished_at + timedelta(seconds=1)})
    notices.append(flow.id, 2, second)
    page = notices.page(flow.id, limit=1)
    assert page.items[0].batch_id == 2 and page.has_more
    notices.append(flow.id, 1, first.model_copy(update={"logical_call_id": uuid4().hex,
        "finished_at": first.finished_at + timedelta(seconds=2), "attempts": 1, "stop_reason": "deadline"}))
    rest = notices.page(flow.id, cursor=page.next_cursor, snapshot=page.snapshot, limit=1)
    assert rest.items[0].batch_id == 1 and rest.items[0].attempts == 3
    assert not rest.has_more
    assert notices.page(flow.id).items[0].attempts == 1
    with pytest.raises(ConsoleFailure, match="cursor_invalid"):
        notices.page(other.id, cursor=page.next_cursor)
    with TestClient(create_app(console_runtime)) as client:
        headers = console_runtime.issuer.headers()
        path = f"/api/flows/{flow.id}/inference-notices"
        assert client.get(path, params={"limit": 101}, headers=headers).status_code == 422
        assert client.get(path, params={"cursor": page.next_cursor, "snapshot": 0}, headers=headers).status_code == 409
        assert client.get(f"/api/flows/{flow.id}/activity", params={"cursor": page.next_cursor},
                          headers=headers).status_code == 409
        assert client.get("/api/flows/999999/inference-notices", headers=headers).status_code == 404
