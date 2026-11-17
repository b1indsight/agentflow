"""Pure inference-call decisions over locked facts; stores own locking, fencing and writes."""

from collections.abc import Sequence
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from app.domain.inference_models import (
    LOCAL_BUB_WORKER_ID,
    InferenceFailure,
    InferenceJob,
    InferenceSummary,
    InferenceTask,
    StopReason,
)
from app.domain.models import Flow, Job, JobKind, JobState, WorkerRecord
from app.domain.review_models import ReviewBatch

# Also frozen into the persisted InferenceSummary.max_attempts literal.
MAX_INFERENCE_ATTEMPTS = 3
_BACKOFF_CODES = frozenset({"rate_limited", "backend_unavailable"})
# The worker may still be executing: never offer it this call again.
_UNKNOWN_OUTCOME_CODES = frozenset({"busy", "connection_closed", "rpc_timeout"})


class InferenceSettlement(BaseModel):
    """What to persist for one settled attempt; ``failure.code`` becomes the job's last error."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: JobState
    call: InferenceJob
    failure: InferenceFailure | None


def decide_inference_cancellation(
    job: Job, *, flow: Flow | None, batch: ReviewBatch | None, batch_current: bool, now: datetime,
) -> InferenceFailure | None:
    """Only source currentness and the original deadline can retire an active call.

    ``flow`` is the row of ``job.flow_id`` and ``batch`` the row of the call's batch, each
    None when the call has no such source. ``batch_current`` is the review store's
    currentness check for that batch.
    """
    call = job.inference
    assert call is not None
    if call.cancel_failure is not None:
        return call.cancel_failure
    if job.flow_id is not None and (
        flow is None
        or not flow.accepts(JobKind.INFERENCE)
        or flow.current_attempt_id != call.source_attempt_id
    ):
        return InferenceFailure(code="cancelled")
    if call.batch_id is not None:
        assert batch is not None
        if (batch.inference_job_id != job.id or not batch_current or batch.decision is not None
                or (call.context_digest is not None
                    and (batch.context is None or batch.context.digest != call.context_digest))):
            return InferenceFailure(code="cancelled")
    # An on-time durable answer stays valid when application resumes after restart.
    received = job.result_received_at if job.state is JobState.RUNNING and job.result_applied_at is None else None
    if (received or now) >= call.deadline_at:
        return InferenceFailure(code="execution_timeout")
    return None


def decide_inference_settlement(
    job: Job, failure: InferenceFailure | None, *, cancellation: InferenceFailure | None, now: datetime,
) -> InferenceSettlement:
    """Finish, fail or requeue one attempt; cancellation overrides the reported outcome.

    A recoverable failure retries until the attempt budget or the original deadline is
    spent. Only a running attempt adds to the failure history.
    """
    call = job.inference
    assert call is not None
    failure = cancellation or failure
    if failure is None:
        return InferenceSettlement(state=JobState.DONE, call=call, failure=None)
    delay = failure.retry_after_seconds
    if delay is None:
        delay = 0.25 * job.attempt if failure.code in _BACKOFF_CODES else 0
    # A provider may suggest any finite delay; compare before constructing a
    # datetime so a huge Retry-After cannot strand the durable inbox forever.
    next_at = (call.deadline_at if delay >= (call.deadline_at - now).total_seconds()
               else now + timedelta(seconds=delay))
    state = JobState.FAILED
    reason: StopReason = "non_retryable"
    if failure.recoverable:
        if cancellation is not None or next_at >= call.deadline_at:
            reason = "deadline"
        elif job.attempt >= MAX_INFERENCE_ATTEMPTS:
            reason = "max_attempts"
        else:
            state = JobState.QUEUED
    worker_ids = frozenset({job.assigned_worker_id}) if job.assigned_worker_id else frozenset()
    call = call.model_copy(update={
        "failures": [*call.failures, failure] if job.state is JobState.RUNNING else call.failures,
        "avoid_workers": call.avoid_workers | worker_ids,
        "excluded_workers": call.excluded_workers | (
            worker_ids if failure.code in _UNKNOWN_OUTCOME_CODES else frozenset()),
        "next_attempt_at": next_at if state is JobState.QUEUED else None,
        "cancel_failure": cancellation,
    })
    if state is JobState.FAILED:
        call = call.model_copy(update={"summary": InferenceSummary(
            logical_call_id=call.task.request_id, attempts=job.attempt,
            stop_reason=reason, last_failure_code=failure.code,
            last_worker_id=job.assigned_worker_id, backend=call.backend,
            finished_at=now, elapsed_ms=max(0, int((now - job.created_at).total_seconds() * 1000)),
        )})
    return InferenceSettlement(state=state, call=call, failure=failure)


def plan_inference_attempt(
    call: InferenceJob, *, request_id: str, now: datetime,
) -> InferenceTask | InferenceFailure | None:
    """Return this dispatch's task, a timeout once the deadline passed, or None while backing off.

    Each dispatch gets its own request identity and only the budget that remains.
    """
    if call.next_attempt_at is not None and call.next_attempt_at > now:
        return None
    if call.deadline_at <= now:
        return InferenceFailure(code="execution_timeout")
    return call.task.model_copy(update={
        "request_id": request_id,
        "timeout_seconds": (call.deadline_at - now).total_seconds(),
    })


def choose_inference_worker(
    call: InferenceJob, task: InferenceTask, available: Sequence[WorkerRecord],
) -> WorkerRecord | None:
    """Pick the preferred ready worker; the caller claims only when it is that worker.

    ``available`` keeps the store's fair base order (least recently assigned first).
    Excluded and unsupported workers never qualify. Workers that already failed this
    call are avoided while another is ready; within a tier the local bub wins.
    """
    candidates = [worker for worker in available if worker.worker_id not in call.excluded_workers
                  and worker.inference is not None and worker.inference.supports(task)]
    return min(candidates, default=None, key=lambda worker: (
        worker.worker_id in call.avoid_workers, worker.worker_id != LOCAL_BUB_WORKER_ID))
