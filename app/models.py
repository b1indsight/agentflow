"""领域模型：枚举、Pydantic 持久化/JSON 契约、内部 dataclass。

见 docs/plan/m0.md §4、docs/architecture/components.md §4.7 与 review-loop.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- 枚举


class FlowState(StrEnum):
    """Flow lifecycle only; UI phases are derived from jobs and delivery."""

    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    FAILED = "failed"


# 处于这些状态的 flow 算「已终结」，(repo, issue) 唯一约束只作用于非终结态。
TERMINAL_STATES = frozenset({FlowState.SUCCEEDED, FlowState.CANCELLED, FlowState.FAILED})


class DeliveryState(StrEnum):
    NONE = "none"
    OPENING = "opening"
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class JobKind(StrEnum):
    INITIAL = "initial"  # 首轮编码
    TRIAGE = (
        "triage"  # settle 后判定（docs/architecture/review-loop.md）：revise/answer/wait/ignore
    )
    REVISION = "revision"  # 修订
    ANSWER = "answer"  # Historical rows only; protocol v6 answers directly from TRIAGE.
    ARCHIVE = "archive"  # 收尾归档


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
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Attempt(BaseModel):
    """One execution/recovery/retry fact within a stable Flow binding."""

    id: int
    flow_id: int
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
        """A merged delivery retires coding/review work while still requiring archive."""
        return self.state is DeliveryState.MERGED and kind is not JobKind.ARCHIVE


class FlowToolCall(BaseModel):
    """Durable idempotency ledger for a fenced Flow tool invocation."""

    call_id: str
    flow_id: int
    attempt_id: int
    job_id: int
    name: str
    arguments: dict[str, object]
    response: dict[str, object] | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Job(BaseModel):
    id: int
    flow_id: int
    kind: JobKind
    state: JobState
    attempt: int = 0
    pid: int | None = None
    heartbeat_at: datetime | None = None
    assigned_worker_id: str | None = None
    assignment_token: str | None = None
    started_at: datetime | None = None
    lease_expires_at: datetime | None = None
    recovery_deadline: datetime | None = None
    result_payload: dict | None = None
    result_digest: str | None = None
    result_received_at: datetime | None = None
    result_applied_at: datetime | None = None
    result_recovery_version: int = 1
    source_job_id: int | None = None
    source_key: str | None = None
    last_error: str | None = None
    payload: dict | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class WorkerRecord(BaseModel):
    worker_id: str
    protocol_version: int
    status: WorkerStatus
    connection_epoch: str | None = None
    current_job_id: int | None = None
    first_connected_at: datetime
    last_connected_at: datetime
    last_seen_at: datetime
    last_assigned_at: datetime | None = None
    last_error: str | None = None
    last_disconnect_reason: str | None = None


class WorkerView(BaseModel):
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


class ReviewItem(BaseModel):
    id: int | None
    attempt_id: int  # 归属到具体 attempt（§9-2），不再挂 flow
    kind: ReviewItemKind
    github_id: int  # review id / comment id
    author: str
    body: str
    thread_id: str | None = None  # GraphQL review thread id（inline 才有）
    path: str | None = None
    line: int | None = None
    original_commit_id: str | None = None
    diff_hunk: str | None = None
    relayed: bool = False
    resolved: bool = False
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- 非持久化 dataclass


@dataclass(frozen=True)
class SandboxPolicy:
    """Legacy/future Sandbox-port policy; M1 trusted-host Codex does not consume it."""

    writable_roots: tuple[Path, ...]  # agent 可写的目录（至少含工作区）
    network: bool  # 是否放开出口（push 时才要，尽量限到 github.com）


@dataclass(frozen=True)
class VcsExecutionContext:
    """Facts fixed before CodeAgent mutates a Flow-owned repository."""

    repo_path: Path
    branch: str
    remote: str
    git_remote_url: str | None
    jj_remote_url: str | None
    git_base_sha: str | None
    jj_base_sha: str | None
    jj_ancestor_sha: str | None
    push_context: CodeAgentContext | None = field(default=None, repr=False)


@dataclass(frozen=True)
class VcsOutcome:
    """VCS facts independently verified after a CodeAgent run."""

    declared_backend: VcsKind
    verified_backend: VcsKind
    branch: str
    commit_sha: str
    remote_sha: str


@dataclass
class AgentRun:
    """CodeAgent 一次运行的结果（codex exec / resume）。"""

    session_id: str | None
    last_message: str
    exit_code: int
    output: CodexResult | InitialCodexResult | None = None
    failure_detail: str | None = None


class SessionUnavailable(RuntimeError):
    """Adapter proved resume was rejected before any model or tool execution."""


@dataclass(frozen=True)
class CodeAgentContext:
    """Ephemeral per-run capabilities; never persist or log this value."""

    push_token: SecretStr | None = None
    push_repository: str | None = None
    mcp_socket: Path | None = None
    cwd: Path | None = None


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


class CodexResult(BaseModel):
    """codex 修订 run 的结构化产出。见 docs/architecture/review-loop.md §B.4。"""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    vcs_backend_used: VcsKind
    addressed: list[AddressedComment]
    deferred: list[DeferredComment]
    needs_clarification: list[str]


class InitialCodexResult(BaseModel):
    """Strict structured result required from an INITIAL CodeAgent run."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    vcs_backend_used: VcsKind
