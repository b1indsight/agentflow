"""Versioned review inputs and durable batches shared by control and worker contracts."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.context_models import ReviewContext
from app.github_models import ReviewThreads
from app.judge_models import JudgeDecision
from app.models import FeedbackRef, ReviewItemKind


class FeedbackUpdate(BaseModel):
    """A GitHub object version; routing, never model output, supplies the authorized signal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ReviewItemKind
    github_id: int = Field(gt=0)
    author: str
    body: str
    updated_at: AwareDatetime
    deleted: bool = False
    # Keep legacy command values readable in persisted v5 snapshots; routing no longer emits them.
    signal: Literal["discussion", "command", "request_changes"] = "discussion"
    submitted: bool = False
    path: str | None = None
    original_commit_id: str | None = None
    diff_hunk: str | None = None


class ReviewFeedback(FeedbackUpdate, FeedbackRef):
    """Frozen content and identity of exactly one version sent to a worker."""


class BatchState(StrEnum):
    READY = "ready"
    PROCESSING = "processing"
    WAITING = "waiting"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ReviewBatch(BaseModel):
    """Frozen feedback and unresolved questions shared by every stage of a review batch."""

    id: int
    flow_id: int
    attempt_id: int
    generation: int
    state: BatchState
    trigger: Literal["discussion", "strong"]
    job_id: int | None
    feedback: list[ReviewFeedback]
    open_points: list[str]
    created_at: datetime
    context: ReviewContext | None = None
    threads: ReviewThreads | None = None
    context_stale: bool = False
    decision: JudgeDecision | None = None

    @property
    def discussion(self) -> str:
        """Render frozen feedback for an authorized direct revision."""
        text = "\n".join(f"{item.author}: {item.body}" for item in self.feedback)
        if self.open_points:
            text += "\nUnresolved questions:\n" + "\n".join(self.open_points)
        return text
