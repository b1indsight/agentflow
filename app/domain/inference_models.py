"""Bounded, business-independent text inference requests, results and safe failures."""

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.domain.json import strict_json

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


class InferenceMessage(InferenceModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(strict=True, max_length=MAX_INPUT_BYTES)


class InferenceTask(InferenceModel):
    version: Literal[1] = 1
    request_id: RequestId = Field(default_factory=lambda: uuid4().hex)
    messages: list[InferenceMessage] = Field(min_length=1, max_length=64)
    output_schema: dict[str, JsonValue] | None = None
    max_output_bytes: int = Field(default=MAX_OUTPUT_BYTES, strict=True, gt=0, le=MAX_OUTPUT_BYTES)
    timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        if len(self.model_dump_json().encode()) > MAX_INPUT_BYTES:
            raise ValueError("inference input exceeds byte limit")
        pending: list[tuple[JsonValue, int]] = [(self.output_schema, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 32:
                raise ValueError("inference schema exceeds nesting limit")
            if isinstance(value, dict):
                ref = value.get("$ref")
                if isinstance(ref, str) and not ref.startswith("#"):
                    raise ValueError("external schema references are not supported")
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
        return self

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True,
                               ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class InferenceOutput(InferenceModel):
    request_id: RequestId
    request_digest: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    text: str = Field(strict=True, max_length=MAX_OUTPUT_BYTES)
    adapter_version: AdapterVersion
    model: str | None = Field(default=None, strict=True, max_length=256)

    @model_validator(mode="after")
    def _bytes(self) -> Self:
        if len(self.text.encode()) > MAX_OUTPUT_BYTES:
            raise ValueError("inference output exceeds byte limit")
        return self

    def validate_for(self, task: InferenceTask) -> None:
        """Correlate before parsing; schemas remain the caller's business contract."""
        if self.request_id != task.request_id or self.request_digest != task.digest:
            raise InferenceError(InferenceFailure(code="invalid_response"))
        if len(self.text.encode()) > task.max_output_bytes:
            raise InferenceError(InferenceFailure(code="output_limit"))
        if not self.text.strip():
            raise InferenceError(InferenceFailure(code="invalid_response"))
        if task.output_schema is not None:
            try:
                strict_json(self.text)
            except ValueError:
                raise InferenceError(InferenceFailure(code="invalid_response")) from None


class InferenceCapability(InferenceModel):
    adapter_version: AdapterVersion
    formats: frozenset[Literal["text", "json"]] = frozenset({"text", "json"})
    max_input_bytes: int = Field(default=MAX_INPUT_BYTES, strict=True, gt=0, le=MAX_INPUT_BYTES)
    max_output_bytes: int = Field(default=MAX_OUTPUT_BYTES, strict=True, gt=0, le=MAX_OUTPUT_BYTES)

    def supports(self, task: InferenceTask) -> bool:
        return (("json" if task.output_schema is not None else "text") in self.formats
                and len(task.model_dump_json().encode()) <= self.max_input_bytes
                and task.max_output_bytes <= self.max_output_bytes)


class InferenceFailure(InferenceModel):
    code: FailureCode
    retry_after_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @property
    def recoverable(self) -> bool:
        return self.code in _RECOVERABLE


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

    ``task`` never changes. ``current_task`` and its epoch identify one dispatch, even
    across controller restarts. Excluded workers cannot be reused for unknown outcomes;
    other failed workers are merely deprioritized after confirmed cleanup.
    """

    task: InferenceTask
    current_task: InferenceTask | None = None
    deadline_at: datetime
    validation: Literal["text", "review"] = "text"
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
    """One failed attempt, without raw provider output or credentials."""

    def __init__(self, failure: InferenceFailure, *, worker_id: str | None = None,
                 backend: str | None = None) -> None:
        super().__init__(failure.code)
        self.failure, self.worker_id, self.backend = failure, worker_id, backend


class InferenceCallFailed(InferenceError):
    """A logical call ended; its summary must not cause another model invocation."""

    def __init__(self, error: InferenceError, summary: InferenceSummary) -> None:
        super().__init__(error.failure, worker_id=error.worker_id, backend=error.backend)
        self.summary = summary
