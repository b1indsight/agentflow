from contextlib import contextmanager
from typing import Any, cast

import pytest

from app.control.service import ControlService
from app.models import (
    DeliveryState,
    Flow,
    FlowDelivery,
    FlowState,
    Job,
    JobKind,
    JobState,
    WorkerRecord,
    WorkerStatus,
    utcnow,
)
from app.store import Store
from tests.result_memory import MemoryStops
from tests.review_memory import MemoryReviews


class RetryStore:
    def __init__(self, failed_job: Job | None = None) -> None:
        self.reviews = MemoryReviews()
        self.stops = MemoryStops()
        self.flow = Flow(
            id=1,
            repo="owner/repo",
            issue_number=8,
            installation_id=42,
            state=FlowState.FAILED,
        )
        self.enqueued: list[JobKind] = []
        self.failed_job = failed_job
        self.delivery: FlowDelivery | None = None

    @contextmanager
    def transaction(self):
        flow, enqueued = self.flow.model_copy(deep=True), list(self.enqueued)
        try:
            yield self
        except Exception:
            self.flow, self.enqueued = flow, enqueued
            raise

    def serialize_issue(self, repo: str, issue_number: int) -> None:
        pass

    def lock_workflow(self, flow_id: int) -> Flow:
        return self.flow.model_copy(deep=True)

    def has_pending_result(self, flow_id: int) -> bool:
        return False

    def get_delivery(self, flow_id: int) -> FlowDelivery | None:
        return self.delivery

    def get_active_flow(self, repo: str, issue_number: int) -> Flow | None:
        return (
            self.flow
            if self.flow.state
            not in {
                FlowState.FAILED,
                FlowState.CANCELLED,
                FlowState.SUCCEEDED,
            }
            else None
        )

    def update_flow(self, flow: Flow) -> None:
        self.flow = flow

    def latest_failed_job(self, flow_id: int) -> Job | None:
        return self.failed_job

    def cancel_jobs(self, flow_id: int) -> None:
        self.reviews.cancel(flow_id)

    def enqueue_job(self, flow_id: int, kind: JobKind, payload: dict | None = None) -> Job:
        self.enqueued.append(kind)
        return Job(id=2, flow_id=flow_id, kind=kind, state=JobState.QUEUED)


def test_explicit_retry_reactivates_a_failed_flow() -> None:
    store = RetryStore()
    service = ControlService(cast(Store, cast(Any, store)))

    service.retry(store.flow)

    assert store.flow.state is FlowState.ACTIVE
    assert store.enqueued == [JobKind.INITIAL]


def test_explicit_retry_requeues_the_failed_job_kind() -> None:
    failed_job = Job(
        id=3,
        flow_id=1,
        kind=JobKind.REVISION,
        state=JobState.FAILED,
        payload={"instruction": "Address feedback"},
    )
    store = RetryStore(failed_job)
    service = ControlService(cast(Store, cast(Any, store)))

    service.retry(store.flow)

    assert store.flow.state is FlowState.ACTIVE
    assert store.enqueued == [JobKind.REVISION]


def test_merged_terminal_flow_requires_manual_archive_reconciliation() -> None:
    store = RetryStore(Job(id=3, flow_id=1, kind=JobKind.ARCHIVE, state=JobState.FAILED))
    store.delivery = FlowDelivery(flow_id=1, branch="flow/1", state=DeliveryState.MERGED)

    with pytest.raises(ValueError, match="terminal"):
        ControlService(cast(Store, cast(Any, store))).retry(store.flow)

    assert store.flow.state is FlowState.FAILED
    assert store.enqueued == []


def test_retry_rolls_back_state_when_enqueue_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # A control crash or database error between activation and enqueue must leave retry possible.
    store = RetryStore()
    service = ControlService(cast(Store, cast(Any, store)))

    def fail_enqueue(*args: object, **kwargs: object) -> Job:
        raise RuntimeError("enqueue interrupted")

    monkeypatch.setattr(store, "enqueue_job", fail_enqueue)
    with pytest.raises(RuntimeError, match="enqueue interrupted"):
        service.retry(store.flow)

    assert store.flow.state is FlowState.FAILED
    assert store.enqueued == []


def test_list_workers_aggregates_current_flow() -> None:
    now = utcnow()
    worker = WorkerRecord(
        worker_id="worker-a",
        protocol_version=1,
        status=WorkerStatus.BUSY,
        current_job_id=9,
        first_connected_at=now,
        last_connected_at=now,
        last_seen_at=now,
    )
    job = Job(id=9, flow_id=3, kind=JobKind.INITIAL, state=JobState.RUNNING)
    store = SimpleWorkerStore(worker, job)

    views = ControlService(cast(Store, cast(Any, store))).list_workers()

    assert views[0].worker_id == "worker-a"
    assert views[0].current_job_id == 9
    assert views[0].current_flow_id == 3


class SimpleWorkerStore:
    def __init__(self, worker: WorkerRecord, job: Job) -> None:
        self.worker = worker
        self.job = job

    def list_workers(self) -> list[WorkerRecord]:
        return [self.worker]

    def get_job(self, job_id: int) -> Job | None:
        return self.job if job_id == self.job.id else None

    def get_worker_execution(self, worker_id: str):
        return None
