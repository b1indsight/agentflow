"""Pure Flow transitions; callers prove event eligibility under their existing locks."""

import hashlib
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.domain.models import WORKING_STATES, ArchiveReason, Flow, FlowState


class FlowEvent(StrEnum):
    """Internal business facts, never arbitrary target states.

    Values are persisted as Flow ledger kinds (see ``fact_kind``); never rename one.
    """

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
    ATTEMPT_ARCHIVED = "attempt_archived"
    ATTEMPT_STARTED = "attempt_started"


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
        case FlowEvent.ATTEMPT_ARCHIVED:
            if state is FlowState.FINALIZING and archive_reason is not None:
                return FlowState.ARCHIVED
        case FlowEvent.ATTEMPT_STARTED:
            if state is FlowState.ARCHIVED:
                return FlowState.QUEUED
    raise ValueError(f"illegal Flow event {event} in {state}")


# The selected Attempt is reconstructed from the creation/start facts in version order.
LEDGER_COLUMNS = (
    "repo", "issue_number", "task_content", "installation_id", "title", "state",
    "current_attempt_id", "archive_reason", "archive_summary", "archived_at",
    "created_at", "updated_at",
)
ORIGIN_KINDS = frozenset({"created", "snapshot"})


class FlowFact(BaseModel):
    """One immutable Flow ledger row, as read back for replay."""

    model_config = ConfigDict(frozen=True)

    kind: str
    attempt_id: int
    occurred_at: datetime
    metadata: dict[str, str | int | bool | None]


def task_digest(task_content: str | None) -> str | None:
    """Identify direct task text; the text itself stays out of activity bodies."""
    return hashlib.sha256(task_content.encode()).hexdigest() if task_content is not None else None


def fact_kind(event: FlowEvent) -> str:
    """The ledger kind recording an event; the archive evidence row records archival."""
    return "archive" if event is FlowEvent.ATTEMPT_ARCHIVED else event.value


def replay_flow(flow_id: int, facts: Sequence[FlowFact], *, task_content: str | None) -> Flow:
    """Rebuild ledger-owned columns from the last origin fact, in version order.

    The origin records only a digest of direct task text, so the caller supplies the
    stored text and replay rejects it unless the digest matches. Every fact after
    the origin must be a legal, effective event; a guarded no-op or an illegal
    event means the ledger and the policy disagree.
    """
    starts = [index for index, fact in enumerate(facts) if fact.kind in ORIGIN_KINDS]
    if not starts:
        raise ValueError(f"Flow {flow_id} ledger has no origin fact")
    origin = facts[starts[-1]]
    if origin.metadata["task_digest"] != task_digest(task_content):
        raise ValueError(f"Flow {flow_id} task content does not match its ledger digest")
    # A snapshot carries every ledger column; creation carries only the inputs.
    columns: dict[str, object] = (
        {**origin.metadata, "state": FlowState.QUEUED, "current_attempt_id": origin.attempt_id,
         "created_at": origin.occurred_at, "updated_at": origin.occurred_at}
        if origin.kind == "created" else dict(origin.metadata)
    )
    columns.pop("task_digest")
    flow = Flow.model_validate({**columns, "id": flow_id, "task_content": task_content})
    if flow.current_attempt_id != origin.attempt_id:
        raise ValueError("snapshot ownership differs from its Attempt")
    selected = {origin.attempt_id}
    for fact in facts[starts[-1] + 1:]:
        if fact.kind == FlowEvent.ATTEMPT_STARTED:
            if fact.attempt_id in selected:
                raise ValueError("start fact reuses an earlier Attempt")
            selected.add(fact.attempt_id)
        flow = _apply(flow, fact)
    return flow


def _apply(flow: Flow, fact: FlowFact) -> Flow:
    match fact.kind:
        case "merge":
            return flow
        case "titled":
            if fact.attempt_id != flow.current_attempt_id:
                raise ValueError("title belongs to a superseded Attempt")
            return flow.model_copy(update={"title": fact.metadata["title"]})
        case "archive_correction":
            # Mirrors observe_archived_merge: only the still-current archived Attempt changes.
            if fact.attempt_id == flow.current_attempt_id and flow.state is FlowState.ARCHIVED:
                return flow.model_copy(update={"archive_reason": ArchiveReason.SUCCEEDED})
            return flow
    event = FlowEvent.ATTEMPT_ARCHIVED if fact.kind == "archive" else FlowEvent(fact.kind)
    resumed = event is FlowEvent.ATTEMPT_STARTED
    if not resumed and fact.attempt_id != flow.current_attempt_id:
        raise ValueError(f"{fact.kind} fact has unexpected Attempt {fact.attempt_id}")
    reason_value = fact.metadata.get("archive_reason")
    reason = ArchiveReason(reason_value) if isinstance(reason_value, str) else None
    state = decide_flow_state(
        flow.state, event, archive_reason=flow.archive_reason if resumed else reason
    )
    if state is None or fact.metadata.get("state", state) != state:
        raise ValueError(f"{fact.kind} fact is not effective from {flow.state}")
    if resumed:
        return flow.model_copy(update={
            "state": state, "current_attempt_id": fact.attempt_id, "archive_reason": None,
            "archive_summary": None, "archived_at": None, "updated_at": fact.occurred_at,
        })
    return flow.model_copy(update={
        "state": state, "archive_reason": reason,
        "archive_summary": fact.metadata.get("archive_summary"),
        "archived_at": fact.occurred_at if event is FlowEvent.ATTEMPT_ARCHIVED else flow.archived_at,
        "updated_at": fact.occurred_at,
    })
