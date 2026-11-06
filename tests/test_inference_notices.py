"""Real routing, review state, PostgreSQL notices and the owner-only detail API."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from app.contracts.console import ConsoleFailure
from app.controler.api import create_app
from app.controler.runtime import ServerRuntime
from app.domain.inference_models import (
    InferenceError,
    InferenceFailure,
    InferenceOutput,
    InferenceSummary,
)
from app.domain.models import utcnow
from app.domain.review_models import BatchState
from app.inference.router import InferenceRouter, validate_output
from app.persistence.activity import ActivityStore
from app.persistence.inference_notices import InferenceNoticeStore
from tests.github import RecordingGitHub
from tests.test_console_api import (
    console_runtime as console_runtime,  # noqa: PLC0414 -- pytest fixture
)
from tests.test_coordinator import coordinator
from tests.test_review_batches import claim, feedback, ingest
from tests.test_review_batches import review_flow as review_flow  # noqa: PLC0414 -- pytest fixture


class Answers:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = []

    async def infer(self, task, *, validate=None, **selection):
        self.calls.append(task)
        failure = self.failures.pop(0) if self.failures else None
        if failure == "cancel":
            raise asyncio.CancelledError
        try:
            if failure and failure != "invalid_verdict":
                raise InferenceError(InferenceFailure(code=failure))
            output = InferenceOutput(request_id=task.request_id, request_digest=task.digest,
                text='{"verdict":"answer","answer":""}' if failure else '{"verdict":"ignore"}',
                adapter_version="amp/v1")
            validate_output(output, validate)
            return output
        except InferenceError as error:
            raise InferenceError(error.failure, worker_id="worker-a", backend="amp/v1") from None


def runtime_for(store, backend):
    runtime = ServerRuntime.__new__(ServerRuntime)
    runtime.settings = SimpleNamespace(judge_timeout_seconds=5)
    runtime.database, runtime.reviews = store.database, store.reviews
    runtime.coordinator = coordinator(store, RecordingGitHub())
    runtime.inference = InferenceRouter(local=None, workers=backend)
    runtime.inference_notices = InferenceNoticeStore(store.database)
    runtime.gateway = Mock()
    return runtime


@pytest.mark.parametrize("last,state", [("execution_timeout", BatchState.READY),
                                        ("authentication", BatchState.WAITING),
                                        ("invalid_verdict", BatchState.WAITING)])
async def test_exhaustion_persists_a_notice_without_jobs_or_github_effects(
    store, review_flow, recording_resources, console_runtime, last, state,
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = claim(store, clock)[0]
    backend = Answers(["execution_timeout", "execution_timeout", last])
    runtime = runtime_for(store, backend)
    await runtime._run_triage()
    assert len(backend.calls) == 3
    assert store.reviews.get_batch(batch.id).state == state
    assert not store.jobs.list_jobs(flow.id) and not runtime.coordinator.github.comments
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
        await runtime._run_triage()  # A later sweep can finish; the failure history remains.
        notice, = InferenceNoticeStore(store.database).page(flow.id).items
        assert notice.historical and store.reviews.get_batch(batch.id).state == BatchState.COMPLETED


@pytest.mark.parametrize("failures,cancel", [(["execution_timeout", "execution_timeout"], False),
                                           (["unavailable"], False), (["cancel"], True)])
async def test_no_false_notice_for_success_unavailable_or_cancellation(
    store, review_flow, recording_resources, failures, cancel,
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    claim(store, clock)
    runtime = runtime_for(store, Answers(failures))
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await runtime._run_triage()
    else:
        await runtime._run_triage()
    assert InferenceNoticeStore(store.database).page(flow.id).items == []


async def test_notice_write_failure_does_not_restart_inference(store, review_flow, recording_resources):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    claim(store, clock)
    backend = Answers(["execution_timeout"] * 3)
    runtime = runtime_for(store, backend)
    runtime.inference_notices = Mock(append=Mock(side_effect=RuntimeError("database unavailable")))
    await runtime._run_triage()
    assert len(backend.calls) == 3 and runtime.inference_notices.append.call_count == 1


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
