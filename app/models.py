"""领域模型：枚举、Pydantic 持久化/JSON 契约、内部 dataclass。

见 docs/plan/m0.md §4、docs/architecture/components.md §4.7 与 review-loop.md。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- 枚举


class FlowState(StrEnum):
    PENDING = "pending"
    CODING = "coding"
    PR_OPEN = "pr_open"
    REVISING = "revising"
    MERGED = "merged"
    ARCHIVED = "archived"
    CANCELLED = "cancelled"
    FAILED = "failed"


# 处于这些状态的 flow 算「已终结」，(repo, issue) 唯一约束只作用于非终结态。
TERMINAL_STATES = frozenset({FlowState.ARCHIVED, FlowState.CANCELLED, FlowState.FAILED})


class JobKind(StrEnum):
    INITIAL = "initial"      # 首轮编码
    TRIAGE = "triage"  # settle 后判定（docs/architecture/review-loop.md）：revise/answer/wait/ignore
    REVISION = "revision"    # 修订
    ANSWER = "answer"        # Judge=answer，只作答不改码
    ARCHIVE = "archive"      # 收尾归档


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
    REVIEW_BODY = "review_body"    # pull_request_review 正文
    INLINE = "inline"              # pull_request_review_comment
    CONVERSATION = "conversation"  # issue_comment（PR 上）


class VcsKind(StrEnum):
    GIT = "git"
    JJ = "jj"
    # 注：VCS 不再是端口——只用于挑 agent_guidance() 的指引文本（§4.6 三轴）。


# --------------------------------------------------------------------- 持久化模型


class Flow(BaseModel):
    """issue 级追踪单元 + 状态机。per-attempt 的产物（branch/PR/会话）挪到 Attempt。

    一条 flow 可挂多个 Attempt（多方案分支，§9-2）；MVP 恰好一个，`current_attempt_id`
    指向当前驱动 flow 的那个。数据模型**不再把 issue↔PR 锁成 1:1**。
    """

    id: int
    repo: str  # "owner/name"
    issue_number: int
    state: FlowState
    installation_id: int | None = None
    current_attempt_id: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Attempt(BaseModel):
    """一次候选解法：自己的 branch / 工作区 / codex 会话 / PR（§9-2）。

    MVP：每 flow 一个。多 agent / 多方案时，一条 flow 挂多个 Attempt，各自独立推进。
    revise 失控闸计数（revision_cycles）是 per-attempt（挂在具体 PR 上）。
    """

    id: int
    flow_id: int
    branch: str | None = None
    codex_session_id: str | None = None
    pr_number: int | None = None
    worker_id: str | None = None
    revision_cycles: int = 0
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
    attempt_id: int             # 归属到具体 attempt（§9-2），不再挂 flow
    kind: ReviewItemKind
    github_id: int              # review id / comment id
    author: str
    body: str
    thread_id: str | None = None       # GraphQL review thread id（inline 才有）
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
    """一次 agent run 的沙盒策略（ports.Sandbox 产出）。只认路径，不认 git/jj。

    codex adapter 把它翻成 `-s workspace-write` (+ 网络开关)；容器 adapter 用它挂 FS + 配网。
    """

    writable_roots: tuple[Path, ...]   # agent 可写的目录（至少含工作区）
    network: bool                      # 是否放开出口（push 时才要，尽量限到 github.com）


@dataclass
class AgentRun:
    """CodeAgent 一次运行的结果（codex exec / resume）。"""

    session_id: str | None
    last_message: str
    exit_code: int
    output: CodexResult | None = None   # --output-schema 解析结果（修订轮）


# ------------------------------------------------------------ 跨模型 JSON 契约（pydantic）


class TriageVerdict(BaseModel):
    """Judge（bub）的语义判定产出。见 docs/architecture/review-loop.md §B.2。"""

    verdict: Verdict
    confidence: float = 0.0
    reason: str = ""
    instruction: str = ""       # verdict=revise：要做的具体改动
    answer: str = ""            # verdict=answer：回给提问者的话
    open_points: list[str] = []


class AddressedComment(BaseModel):
    comment_id: int
    note: str


class DeferredComment(BaseModel):
    comment_id: int
    reason: str


class CodexResult(BaseModel):
    """codex 修订 run 的结构化产出。见 docs/architecture/review-loop.md §B.4。"""

    summary: str = ""
    addressed: list[AddressedComment] = []
    deferred: list[DeferredComment] = []
    needs_clarification: list[str] = []
