"""Explicit worker dependencies for in-memory protocol tests."""

from app.contracts.protocol import ExecutionStopped
from app.domain.models import WorkerRecord, WorkerStatus, utcnow
from app.persistence.stop_store import StopIntent


class NoPendingStops:
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
