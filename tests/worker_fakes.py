"""Explicit worker dependencies for in-memory protocol tests."""

from collections.abc import Awaitable, Callable
from unittest.mock import Mock

from app.contracts.protocol import ExecutionStopped
from app.controler.workflow.results import ResultApplication
from app.domain.models import JobState, WorkerRecord, WorkerStatus, utcnow
from app.persistence.stop_store import StopIntent, StopStore
from app.persistence.store import Store


class NoPendingStops(StopStore):
    def pending(self, worker_id: str) -> StopIntent | None:
        return None

    def receipt_request(self, worker_id: str, request_id: str) -> StopIntent | None:
        return None

    def authorize_dispatch(self, job_id: int, worker_id: str, token: str, epoch: str) -> bool:
        return True

    def confirm(self, worker_id: str, receipt: ExecutionStopped) -> bool:
        raise AssertionError("no stop was requested")


def connected_worker(worker_id: str, protocol_version: int, connection_epoch: str) -> WorkerRecord:
    now = utcnow()
    return WorkerRecord(worker_id=worker_id, protocol_version=protocol_version,
                        connection_epoch=connection_epoch, status=WorkerStatus.ONLINE,
                        first_connected_at=now, last_connected_at=now, last_seen_at=now)



def result_application(store: Store, commit_result: Callable[[int], Awaitable[JobState]]) -> ResultApplication:
    """Exercise result locking and recovery around the supplied commit callback."""
    results = ResultApplication(store, Mock(), Mock(), Mock())
    results._apply = commit_result  # type: ignore[method-assign, assignment]
    return results
