"""Durable scheduling facts for answer jobs; execution uses CodingAgent's contract."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

if TYPE_CHECKING:
    from app.domain.models import CodingExecutionResult

MAX_INPUT_BYTES = 128_000
MAX_OUTPUT_BYTES = 64_000
LOCAL_BUB_WORKER_ID = "__local_bub__"
RequestId = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{32}$")]
AdapterVersion = Annotated[str, Field(strict=True, pattern=r"^[a-zA-Z0-9._/-]{1,64}$")]
FailureCode = Literal[
    "unavailable", "busy", "connection_closed", "rpc_timeout", "execution_timeout",
    "rate_limited", "backend_unavailable", "authentication", "configuration",
    "invalid_response", "output_limit", "backend_error", "cancelled",
]
StopReason = Literal["max_attempts", "deadline", "non_retryable", "unavailable"]
_RECOVERABLE = frozenset({
    "busy", "connection_closed", "rpc_timeout", "execution_timeout", "rate_limited",
    "backend_unavailable",
})


class InferenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InferenceFailure(InferenceModel):
    code: FailureCode
    retry_after_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @property
    def recoverable(self) -> bool:
        return self.code in _RECOVERABLE

    @classmethod
    def from_result(cls, result: CodingExecutionResult) -> Self | None:
        """Translate an executed agent outcome into the queue's existing retry policy."""
        if result.exit_code == 0 and result.failure_kind is None:
            return None
        code = result.failure_kind
        return cls(code="execution_timeout" if code == "timeout" else code or "backend_error",
                   retry_after_seconds=result.retry_after_seconds)


class InferenceSummary(InferenceModel):
    """Stable end-of-call facts; callers attach their own Flow/batch attribution."""

    logical_call_id: RequestId
    attempts: int = Field(strict=True, ge=0, le=3)
    max_attempts: Literal[3] = 3
    stop_reason: StopReason
    last_failure_code: FailureCode
    last_worker_id: str | None = None
    backend: AdapterVersion | None = None
    finished_at: datetime
    elapsed_ms: int = Field(ge=0)

    @property
    def notify(self) -> bool:
        return self.last_failure_code != "cancelled" and (self.attempts == self.max_attempts or (
            self.attempts > 0 and self.stop_reason == "deadline"
        ))


class InferenceJob(InferenceModel):
    """One durable logical call; only authorized dispatches consume its three attempts.

    Start arguments never change. The current request and epoch fence one dispatch,
    including after controller restarts. Unknown-outcome workers cannot be reused;
    other failed workers are merely deprioritized after confirmed cleanup.
    """

    request_id: RequestId = Field(default_factory=lambda: uuid4().hex)
    task: str = Field(strict=True, max_length=MAX_INPUT_BYTES)
    output_schema: dict[str, JsonValue] | None = None
    model: str | None = None
    effort: str | None = None
    max_output_bytes: int = Field(default=MAX_OUTPUT_BYTES, strict=True, gt=0, le=MAX_OUTPUT_BYTES)
    timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    current_request_id: RequestId | None = None
    deadline_at: datetime
    validation: Literal["text", "review", "flow_naming"] = "text"
    batch_id: int | None = None
    summary: InferenceSummary | None = None
    consumed: bool = False
    cancel_failure: InferenceFailure | None = None
    failures: list[InferenceFailure] = Field(default_factory=list, max_length=3)
    avoid_workers: frozenset[str] = frozenset()
    excluded_workers: frozenset[str] = frozenset()
    next_attempt_at: datetime | None = None
    assignment_epoch: str | None = None
    backend: AdapterVersion | None = None
    source_attempt_id: int | None = None
    context_digest: str | None = None


class InferenceError(Exception):
    """A failed inference, without raw provider output or credentials."""

    def __init__(self, failure: InferenceFailure, *, backend: str | None = None) -> None:
        super().__init__(failure.code)
        self.failure, self.backend = failure, backend
