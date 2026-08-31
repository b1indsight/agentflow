"""Versioned messages exchanged by the control plane and remote workers."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.models import CodexResult, JobState, ReviewItemKind, TriageVerdict

PROTOCOL_VERSION: Literal[2] = 2


class ProtocolModel(BaseModel):
    """Strict protocol base so malformed remote input fails at the trust boundary."""

    model_config = ConfigDict(extra="forbid")


class Message(ProtocolModel):
    protocol_version: Literal[2] = PROTOCOL_VERSION


class ReviewFeedback(ProtocolModel):
    kind: ReviewItemKind
    github_id: int
    author: str
    body: str
    path: str | None = None
    original_commit_id: str | None = None
    diff_hunk: str | None = None


class InitialTask(ProtocolModel):
    kind: Literal["initial"] = "initial"
    attempt_id: int
    repo: str
    issue_number: int
    issue_title: str
    issue_body: str
    branch: str


class TriageTask(ProtocolModel):
    kind: Literal["triage"] = "triage"
    attempt_id: int
    discussion: str
    pr_context: str


class RevisionTask(ProtocolModel):
    kind: Literal["revision"] = "revision"
    attempt_id: int
    repo: str
    branch: str
    session_id: str
    instruction: str
    feedback: list[ReviewFeedback]


class AnswerTask(ProtocolModel):
    kind: Literal["answer"] = "answer"
    attempt_id: int
    question: str
    pr_context: str


class ArchiveTask(ProtocolModel):
    kind: Literal["archive"] = "archive"
    attempt_id: int
    repo: str
    branches: list[str]


ExecutionTask = Annotated[
    InitialTask | TriageTask | RevisionTask | AnswerTask | ArchiveTask,
    Field(discriminator="kind"),
]


class InitialResult(ProtocolModel):
    kind: Literal["initial"] = "initial"
    session_id: str | None
    last_message: str
    exit_code: int


class TriageResult(ProtocolModel):
    kind: Literal["triage"] = "triage"
    verdict: TriageVerdict


class RevisionResult(ProtocolModel):
    kind: Literal["revision"] = "revision"
    session_id: str | None
    last_message: str
    exit_code: int
    output: CodexResult = Field(default_factory=CodexResult)


class AnswerResult(ProtocolModel):
    kind: Literal["answer"] = "answer"
    answer: str


class ArchiveResult(ProtocolModel):
    kind: Literal["archive"] = "archive"


ExecutionResult = Annotated[
    InitialResult | TriageResult | RevisionResult | AnswerResult | ArchiveResult,
    Field(discriminator="kind"),
]


class RecoveryDescriptor(ProtocolModel):
    job_id: int
    assignment_token: str
    state: Literal["received", "started", "succeeded", "failed"]
    result_digest: str | None = None
    executor_active: bool = False


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


WorkerMessage = Annotated[
    Register | Started | Heartbeat | Succeeded | Failed,
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


ServerMessage = Annotated[
    Registered
    | Assignment
    | ResumeAssignment
    | ReplayResult
    | RecoveryRejected
    | ResultAccepted
    | ProtocolError,
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
