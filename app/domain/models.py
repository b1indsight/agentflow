"""领域模型：枚举、Pydantic 持久化/JSON 契约、内部 dataclass。

见 docs/plan/m0.md §4、docs/architecture/components.md §4.7 与 review-loop.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, model_validator

from app.domain.delivery_models import DeliveryReceipt
from app.domain.inference_models import InferenceCapability, InferenceJob


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- 枚举


class FlowState(StrEnum):
    """Authoritative business stage; finalizing shares running's board group."""

    QUEUED = "queued"
    RUNNING = "running"
    REVIEW = "review"
    BLOCKED = "blocked"
    FINALIZING = "finalizing"
    ARCHIVED = "archived"


TERMINAL_STATES = frozenset({FlowState.ARCHIVED})
WORKING_STATES = frozenset({FlowState.QUEUED, FlowState.RUNNING, FlowState.REVIEW})


class ArchiveReason(StrEnum):
    SUCCEEDED = "succeeded"
    ABANDONED = "abandoned"


class DeliveryState(StrEnum):
    NONE = "none"
    OPENING = "opening"
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class JobKind(StrEnum):
    INITIAL = "initial"  # 首轮编码
    REVISION = "revision"  # 修订
    ARCHIVE = "archive"  # 收尾归档
    INFERENCE = "inference"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class WorkerStatus(StrEnum):
    ONLINE = "online"
    BUSY = "busy"
    RECOVERING = "recovering"
    OFFLINE = "offline"
    FAILED = "failed"


class Verdict(StrEnum):
    REVISE = "revise"
    WAIT = "wait"
    ANSWER = "answer"
    IGNORE = "ignore"


class ReviewItemKind(StrEnum):
    REVIEW_BODY = "review_body"  # pull_request_review 正文
    INLINE = "inline"  # pull_request_review_comment
    CONVERSATION = "conversation"  # issue_comment（PR 上）


class VcsKind(StrEnum):
    GIT = "git"
    JJ = "jj"


# --------------------------------------------------------------------- 持久化模型


class Flow(BaseModel):
    """Stable issue-level lifecycle and delivery identity."""

    id: int
    repo: str  # "owner/name"
    issue_number: int
    state: FlowState
    installation_id: int | None = None
    current_attempt_id: int | None = None
    current_round_no: int = 1
    archive_reason: ArchiveReason | None = None
    archive_summary: str | None = None
    archived_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def accepts(self, kind: JobKind) -> bool:
        """Finalization never authorizes more coding, even if its PR is reopened."""
        if kind is JobKind.ARCHIVE:
            return self.state is FlowState.FINALIZING
        return self.state in WORKING_STATES and self.archive_reason is None


class Attempt(BaseModel):
    """One execution/recovery/retry fact within a stable Flow binding."""

    id: int
    flow_id: int
    round_no: int = 1
    codex_session_id: str | None = None
    session_generation: int = 0
    last_revision_summary: str = ""
    revision_cycles: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class FlowExecution(BaseModel):
    """Stable ownership of one worker slot and repo for the lifetime of a Flow."""

    flow_id: int
    worker_id: str
    repo_identity: str
    bound_at: datetime = Field(default_factory=utcnow)
    recovery_deadline: datetime | None = None
    released_at: datetime | None = None


class FlowDelivery(BaseModel):
    """Flow-owned branch and pull-request delivery state."""

    flow_id: int
    branch: str
    base_branch: str | None = None
    state: DeliveryState = DeliveryState.NONE
    pr_number: int | None = None
    url: str | None = None
    head_sha: str | None = None
    merged_at: datetime | None = None
    merge_commit_sha: str | None = None
    title: str | None = None
    body: str | None = None
    version: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def supersedes(self, kind: JobKind) -> bool:
        """Closed PRs retire coding/review intent; merged deliveries still require archive."""
        return self.state in {DeliveryState.CLOSED, DeliveryState.MERGED} and kind is not JobKind.ARCHIVE


class FlowToolCall(BaseModel):
    """Durable idempotency ledger for a fenced Flow tool invocation."""

    call_id: str
    flow_id: int
    attempt_id: int
    job_id: int
    name: str
    arguments: dict[str, object]
    response: DeliveryReceipt | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class FlowRound(BaseModel):
    """Archived round snapshot; evidence and corrections do not authorize execution."""

    flow_id: int
    round_no: int
    repo: str
    pr_number: int | None
    original_reason: ArchiveReason
    archive_reason: ArchiveReason
    archive_summary: str | None
    archived_at: datetime
    delivery: FlowDelivery | None
    execution: FlowExecution | None
    merged_at: datetime | None = None
    merge_commit_sha: str | None = None
    version: int = 0


class Job(BaseModel):
    id: int
    flow_id: int | None
    round_no: int = 1
    kind: JobKind
    state: JobState
    inference: InferenceJob | None = None
    attempt: int = 0
    heartbeat_at: datetime | None = None
    assigned_worker_id: str | None = None
    assignment_token: str | None = None
    started_at: datetime | None = None
    dispatch_authorized_at: datetime | None = None
    lease_expires_at: datetime | None = None
    recovery_deadline: datetime | None = None
    result_payload: dict[str, JsonValue] | None = None
    result_digest: str | None = None
    result_received_at: datetime | None = None
    result_applied_at: datetime | None = None
    verify_failures: int = 0
    verify_next_at: datetime | None = None
    verify_last_error: str | None = None
    source_job_id: int | None = None
    source_key: str | None = None
    prepare_failures: int = 0
    prepare_next_at: datetime | None = None
    prepare_first_failed_at: datetime | None = None
    prepare_last_error: str | None = None
    last_error: str | None = None
    payload: dict | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _kind_fields(self) -> Job:
        if self.kind is JobKind.INFERENCE:
            if self.inference is None:
                raise ValueError("inference jobs require a frozen inference call")
        elif self.flow_id is None or self.inference is not None:
            raise ValueError("coding jobs require a Flow and cannot contain inference")
        return self

    @property
    def required_flow_id(self) -> int:
        """Narrow the coding-only boundary; inference provenance grants no Flow authority."""
        if self.kind is JobKind.INFERENCE or self.flow_id is None:
            raise ValueError("inference jobs have no coding Flow authority")
        return self.flow_id


class WorkerRecord(BaseModel):
    draining: bool = False
    worker_id: str
    protocol_version: int
    status: WorkerStatus
    execute: bool = True
    inference: InferenceCapability | None = None
    current_inference_job_id: int | None = None
    last_inference_assigned_at: datetime | None = None
    connection_epoch: str | None = None
    current_job_id: int | None = None
    first_connected_at: datetime
    last_connected_at: datetime
    last_seen_at: datetime
    last_assigned_at: datetime | None = None
    last_error: str | None = None
    last_disconnect_reason: str | None = None


class WorkerView(BaseModel):
    draining: bool = False
    worker_id: str
    status: WorkerStatus
    current_job_id: int | None = None
    current_flow_id: int | None = None
    last_seen_at: datetime
    last_error: str | None = None
    connection_epoch: str | None = None
    lease_expires_at: datetime | None = None
    recovery_deadline: datetime | None = None
    last_disconnect_reason: str | None = None


# ---------------------------------------------------------------- 非持久化 dataclass


@dataclass(frozen=True)
class VcsExecutionContext:
    """Facts fixed before CodingAgent mutates a Flow-owned repository."""

    repo_path: Path
    branch: str
    remote: str
    git_remote_url: str | None
    jj_remote_url: str | None
    git_base_sha: str | None
    jj_base_sha: str | None
    jj_ancestor_sha: str | None
    push_token: SecretStr | None = field(default=None, repr=False)


@dataclass(frozen=True)
class VcsOutcome:
    """VCS facts independently verified after a CodingAgent run."""

    backend: VcsKind
    branch: str
    commit_sha: str
    remote_sha: str


@dataclass
class CodingExecutionResult:
    """CodingAgent 一次运行的结果（codex exec / resume）。"""

    session_id: str | None
    last_message: str
    exit_code: int
    failure_detail: str | None = None
    failure_kind: Literal["timeout", "output_limit"] | None = None


class SessionUnavailable(RuntimeError):
    """Adapter proved resume was rejected before any model or tool execution."""


@dataclass(frozen=True)
class GitHubCapability:
    """One assignment's repository-scoped installation credential, held only in memory."""

    token: SecretStr = field(repr=False)
    repository: str
    expires_at: datetime


@dataclass(frozen=True)
class CodingExecutionContext:
    """Ephemeral per-run capabilities; never persist or log this value."""

    github: GitHubCapability | None = None
    mcp_url: str | None = None
    mcp_token: SecretStr | None = None


# ------------------------------------------------------------ 跨模型 JSON 契约（pydantic）


class TriageVerdict(BaseModel):
    """Judge（bub）的语义判定产出。见 docs/architecture/review-loop.md §B.2。"""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    confidence: float = Field(default=0.0, strict=True, ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(default="", strict=True, max_length=2000)
    instruction: str = Field(default="", strict=True, max_length=8000)
    answer: str = Field(default="", strict=True, max_length=12000)
    open_points: list[Annotated[str, Field(strict=True, min_length=1, max_length=1000)]] = Field(
        default_factory=list, max_length=20,
    )

    @model_validator(mode="after")
    def _action_fields(self) -> TriageVerdict:
        if self.verdict is Verdict.REVISE:
            if not self.instruction.strip() or self.answer:
                raise ValueError("revise requires an instruction and forbids an answer")
        elif self.verdict is Verdict.ANSWER:
            if not self.answer.strip() or self.instruction or self.open_points:
                raise ValueError("answer requires a complete answer with no unresolved work")
        elif self.instruction or self.answer:
            raise ValueError("wait/ignore cannot contain an instruction or answer")
        if self.verdict is Verdict.IGNORE and self.open_points:
            raise ValueError("ignore cannot discard unresolved work")
        return self


class FeedbackRef(BaseModel):
    """Identity of one immutable review version, independent of GitHub's ID namespaces."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    item_id: int = Field(gt=0)
    version: int = Field(gt=0)


class AddressedComment(FeedbackRef):
    comment_id: int
    note: str


class DeferredComment(FeedbackRef):
    comment_id: int
    reason: str


class RevisionCodingOutput(BaseModel):
    """codex 修订 run 的结构化产出。见 docs/architecture/review-loop.md §B.4。"""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    vcs_backend_used: VcsKind
    addressed: list[AddressedComment]
    deferred: list[DeferredComment]
    needs_clarification: list[str]


class InitialCodingOutput(BaseModel):
    """Strict structured result required from an INITIAL CodingAgent run."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    vcs_backend_used: VcsKind
