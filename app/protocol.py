"""Versioned messages exchanged by the control plane and remote workers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    field_serializer,
    model_validator,
)

from app.context_models import MAX_CONTEXT_BYTES, ReviewContext
from app.delivery_models import DeliveryReceipt
from app.judge_models import JudgeDecision
from app.models import CodexResult, FeedbackRef, JobState, VcsKind
from app.review_models import ReviewFeedback

PROTOCOL_VERSION: Literal[10] = 10


class ProtocolModel(BaseModel):
    """Strict protocol base so malformed remote input fails at the trust boundary."""

    model_config = ConfigDict(extra="forbid")


class Message(ProtocolModel):
    protocol_version: Literal[10] = PROTOCOL_VERSION


class ReviewTask(ProtocolModel):
    batch_id: int = Field(gt=0)
    attempt_id: int
    feedback: list[ReviewFeedback]


class InitialTask(ProtocolModel):
    kind: Literal["initial"] = "initial"
    flow_id: int = Field(gt=0)
    attempt_id: int
    repo: str
    issue_number: int
    issue_title: str
    issue_body: str
    branch: str
    base_branch: str = "main"
    remote: str = "origin"
    repo_identity: str = ""


class PushSecret(ProtocolModel):
    """Wire-only Git credential whose normal repr remains redacted."""

    token: SecretStr = Field(min_length=1)
    repository: str
    expires_at: datetime

    @field_serializer("token", when_used="json")
    def _serialize_token(self, token: SecretStr) -> str:
        return token.get_secret_value()


class AssignmentSecret(ProtocolModel):
    push: PushSecret


class TriageTask(ProtocolModel):
    kind: Literal["triage"] = "triage"
    batch_id: int = Field(gt=0)
    attempt_id: int
    feedback: list[FeedbackRef]
    context: ReviewContext

    @model_validator(mode="after")
    def _context_batch(self) -> TriageTask:
        if self.context.batch_id != self.batch_id:
            raise ValueError("triage context belongs to another batch")
        if len(self.context.model_dump_json().encode()) > MAX_CONTEXT_BYTES:
            raise ValueError("triage context exceeds the byte budget")
        return self


class RevisionTask(ReviewTask):
    kind: Literal["revision"] = "revision"
    flow_id: int = Field(gt=0)
    repo: str
    branch: str
    repo_identity: str
    pr_number: int = Field(gt=0)
    base_branch: str
    expected_head_sha: str = Field(min_length=1)
    session_id: str | None
    session_generation: int = Field(ge=0)
    instruction: str
    issue_title: str
    issue_body: str
    previous_summary: str
    open_points: list[str]
    context: ReviewContext | None = None


class RevisionInvocation(ProtocolModel):
    mode: Literal["resume", "fallback"]
    session_id: str | None
    exit_code: int
    failure: str | None = None


class ArchiveIdentity(ProtocolModel):
    """Frozen ownership and delivery facts shared by cleanup commands and receipts."""

    flow_id: int = Field(gt=0)
    attempt_id: int = Field(gt=0)
    repo: str
    worker_id: str
    repo_identity: str
    delivery_version: int = Field(ge=0)


class ArchiveTask(ArchiveIdentity):
    kind: Literal["archive"] = "archive"


ExecutionTask = Annotated[
    InitialTask | TriageTask | RevisionTask | ArchiveTask,
    Field(discriminator="kind"),
]


class InitialResult(ProtocolModel):
    kind: Literal["initial"] = "initial"
    session_id: str | None
    last_message: str
    exit_code: int
    failure_stage: str | None = None
    failure_detail: str | None = None
    vcs_backend: VcsKind | None = None
    branch: str | None = None
    commit_sha: str | None = None
    remote_sha: str | None = None
    pushed: bool = False
    open_pr_call_id: str | None = None
    delivery_receipt: DeliveryReceipt | None = None


class ReviewResult(ProtocolModel):
    batch_id: int = Field(gt=0)
    feedback_refs: list[FeedbackRef]


class TriageResult(ReviewResult):
    kind: Literal["triage"] = "triage"
    decision: JudgeDecision


class RevisionResult(ReviewResult):
    kind: Literal["revision"] = "revision"
    session_id: str | None
    last_message: str
    exit_code: int
    flow_id: int = Field(gt=0)
    attempt_id: int = Field(gt=0)
    previous_session_id: str | None
    session_generation: int = Field(ge=0)
    mode: Literal["resume", "fallback"]
    invocations: list[RevisionInvocation] = Field(default_factory=list, max_length=2)
    output: CodexResult | None = None
    failure_stage: str | None = None
    failure_detail: str | None = None
    vcs_backend: VcsKind | None = None
    branch: str | None = None
    commit_sha: str | None = None
    remote_sha: str | None = None
    clean_worktree: bool = False


class ArchiveResult(ArchiveIdentity):
    kind: Literal["archive"] = "archive"
    cleanup: Literal["removed", "already_absent"]


ExecutionResult = Annotated[
    InitialResult | TriageResult | RevisionResult | ArchiveResult,
    Field(discriminator="kind"),
]


class RecoveryDescriptor(ProtocolModel):
    job_id: int
    assignment_token: str
    state: Literal["received", "started", "succeeded", "failed"]
    result_digest: str | None = None
    stop_request_id: str | None = None
    executor_active: bool = False
    archive_resume: bool = False


class Register(Message):
    type: Literal["register"] = "register"
    worker_id: str
    recovery: RecoveryDescriptor | None = None


class Started(Message):
    type: Literal["started"] = "started"
    job_id: int
    assignment_token: str
    connection_epoch: str


class Heartbeat(Message):
    type: Literal["heartbeat"] = "heartbeat"
    job_id: int
    assignment_token: str
    connection_epoch: str


class Succeeded(Message):
    type: Literal["succeeded"] = "succeeded"
    job_id: int
    assignment_token: str
    connection_epoch: str
    result: ExecutionResult


class Failed(Message):
    type: Literal["failed"] = "failed"
    job_id: int
    assignment_token: str
    connection_epoch: str
    error: str


class FlowToolRequest(Message):
    type: Literal["flow_tool_request"] = "flow_tool_request"
    job_id: int
    flow_id: int
    attempt_id: int
    assignment_token: str
    connection_epoch: str
    call_id: str
    name: str
    arguments: dict[str, object]
    vcs_backend: VcsKind
    branch: str
    commit_sha: str
    remote_sha: str


class StopFence(Message):
    request_id: str
    flow_id: int
    job_id: int
    assignment_token: str
    connection_epoch: str


class StopExecution(StopFence):
    type: Literal["stop_execution"] = "stop_execution"


class ExecutionStopped(StopFence):
    type: Literal["execution_stopped"] = "execution_stopped"
    execution_nonce: str = Field(min_length=1, max_length=128)


class StopAccepted(StopFence):
    type: Literal["stop_accepted"] = "stop_accepted"


WorkerMessage = Annotated[
    Register | Started | Heartbeat | Succeeded | Failed | FlowToolRequest | ExecutionStopped,
    Field(discriminator="type"),
]

TerminalMessage = Succeeded | Failed


def terminal_digest(message: TerminalMessage) -> str:
    """Hash only terminal content so a replay may use a freshly fenced epoch."""

    if isinstance(message, Succeeded):
        payload: dict[str, object] = {
            "type": message.type,
            "result": message.result.model_dump(mode="json"),
        }
    else:
        payload = {"type": message.type, "error": message.error}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class Registered(Message):
    type: Literal["registered"] = "registered"
    heartbeat_interval: float
    connection_epoch: str


class Assignment(Message):
    type: Literal["assignment"] = "assignment"
    job_id: int
    flow_id: int
    assignment_token: str
    connection_epoch: str
    task: ExecutionTask
    secret: AssignmentSecret | None = None

    @model_validator(mode="after")
    def _flow_identity(self) -> Assignment:
        if (isinstance(self.task, (InitialTask, RevisionTask, ArchiveTask))
                and self.task.flow_id != self.flow_id):
            raise ValueError("assignment and task belong to different Flows")
        return self


class ResumeAssignment(Message):
    type: Literal["resume_assignment"] = "resume_assignment"
    job_id: int
    assignment_token: str
    connection_epoch: str
    heartbeat_interval: float


class ReplayResult(Message):
    type: Literal["replay_result"] = "replay_result"
    job_id: int
    assignment_token: str
    connection_epoch: str
    heartbeat_interval: float


class RecoveryRejected(Message):
    type: Literal["recovery_rejected"] = "recovery_rejected"
    job_id: int
    reason: str


class ResultAccepted(Message):
    type: Literal["result_accepted"] = "result_accepted"
    job_id: int
    state: JobState
    assignment_token: str
    connection_epoch: str


class ProtocolError(Message):
    type: Literal["error"] = "error"
    code: str
    detail: str


class FlowToolErrorKind(StrEnum):
    """Stable error classes that tell a worker whether and how to retry a tool call."""

    INVALID_REQUEST = "invalid_request"
    STALE_ASSIGNMENT = "stale_assignment"
    CONFLICT = "conflict"
    AUTHORIZATION = "authorization"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    UPSTREAM = "upstream"


class FlowToolResponse(Message):
    type: Literal["flow_tool_response"] = "flow_tool_response"
    call_id: str
    result: DeliveryReceipt | None = None
    error: str | None = None
    error_kind: FlowToolErrorKind | None = None
    retryable: bool = False
    retry_after: str | None = None

    @model_validator(mode="after")
    def _validate_outcome(self) -> FlowToolResponse:
        if (self.result is None) == (self.error is None):
            raise ValueError("Flow tool response must contain exactly one of result or error")
        if self.error is not None and self.error_kind is None:
            raise ValueError("Flow tool error requires an error kind")
        if self.error is None and (
            self.error_kind is not None or self.retryable or self.retry_after is not None
        ):
            raise ValueError("successful Flow tool response cannot contain error metadata")
        if self.retry_after is not None and not self.retryable:
            raise ValueError("Flow tool retry_after requires a retryable error")
        return self


ServerMessage = Annotated[
    Registered
    | Assignment
    | ResumeAssignment
    | ReplayResult
    | RecoveryRejected
    | ResultAccepted
    | ProtocolError
    | FlowToolResponse
    | StopExecution
    | StopAccepted,
    Field(discriminator="type"),
]

_WORKER_MESSAGES: TypeAdapter[WorkerMessage] = TypeAdapter(WorkerMessage)
_SERVER_MESSAGES: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)


def parse_worker_message(raw: str) -> WorkerMessage:
    """Validate a worker-to-serve JSON message."""

    return _WORKER_MESSAGES.validate_json(raw)


def parse_server_message(raw: str) -> ServerMessage:
    """Validate a serve-to-worker JSON message."""

    return _SERVER_MESSAGES.validate_json(raw)
