"""Versioned messages exchanged by the control plane and remote workers."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.models import CodexResult, JobState, ReviewItemKind, TriageVerdict

PROTOCOL_VERSION: Literal[1] = 1


class ProtocolModel(BaseModel):
    """Strict protocol base so malformed remote input fails at the trust boundary."""

    model_config = ConfigDict(extra="forbid")


class Message(ProtocolModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION


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


class Register(Message):
    type: Literal["register"] = "register"
    worker_id: str


class Started(Message):
    type: Literal["started"] = "started"
    job_id: int
    assignment_token: str


class Heartbeat(Message):
    type: Literal["heartbeat"] = "heartbeat"
    job_id: int
    assignment_token: str


class Succeeded(Message):
    type: Literal["succeeded"] = "succeeded"
    job_id: int
    assignment_token: str
    result: ExecutionResult


class Failed(Message):
    type: Literal["failed"] = "failed"
    job_id: int
    assignment_token: str
    error: str


WorkerMessage = Annotated[
    Register | Started | Heartbeat | Succeeded | Failed,
    Field(discriminator="type"),
]


class Registered(Message):
    type: Literal["registered"] = "registered"
    heartbeat_interval: float


class Assignment(Message):
    type: Literal["assignment"] = "assignment"
    job_id: int
    flow_id: int
    assignment_token: str
    task: ExecutionTask


class ResultAccepted(Message):
    type: Literal["result_accepted"] = "result_accepted"
    job_id: int
    state: JobState


class ProtocolError(Message):
    type: Literal["error"] = "error"
    code: str
    detail: str


ServerMessage = Annotated[
    Registered | Assignment | ResultAccepted | ProtocolError,
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
