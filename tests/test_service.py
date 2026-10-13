from contextlib import contextmanager
from typing import Any, cast

import pytest

from app.controler.workflow.service import ControlService
from app.domain.models import (
    DeliveryState,
    Flow,
    FlowDelivery,
    FlowState,
    Job,
    JobKind,
    JobState,
)
from app.persistence.store import Store
from tests.persistence_fakes import RepositoryViews
from tests.result_memory import MemoryResults, MemoryStops
from tests.review_memory import MemoryReviews


class RetryStore(RepositoryViews):
    def __init__(self, failed_job: Job | None = None) -> None:
        self.results = MemoryResults()
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
        yield self

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


@pytest.mark.parametrize("failed_kind,expected_kind", [
    (None, JobKind.INITIAL),
    (JobKind.REVISION, JobKind.REVISION),
])
def test_explicit_retry_reactivates_flow_and_requeues_kind(failed_kind, expected_kind) -> None:
    failed_job = (Job(id=3, flow_id=1, kind=failed_kind, state=JobState.FAILED,
                      payload={"instruction": "Address feedback"})
                  if failed_kind is not None else None)
    store = RetryStore(failed_job)
    service = ControlService(
        database=cast(Store, cast(Any, store)).database,
        deliveries=cast(Store, cast(Any, store)).deliveries,
        flows=cast(Store, cast(Any, store)).flows,
        jobs=cast(Store, cast(Any, store)).jobs,
        result_store=cast(Store, cast(Any, store)).results,
        reviews=cast(Store, cast(Any, store)).reviews,
        stops=cast(Store, cast(Any, store)).stops,
        webhook_store=cast(Store, cast(Any, store)).webhooks,
        workers=cast(Store, cast(Any, store)).workers,
    )

    service.retry(store.flow)

    assert store.flow.state is FlowState.ACTIVE
    assert store.enqueued == [expected_kind]


def test_merged_terminal_flow_requires_manual_archive_reconciliation() -> None:
    store = RetryStore(Job(id=3, flow_id=1, kind=JobKind.ARCHIVE, state=JobState.FAILED))
    store.delivery = FlowDelivery(flow_id=1, branch="flow/1", state=DeliveryState.MERGED)

    with pytest.raises(ValueError, match="terminal"):
        ControlService(
            database=cast(Store, cast(Any, store)).database,
            deliveries=cast(Store, cast(Any, store)).deliveries,
            flows=cast(Store, cast(Any, store)).flows,
            jobs=cast(Store, cast(Any, store)).jobs,
            result_store=cast(Store, cast(Any, store)).results,
            reviews=cast(Store, cast(Any, store)).reviews,
            stops=cast(Store, cast(Any, store)).stops,
            webhook_store=cast(Store, cast(Any, store)).webhooks,
            workers=cast(Store, cast(Any, store)).workers,
        ).retry(store.flow)

    assert store.flow.state is FlowState.FAILED
    assert store.enqueued == []
