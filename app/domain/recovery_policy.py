"""Pure lease decisions over locked facts; stores own fencing and atomic updates."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.domain.models import DeliveryState, FlowState, JobKind

RECOVERY_EXPIRED = "worker recovery deadline expired"
RECOVERY_BLOCKED = "archive blocked: original worker executor status is unknown"


class RecoveryAction(StrEnum):
    KEEP = "keep"
    REQUEUE = "requeue"
    FAIL = "fail"
    BLOCKED = "blocked"


class JobRecoveryFacts(BaseModel):
    """A running job and its related facts read under the sweeper's worker locks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    flow_id: int
    kind: JobKind
    flow_state: FlowState
    delivery_state: DeliveryState | None
    lease_expires_at: datetime | None
    recovery_deadline: datetime | None
    started_at: datetime | None = None
    dispatch_authorized_at: datetime | None = None
    result_received_at: datetime | None = None
    archive_frozen: bool = False
    has_pending_stop: bool = False


class BindingRecoveryFacts(BaseModel):
    """An unreleased binding, read after this sweep's job transitions are applied."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flow_id: int
    worker_id: str
    flow_state: FlowState
    delivery_state: DeliveryState | None
    recovery_deadline: datetime | None
    has_pending_stop: bool = False
    has_unstarted_dispatch: bool = False
    has_unapplied_result: bool = False


def _preserve_delivery(flow_state: FlowState, delivery_state: DeliveryState | None) -> bool:
    return flow_state is FlowState.ACTIVE and delivery_state in {
        DeliveryState.CLOSED, DeliveryState.MERGED,
    }


def decide_job_recovery(job: JobRecoveryFacts, now: datetime) -> RecoveryAction:
    """Requeue only proven unstarted work; unknown execution retains ownership.

    A closed/merged active Flow keeps its diagnostic even with an outstanding
    stop. Neither that diagnostic nor timeout authorizes releasing its binding.
    """
    if job.result_received_at is not None:
        return RecoveryAction.KEEP
    # SQL dispatch/reconnect paths use Store's _MAY_HAVE_EXECUTED for the same proof.
    may_have_executed = (job.started_at is not None or job.dispatch_authorized_at is not None
                         or (job.kind is JobKind.ARCHIVE and job.archive_frozen))
    if (not may_have_executed and not job.has_pending_stop
            and job.lease_expires_at is not None and job.lease_expires_at <= now):
        return RecoveryAction.REQUEUE
    if job.recovery_deadline is None or job.recovery_deadline > now:
        return RecoveryAction.KEEP
    if _preserve_delivery(job.flow_state, job.delivery_state):
        return RecoveryAction.BLOCKED if may_have_executed else RecoveryAction.KEEP
    if job.started_at is not None and not job.has_pending_stop:
        return RecoveryAction.FAIL
    return RecoveryAction.KEEP


def should_release_binding(binding: BindingRecoveryFacts, now: datetime) -> bool:
    """Release expired idle ownership only after stop, dispatch and result fences converge."""
    return (
        binding.recovery_deadline is not None and binding.recovery_deadline <= now
        and not _preserve_delivery(binding.flow_state, binding.delivery_state)
        and not binding.has_pending_stop
        and not binding.has_unstarted_dispatch
        and not binding.has_unapplied_result
    )
