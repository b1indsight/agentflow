"""Pure lease decisions over locked facts; stores own fencing and atomic updates."""

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.domain.models import DeliveryState, FlowState, Job, JobKind
from app.github import GitHubError, retry_delay

RECOVERY_BLOCKED = "Flow blocked: original worker executor status is unknown"


class RecoveryAction(StrEnum):
    KEEP = "keep"
    REQUEUE = "requeue"
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
    has_unreclaimed_execution: bool = False
    has_unapplied_result: bool = False


def _preserve_delivery(flow_state: FlowState, delivery_state: DeliveryState | None) -> bool:
    return flow_state is not FlowState.ARCHIVED and delivery_state in {
        DeliveryState.CLOSED,
        DeliveryState.MERGED,
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
    return RecoveryAction.BLOCKED if may_have_executed else RecoveryAction.KEEP


def should_release_binding(binding: BindingRecoveryFacts, now: datetime) -> bool:
    """An applied result does not reclaim its workspace; retain it until explicit cleanup."""
    return (
        binding.recovery_deadline is not None
        and binding.recovery_deadline <= now
        and binding.flow_state is not FlowState.FINALIZING
        and not _preserve_delivery(binding.flow_state, binding.delivery_state)
        and not binding.has_pending_stop
        and not binding.has_unreclaimed_execution
        and not binding.has_unapplied_result
    )


class PreparationRetry(BaseModel):
    """Retry bookkeeping for one failed or expired preparation read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    failures: int
    first_failed_at: datetime
    # None: the budget or window is spent and the job ends as a control failure.
    next_at: datetime | None
    cause: str
    reason: str


def decide_preparation_retry(
    job: Job, error: GitHubError | None, *, now: datetime, max_failures: int, window_seconds: float,
) -> PreparationRetry:
    """Retry a transient read within its failure count and time window, else stop.

    A missing error means the persisted window expired before another request was
    issued. The window starts at the first failure, and a retry that would only be
    allowed after it closes is not scheduled.
    """
    failures = job.prepare_failures + (error is not None)
    first = job.prepare_first_failed_at or now
    next_at = now + timedelta(seconds=retry_delay(failures, now, error))
    permanent = error is not None and not error.retryable
    exhausted = (permanent or error is None or failures >= max_failures
                 or next_at >= first + timedelta(seconds=window_seconds))
    cause = str(error) if error else job.prepare_last_error or "preparation window expired"
    return PreparationRetry(
        failures=failures, first_failed_at=first, next_at=None if exhausted else next_at, cause=cause,
        reason=f"preparation retry budget/window exhausted: {cause}" if exhausted and not permanent else cause,
    )
