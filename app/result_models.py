"""Frozen result applications and the finite set of recoverable GitHub operations."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.github_models import PullRequestSnapshot
from app.models import FeedbackRef, FlowDelivery, FlowState, JobKind, JobState
from app.review_models import BatchState


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EffectSpec(FrozenModel):
    key: str
    installation_id: int
    repo: str


class CommentEffect(EffectSpec):
    kind: Literal["comment"] = "comment"
    number: int
    body: str


class PullRequestEffect(EffectSpec):
    kind: Literal["pull_request"] = "pull_request"
    branch: str
    base_branch: str
    head_sha: str
    title: str
    body: str


class LabelsEffect(EffectSpec):
    kind: Literal["labels"] = "labels"
    # INITIAL resolves this explicit dependency from its preceding PR receipt.
    number: int | Literal["created_pr"]
    labels: list[str]


class ReviewersEffect(EffectSpec):
    kind: Literal["reviewers"] = "reviewers"
    number: int | Literal["created_pr"]
    reviewers: list[str]


class ResolveEffect(EffectSpec):
    kind: Literal["resolve"] = "resolve"
    thread_id: str


Effect = Annotated[
    CommentEffect | PullRequestEffect | LabelsEffect | ReviewersEffect | ResolveEffect,
    Field(discriminator="kind"),
]


class DerivedJob(FrozenModel):
    kind: JobKind
    payload: dict[str, object]


class RunUpdate(FrozenModel):
    kind: Literal["initial", "revision"]
    session_id: str | None
    exit_code: int


class ResultPlan(FrozenModel):
    """Only persisted data drives retries; new feedback/configuration cannot change an effect."""

    job_id: int
    flow_id: int
    digest: str
    attempt_id: int | None
    state: JobState
    flow_state: FlowState | None = None
    error: str | None = None
    run: RunUpdate | None = None
    delivery: FlowDelivery | None = None
    derived: DerivedJob | None = None
    batch_id: int | None = None
    batch_state: BatchState | None = None
    consumed: list[FeedbackRef] = Field(default_factory=list)
    open_points: list[str] = Field(default_factory=list)
    effects: list[Effect] = Field(default_factory=list)


class EffectState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY = "retry"
    UNCERTAIN = "uncertain"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class EffectReceipt(FrozenModel):
    comment_id: int | None = None
    pull_request: PullRequestSnapshot | None = None


class EffectRecord(BaseModel):
    job_id: int
    effect_key: str
    position: int
    payload: Effect
    state: EffectState
    receipt: EffectReceipt | None = None
    claimed_by: str | None = None
    claim_expires_at: datetime | None = None
    dispatched_at: datetime | None = None
    attempts: int = 0
    next_attempt_at: datetime
    last_error: str | None = None


class ResultPending(RuntimeError):
    """An accepted result remains durable while its external outcome is pending reconciliation."""
