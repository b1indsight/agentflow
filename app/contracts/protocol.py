"""Versioned messages exchanged by the control plane and remote workers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self, TypedDict
from uuid import UUID, uuid4, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    SecretStr,
    StrictBool,
    Tag,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)

from app.contracts.conversation import ConversationError, HistoryPage, SessionId
from app.domain.context_models import ReviewContext
from app.domain.delivery_models import DeliveryReceipt
from app.domain.inference_models import (
    InferenceCapability,
    InferenceFailure,
    InferenceOutput,
    InferenceTask,
)
from app.domain.models import (
    FeedbackRef,
    RevisionCodingOutput,
    VcsKind,
    WorkspaceInventory,
)
from app.domain.review_models import ReviewFeedback

PROTOCOL_VERSION: Literal[23] = 23
MAX_PAYLOAD_ITEMS = 32
MessageId = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{32}$")]
ConnectionEpoch = Annotated[str, Field(strict=True, min_length=1, max_length=64)]
_ID_NAMESPACE = UUID("46ebd500-2f34-463f-b746-f5c03b53b0ac")


class ProtocolModel(BaseModel):
    """Strict protocol base so malformed remote input fails at the trust boundary."""

    model_config = ConfigDict(extra="forbid")


class InitialTask(ProtocolModel):
    kind: Literal["initial"] = "initial"
    flow_id: int = Field(gt=0)
    attempt_id: int
    repo: str
    issue_number: int | None
    task_content: str
    branch: str
    base_branch: str = "main"
    remote: str = "origin"
    repo_identity: str = ""


class PushSecret(ProtocolModel):
    """Wire-only Git credential whose normal repr remains redacted."""

    token: SecretStr = Field(min_length=1)
    repository: str
    expires_at: datetime
    bot_login: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*\[bot\]$")
    bot_user_id: int = Field(gt=0)

    @field_serializer("token", when_used="json")
    def _serialize_token(self, token: SecretStr) -> str:
        return token.get_secret_value()


class AssignmentSecret(ProtocolModel):
    push: PushSecret


class RevisionTask(ProtocolModel):
    batch_id: int = Field(gt=0)
    attempt_id: int
    feedback: list[ReviewFeedback]
    kind: Literal["revision"] = "revision"
    purpose: Literal["answer", "revise"] = "revise"
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
    task_content: str
    previous_summary: str
    open_points: list[str]
    context: ReviewContext | None = None


class RevisionExecutionRecord(ProtocolModel):
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
    InitialTask | RevisionTask | ArchiveTask,
    Field(discriminator="kind"),
]


class InitialTaskResult(ProtocolModel):
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


class RevisionTaskResult(ProtocolModel):
    batch_id: int = Field(gt=0)
    feedback_refs: list[FeedbackRef]
    kind: Literal["revision"] = "revision"
    session_id: str | None
    last_message: str
    exit_code: int
    flow_id: int = Field(gt=0)
    attempt_id: int = Field(gt=0)
    previous_session_id: str | None
    session_generation: int = Field(ge=0)
    mode: Literal["resume", "fallback"]
    invocations: list[RevisionExecutionRecord] = Field(default_factory=list, max_length=2)
    output: RevisionCodingOutput | None = None
    failure_stage: str | None = None
    failure_detail: str | None = None
    vcs_backend: VcsKind | None = None
    branch: str | None = None
    commit_sha: str | None = None
    remote_sha: str | None = None
    clean_worktree: bool = False


class ArchiveResult(ArchiveIdentity):
    kind: Literal["archive"] = "archive"
    cleanup: Literal["released", "removed", "already_absent"]


ExecutionResult = Annotated[
    InitialTaskResult | RevisionTaskResult | ArchiveResult,
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


class ExecutionIdentity(ProtocolModel):
    """Assignment authority, independent of a connection or message identity."""

    job_id: int
    assignment_token: str


class RecoveryDecision(ExecutionIdentity):
    action: Literal["resume", "replay"]


class Succeeded(ExecutionIdentity):
    """Durable terminal data; never a standalone wire message."""

    type: Literal["succeeded"] = "succeeded"
    result: ExecutionResult


class Failed(ExecutionIdentity):
    type: Literal["failed"] = "failed"
    error: str


class StopFence(ProtocolModel):
    request_id: str
    flow_id: int
    job_id: int
    assignment_token: str


class ExecutionStopped(StopFence):
    """Durable evidence of stopped execution, without delivery metadata."""

    execution_nonce: str = Field(min_length=1, max_length=128)


TerminalPayload = Annotated[Succeeded | Failed, Field(discriminator="type")]


def terminal_digest(message: TerminalPayload) -> str:
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


class AssignmentArguments(ExecutionIdentity):
    flow_id: int
    workspace: str = Field(pattern=r"^[0-9a-f]{32}$")
    generation: int = Field(strict=True, gt=0)
    task: ExecutionTask
    secret: AssignmentSecret | None = None

    @model_validator(mode="after")
    def _flow_identity(self) -> Self:
        if self.task.flow_id != self.flow_id:
            raise ValueError("assignment and task belong to different Flows")
        return self


class HistoryArguments(ProtocolModel):
    # Cursor semantics stay in HistoryQuery, yielding a query error, not a disconnect.
    session_id: SessionId
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)
    limit: int = Field(default=100, ge=1, le=500)


class RecoveryRejectedArguments(ProtocolModel):
    job_id: int
    reason: str
    register_message_id: MessageId


class AcceptResultArguments(ExecutionIdentity):
    result_id: MessageId


class AcceptStopArguments(StopFence):
    receipt_id: MessageId


class ResultAcceptance(ExecutionIdentity):
    state: Literal["done", "failed"]


class FlowToolArguments(ExecutionIdentity):
    flow_id: int
    attempt_id: int
    call_id: str
    arguments: dict[str, object]
    vcs_backend: VcsKind
    branch: str
    commit_sha: str
    remote_sha: str


class ToolCall(ProtocolModel):
    """One typed invocation; its identity survives transport repackaging when replayed."""

    type: Literal["tool_call"]
    message_id: MessageId
    reply_to: None
    method: str


class ToolResult(ProtocolModel):
    """A method-specific result correlated to one invocation, not its containing message."""

    type: Literal["tool_result"]
    message_id: MessageId
    reply_to: MessageId
    method: str

    @model_validator(mode="after")
    def _reply_identity(self) -> Self:
        if self.reply_to == self.message_id:
            raise ValueError("a result cannot reply to itself")
        return self


class PayloadValidationError(ProtocolModel):
    """Fixed public failure; never carry input, field names or validator diagnostics."""

    code: Literal["validation_error"]
    message: Literal["message payload failed validation"]


class ValidationErrorResult(ToolResult):
    """Reject one item by its original ID, without claiming any business acceptance."""

    error: PayloadValidationError


class Assignment(ToolCall):
    method: Literal["worker.execute"]
    arguments: AssignmentArguments


class StopExecution(ToolCall):
    method: Literal["worker.stop"]
    arguments: StopFence


class ConversationQuery(ToolCall):
    method: Literal["worker.history"]
    arguments: HistoryArguments


class InferenceRequest(ToolCall):
    method: Literal["worker.infer"]
    job_id: int = Field(strict=True, gt=0)
    assignment_token: str = Field(strict=True, min_length=1)
    arguments: InferenceTask

    @model_validator(mode="after")
    def _request_identity(self) -> Self:
        if self.message_id != self.arguments.request_id:
            raise ValueError("inference request identity mismatch")
        return self


class RecoveryRejected(ToolCall):
    method: Literal["worker.reject_recovery"]
    arguments: RecoveryRejectedArguments


class RepositoryDemand(ToolCall):
    """Level-triggered capacity request; it grants no job or Flow ownership."""

    method: Literal["worker.provision"]
    repository: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ProvisionError(StrEnum):
    """Public repository-local failures; only filesystem errors retry automatically."""

    INVALID_REPOSITORY = "invalid_repository"
    WORKSPACE_CONFLICT = "workspace_conflict"
    FILESYSTEM_ERROR = "filesystem_error"


class ProvisionResult(ToolResult):
    """Finish one capacity request; only separately reported inventory grants capacity."""

    method: Literal["worker.provision"]
    repository: str
    error: ProvisionError | None


class Started(ToolCall):
    method: Literal["server.execution_started"]
    arguments: ExecutionIdentity


class AcceptResultRequest(ToolCall):
    method: Literal["server.accept_result"]
    arguments: AcceptResultArguments


class AcceptStop(ToolCall):
    method: Literal["server.accept_stop"]
    arguments: AcceptStopArguments


class FlowToolRequest(ToolCall):
    arguments: FlowToolArguments


class ExecuteResult(ToolResult):
    method: Literal["worker.execute"]
    job_id: int
    assignment_token: str
    result: ExecutionResult | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _one_outcome(self) -> Self:
        if (self.result is None) == (self.error is None):
            raise ValueError("execution result requires exactly one of result or error")
        return self

    def terminal(self) -> TerminalPayload:
        """Project only durable fields; message and invocation metadata never enter the journal."""
        if self.result is not None:
            return Succeeded(
                job_id=self.job_id, assignment_token=self.assignment_token, result=self.result
            )
        assert self.error is not None
        return Failed(job_id=self.job_id, assignment_token=self.assignment_token, error=self.error)


class StopResult(ToolResult):
    method: Literal["worker.stop"]
    result: ExecutionStopped


class ConversationResult(ToolResult):
    method: Literal["worker.history"]
    page: HistoryPage | None = None
    error: ConversationError | None = None

    @model_validator(mode="after")
    def _one_outcome(self) -> Self:
        if (self.page is None) == (self.error is None):
            raise ValueError("conversation result requires exactly one of page or error")
        return self


class InferenceResult(ToolResult):
    method: Literal["worker.infer"]
    job_id: int = Field(strict=True, gt=0)
    assignment_token: str = Field(strict=True, min_length=1)
    output: InferenceOutput | None = None
    failure: InferenceFailure | None = None

    @model_validator(mode="after")
    def _one_outcome(self) -> Self:
        if (self.output is None) == (self.failure is None):
            raise ValueError("inference result requires exactly one outcome")
        return self


class AcceptResultResponse(ToolResult):
    method: Literal["server.accept_result"]
    result: ResultAcceptance


class StopAccepted(ToolResult):
    method: Literal["server.accept_stop"]
    result: StopFence


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


class FlowToolResponse(ToolResult):
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


_FIXED_METHODS = frozenset(
    {
        "worker.execute",
        "worker.stop",
        "worker.history",
        "worker.infer",
        "worker.provision",
        "worker.reject_recovery",
        "server.execution_started",
        "server.accept_result",
        "server.accept_stop",
    }
)


def _method_tag(value: object) -> str | None:
    """Unknown names retain the existing typed Flow INVALID_REQUEST path.

    Known methods never fall back after malformed arguments. This discriminator selects
    a schema only; it neither registers handlers nor invokes code named by remote input.
    """
    if isinstance(value, (ToolCall, ToolResult)):
        method: object = value.method
    elif isinstance(value, dict):
        method = value.get("method")
    else:
        return None
    if not isinstance(method, str):
        return None
    return method if method in _FIXED_METHODS else "flow_tool"


WorkerCall = Annotated[
    Annotated[Started, Tag("server.execution_started")]
    | Annotated[AcceptResultRequest, Tag("server.accept_result")]
    | Annotated[AcceptStop, Tag("server.accept_stop")]
    | Annotated[FlowToolRequest, Tag("flow_tool")],
    Discriminator(_method_tag),
]


def _result_tag(value: object) -> str | None:
    if isinstance(value, ValidationErrorResult):
        return "validation_error"
    if isinstance(value, dict) and isinstance(value.get("error"), dict):
        # An error object is never retried as a success/domain-error schema.
        return "validation_error"
    return _method_tag(value)


WorkerResult = Annotated[
    Annotated[ExecuteResult, Tag("worker.execute")]
    | Annotated[StopResult, Tag("worker.stop")]
    | Annotated[ConversationResult, Tag("worker.history")]
    | Annotated[InferenceResult, Tag("worker.infer")]
    | Annotated[ProvisionResult, Tag("worker.provision")]
    | Annotated[ValidationErrorResult, Tag("validation_error")],
    Discriminator(_result_tag),
]
WorkerPayload = Annotated[WorkerCall | WorkerResult, Field(discriminator="type")]
ServerCall = Annotated[
    Assignment | StopExecution | ConversationQuery | InferenceRequest | RecoveryRejected | RepositoryDemand,
    Field(discriminator="method"),
]
ServerResult = Annotated[
    Annotated[AcceptResultResponse, Tag("server.accept_result")]
    | Annotated[StopAccepted, Tag("server.accept_stop")]
    | Annotated[FlowToolResponse, Tag("flow_tool")]
    | Annotated[ValidationErrorResult, Tag("validation_error")],
    Discriminator(_result_tag),
]
ServerPayload = Annotated[ServerCall | ServerResult, Field(discriminator="type")]


class Message[Payload: ToolCall | ToolResult](ProtocolModel):
    """Common message fields. Every field is required, including explicit nulls and [].

    Use a concrete control or directional message and its directional parser. This model
    performs static checks only; receipt ACKs confer no business completion or authority.
    """

    protocol_version: Literal[23]
    message_id: MessageId
    connection_epoch: ConnectionEpoch | None
    ack: StrictBool
    ack_id: MessageId | None
    control: Literal["register", "registered", "inventory", "heartbeat", "cancel_job", "error"] | None
    payload: list[Payload] = Field(max_length=MAX_PAYLOAD_ITEMS)

    @field_validator("protocol_version", mode="before")
    @classmethod
    def _version_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("protocol_version must be an integer")
        return value

    @model_validator(mode="after")
    def _message_contract(self) -> Self:
        if self.ack != (self.ack_id is not None):
            raise ValueError("ack and ack_id must occur together")
        ids = [item.message_id for item in self.payload]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate payload message_id")
        if self.control is None and not self.payload:
            if not self.ack or self.connection_epoch is not None:
                raise ValueError("empty payload without control requires a null-epoch ACK")
        elif self.control == "register":
            if self.connection_epoch is not None or self.ack:
                raise ValueError("Register has no epoch or prior control to acknowledge")
        elif self.control != "error" and self.connection_epoch is None:
            raise ValueError("established-connection messages require an epoch")
        return self


class WorkerMessage(Message[WorkerPayload]):
    """Worker message with control=None; empty payload is allowed only for a receipt ACK."""

    control: Literal[None]  # noqa: PYI061 - Pydantic's discriminator requires Literal for null.


class ServerMessage(Message[ServerPayload]):
    """Server message with control=None; empty payload is allowed only for a receipt ACK."""

    control: Literal[None]  # noqa: PYI061 - Pydantic's discriminator requires Literal for null.


class InventoryPage(ProtocolModel):
    """A bounded snapshot page. The receiver explicitly requests each subsequent page."""

    inventory: list[WorkspaceInventory] = Field(max_length=MAX_PAYLOAD_ITEMS)
    recoveries: list[RecoveryDescriptor] = Field(max_length=MAX_PAYLOAD_ITEMS)
    inventory_complete: StrictBool


class Register(Message[WorkerPayload], InventoryPage):
    control: Literal["register"]
    worker_id: str
    workspace: str = Field(pattern=r"^[0-9a-f]{32}$")
    repo_workspace_limit: int = Field(strict=True, gt=0)
    execute: StrictBool = True
    inference: InferenceCapability | None

    @model_validator(mode="after")
    def _recovery_payload(self) -> Self:
        if not self.execute and self.recoveries:
            raise ValueError("infer-only workers cannot recover coding execution")
        if self.payload:
            raise ValueError("registration acceptances follow recovery decisions")
        return self


class Inventory(Message[WorkerPayload], InventoryPage):
    control: Literal["inventory"]

    @model_validator(mode="after")
    def _no_payload(self) -> Self:
        if self.payload:
            raise ValueError("inventory cannot carry business payload")
        return self


class Registered(Message[ServerPayload]):
    control: Literal["registered"]
    heartbeat_interval: float
    recoveries: list[RecoveryDecision] = Field(max_length=MAX_PAYLOAD_ITEMS)
    ready: StrictBool

    @model_validator(mode="after")
    def _recovery_payload(self) -> Self:
        if self.payload:
            raise ValueError("registration decisions cannot carry business payload")
        return self


class Heartbeat(Message[WorkerPayload]):
    control: Literal["heartbeat"]
    job_id: int
    assignment_token: str

    @model_validator(mode="after")
    def _execution_payload(self) -> Self:
        for item in self.payload:
            if isinstance(item, (ConversationResult, InferenceResult, ProvisionResult, ValidationErrorResult)):
                continue
            if isinstance(item, (Started, AcceptResultRequest, AcceptStop, FlowToolRequest)):
                identity = (item.arguments.job_id, item.arguments.assignment_token)
            elif isinstance(item, ExecuteResult):
                identity = (item.job_id, item.assignment_token)
            else:
                identity = (item.result.job_id, item.result.assignment_token)
            if identity != (self.job_id, self.assignment_token):
                raise ValueError("heartbeat and payload belong to different executions")
        return self


class CancelJob(Message[ServerPayload]):
    """Cancel only the inference assignment identified by this connection and token."""

    control: Literal["cancel_job"]
    job_id: int = Field(strict=True, gt=0)
    assignment_token: str = Field(strict=True, min_length=1)

    @model_validator(mode="after")
    def _no_payload(self) -> Self:
        if self.payload:
            raise ValueError("CancelJob cannot carry business payload")
        return self


class ProtocolError(Message[ServerPayload]):
    control: Literal["error"]
    code: str
    detail: str

    @model_validator(mode="after")
    def _no_payload(self) -> Self:
        if self.payload:
            raise ValueError("ProtocolError cannot carry business payload")
        return self


WorkerMessageUnion = Annotated[Register | Inventory | Heartbeat | WorkerMessage, Field(discriminator="control")]
ServerMessageUnion = Annotated[
    Registered | CancelJob | ProtocolError | ServerMessage, Field(discriminator="control")
]

_WORKER_MESSAGES: TypeAdapter[WorkerMessageUnion] = TypeAdapter(WorkerMessageUnion)
_SERVER_MESSAGES: TypeAdapter[ServerMessageUnion] = TypeAdapter(ServerMessageUnion)


def parse_worker_message(raw: str) -> WorkerMessageUnion:
    """Validate the complete worker message before any control or payload has effects."""

    return _WORKER_MESSAGES.validate_json(raw)


def parse_server_message(raw: str) -> ServerMessageUnion:
    """Validate the complete server message; historical records use durable decoders instead."""

    return _SERVER_MESSAGES.validate_json(raw)


def _stable_id(parts: list[str | int]) -> MessageId:
    return uuid5(_ID_NAMESPACE, json.dumps(parts, ensure_ascii=False, separators=(",", ":"))).hex


def execution_call_id(job_id: int, assignment_token: str) -> MessageId:
    """E: stable assignment invocation, without a connection epoch."""
    return _stable_id(["worker.execute", job_id, assignment_token])


def _terminal_id(job_id: int, assignment_token: str, digest: str) -> MessageId:
    return _stable_id(["terminal", job_id, assignment_token, digest])


def terminal_id(terminal: TerminalPayload) -> MessageId:
    """T: identity of the already-redacted durable execution outcome."""
    return _terminal_id(terminal.job_id, terminal.assignment_token, terminal_digest(terminal))


def accept_result_id(result_id: MessageId) -> MessageId:
    """A: request to acknowledge business application of one terminal result."""
    return _stable_id(["server.accept_result", result_id])


def stop_call_id(fence: StopFence) -> MessageId:
    """C: the same stop invocation across reconnects."""
    return _stable_id(
        ["stop_execution", fence.request_id, fence.flow_id, fence.job_id, fence.assignment_token]
    )


def stop_receipt_id(receipt: ExecutionStopped) -> MessageId:
    """R: evidence from one actual execution, including its nonce."""
    return _stable_id(
        [
            "execution_stopped",
            receipt.request_id,
            receipt.flow_id,
            receipt.job_id,
            receipt.assignment_token,
            receipt.execution_nonce,
        ]
    )


def accept_stop_id(receipt_id: MessageId) -> MessageId:
    """B: request to acknowledge one fenced stop receipt."""
    return _stable_id(["server.accept_stop", receipt_id])


class MessageHeader(TypedDict):
    protocol_version: Literal[23]
    message_id: MessageId
    connection_epoch: str | None
    ack: bool
    ack_id: MessageId | None


def message_header(connection_epoch: str | None) -> MessageHeader:
    """Construct fresh delivery metadata without supplying defaults to the wire parser."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": uuid4().hex,
        "connection_epoch": connection_epoch,
        "ack": False,
        "ack_id": None,
    }


class CallHeader(TypedDict):
    type: Literal["tool_call"]
    message_id: MessageId
    reply_to: None


def call_header(message_id: MessageId | None = None) -> CallHeader:
    """Construct an invocation header, retaining a stable identity when replaying."""
    return {"type": "tool_call", "message_id": message_id or uuid4().hex, "reply_to": None}


class ResultHeader(TypedDict):
    type: Literal["tool_result"]
    message_id: MessageId
    reply_to: MessageId


def result_header(reply_to: MessageId, message_id: MessageId | None = None) -> ResultHeader:
    """Correlate a result to its invocation rather than the containing message."""
    return {
        "type": "tool_result",
        "message_id": message_id or uuid4().hex,
        "reply_to": reply_to,
    }
