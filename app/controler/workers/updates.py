"""Connection-scoped Worker handoff; durable update intent remains on the Worker."""

from app.contracts.updates import MaintenanceReply, WorkerMaintenance
from app.persistence.workers import WorkerStore


class WorkerUpdateSession:
    """Fence this authenticated connection without a Controller-side update table."""

    def __init__(
        self,
        workers: WorkerStore,
        worker_id: str,
        epoch: str,
        pending: WorkerMaintenance | None = None,
    ) -> None:
        self.workers, self.worker_id, self.epoch = workers, worker_id, epoch
        self.pending = pending

    @property
    def paused(self) -> bool:
        return self.pending is not None and self.pending.action in {"pause", "commit", "resume"}

    def handle(self, message: WorkerMaintenance) -> MaintenanceReply:
        """A reconnect restores intent from the authenticated Worker's local operation."""
        reply = MaintenanceReply(
            request_id=message.request_id,
            operation_id=message.operation_id,
            connection_epoch=self.epoch,
            accepted=False,
            reason="maintenance ownership or state mismatch",
        )
        if message.connection_epoch != self.epoch or message.identity.worker_id != self.worker_id:
            return reply
        pending = self.pending
        if message.action == "pause" and not self.paused:
            pending = None
        if pending is not None and (
            pending.operation_id != message.operation_id or pending.release != message.release
        ):
            return reply
        idle = self.workers.idle_for_update(self.worker_id)
        if message.action == "pause":
            if pending is not None and (
                pending.action != "pause"
                or pending.instance_id != message.instance_id
                or pending.running_sha != message.running_sha
            ):
                return reply
        elif message.action == "ready":
            if message.running_sha != message.release.sha:
                return reply
            if pending is not None and (
                pending.action not in {"resume", "ready"}
                or pending.instance_id != message.instance_id
            ):
                return reply
        elif pending is None:
            return reply
        elif message.action in {"commit", "abort"}:
            if (
                pending.action not in {"pause", message.action}
                or pending.instance_id != message.instance_id
                or pending.running_sha != message.running_sha
            ):
                return reply
            if message.action == "commit" and (not idle or pending.identity != message.identity):
                return reply
        elif pending.action == "resume":
            if (
                pending.instance_id != message.instance_id
                or pending.running_sha != message.running_sha
                or pending.identity != message.identity
            ):
                return reply
        elif (
            pending.action != "commit"
            or not idle
            or pending.instance_id == message.instance_id
            or pending.connection_epoch == self.epoch
            or message.running_sha != message.release.sha
            or pending.identity != message.identity
        ):
            return reply
        self.pending = message
        return reply.model_copy(update={"accepted": True, "idle": idle, "reason": None})
