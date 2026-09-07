"""Versioned review inputs and durable batches shared by control and worker contracts."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

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
