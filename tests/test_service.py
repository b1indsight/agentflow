from typing import Any, cast

from app.control.service import ControlService
from app.models import Flow, FlowState, Job, JobKind, JobState
from app.store import Store


class RetryStore:
    def __init__(self, failed_job: Job | None = None) -> None:
        self.flow = Flow(
            id=1,
            repo="owner/repo",
            issue_number=8,
            installation_id=42,
            state=FlowState.FAILED,
        )
        self.enqueued: list[JobKind] = []
        self.failed_job = failed_job

    def get_active_flow(self, repo: str, issue_number: int) -> Flow | None:
        return self.flow if self.flow.state not in {
            FlowState.FAILED,
            FlowState.CANCELLED,
            FlowState.ARCHIVED,
        } else None

    def update_flow(self, flow: Flow) -> None:
        self.flow = flow

    def latest_failed_job(self, flow_id: int) -> Job | None:
        return self.failed_job

    def enqueue_job(
        self, flow_id: int, kind: JobKind, payload: dict | None = None
    ) -> Job:
        self.enqueued.append(kind)
        return Job(id=2, flow_id=flow_id, kind=kind, state=JobState.QUEUED)


def test_explicit_retry_reactivates_a_failed_flow() -> None:
    store = RetryStore()
    service = ControlService(cast(Store, cast(Any, store)))

    service.retry(store.flow)

    assert store.flow.state is FlowState.PENDING
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

    assert store.flow.state is FlowState.PR_OPEN
    assert store.enqueued == [JobKind.REVISION]
