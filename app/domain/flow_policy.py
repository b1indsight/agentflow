"""Pure Flow transitions; callers prove event eligibility under their existing locks."""

from enum import StrEnum

from app.domain.models import WORKING_STATES, ArchiveReason, FlowState


class FlowEvent(StrEnum):
    """Internal business facts, never arbitrary target states or persisted wire data."""

    CREATED = "created"
    JOB_ENQUEUED = "job_enqueued"
    JOB_CLAIMED = "job_claimed"
    ASSIGNMENT_REQUEUED = "assignment_requeued"
    EXECUTION_UNCERTAIN = "execution_uncertain"
    RESULT_FAILED = "result_failed"
    DELIVERY_BASELINE_REJECTED = "delivery_baseline_rejected"
    DELIVERY_APPLIED = "delivery_applied"
    FINALIZATION_REQUESTED = "finalization_requested"
    CLEANUP_APPLIED = "cleanup_applied"
    RETRY_REQUESTED = "retry_requested"
    ROUND_ARCHIVED = "round_archived"
    ROUND_RESUMED = "round_resumed"


def decide_flow_state(
    state: FlowState | None,
    event: FlowEvent,
    *,
    archive_reason: ArchiveReason | None,
) -> FlowState | None:
    """Return a state to write, None for a guarded no-op, or reject an illegal event.

    Returning the source state still requests a write. In particular, repeated
    failures retain their timestamp semantics, while an already-running claim
    must not refresh the Flow. Only creation accepts an absent source state.
    """
    if state is None:
        if event is FlowEvent.CREATED and archive_reason is None:
            return FlowState.QUEUED
        raise ValueError(f"Flow event {event} requires an existing Flow")
    match event:
        case FlowEvent.JOB_ENQUEUED:
            return (FlowState.QUEUED
                    if state is FlowState.REVIEW and archive_reason is None else None)
        case FlowEvent.JOB_CLAIMED:
            return (FlowState.RUNNING if state in {FlowState.QUEUED, FlowState.REVIEW}
                    and archive_reason is None else None)
        case FlowEvent.ASSIGNMENT_REQUEUED:
            return (FlowState.QUEUED
                    if state is FlowState.RUNNING and archive_reason is None else None)
        case FlowEvent.EXECUTION_UNCERTAIN:
            return FlowState.BLOCKED if state is not FlowState.ARCHIVED else None
        case FlowEvent.RESULT_FAILED:
            if state is not FlowState.ARCHIVED:
                return FlowState.BLOCKED
        case FlowEvent.DELIVERY_BASELINE_REJECTED | FlowEvent.DELIVERY_APPLIED:
            if state in WORKING_STATES | {FlowState.BLOCKED} and archive_reason is None:
                return (FlowState.REVIEW if event is FlowEvent.DELIVERY_APPLIED
                        else FlowState.BLOCKED)
        case FlowEvent.FINALIZATION_REQUESTED:
            if state is not FlowState.ARCHIVED and archive_reason is not None:
                return FlowState.FINALIZING
        case FlowEvent.CLEANUP_APPLIED:
            if state in {FlowState.BLOCKED, FlowState.FINALIZING} and archive_reason is not None:
                return FlowState.FINALIZING
        case FlowEvent.RETRY_REQUESTED:
            if state is FlowState.BLOCKED:
                return FlowState.FINALIZING if archive_reason is not None else FlowState.QUEUED
        case FlowEvent.ROUND_ARCHIVED:
            if state is FlowState.FINALIZING and archive_reason is not None:
                return FlowState.ARCHIVED
        case FlowEvent.ROUND_RESUMED:
            if state is FlowState.ARCHIVED:
                return FlowState.QUEUED
    raise ValueError(f"illegal Flow event {event} in {state}")
