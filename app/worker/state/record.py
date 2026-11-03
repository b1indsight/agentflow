"""Pure journal v5 schema and strict, read-only v4 conversion.

ExecutionState remains the IO owner. This module never opens a path, acquires a lock,
rewrites a journal or acknowledges an execution. Pass the already established root identity
to decode_journal; normal locked writes are responsible for persisting the new format.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, JsonValue, StrictInt, TypeAdapter, model_validator

from app.contracts.protocol import (
    ArchiveTask,
    ExecutionStopped,
    ProtocolModel,
    RevisionExecutionRecord,
    TerminalPayload,
    terminal_digest,
)
from app.contracts.receipts import LegacyStopped, LegacyTerminal
from app.worker.execution.workspaces import WorkspaceBinding

_JSON = TypeAdapter(dict[str, JsonValue])
_INTEGER = TypeAdapter(StrictInt)


class _RecordFields(ProtocolModel):
    job_id: int
    flow_id: int
    assignment_token: str
    state: Literal["received", "started", "succeeded", "failed"]
    result_digest: str | None = None
    terminal: TerminalPayload | None = None
    revision_phase: Literal["resume", "fallback"] | None = None
    fallback_consumed: bool = False
    invocations: list[RevisionExecutionRecord] = Field(default_factory=list)
    archive_task: ArchiveTask | None = None
    root_identity: tuple[str, int, int]
    acknowledged: bool = False
    cleanup_verified: bool = False
    workspace: WorkspaceBinding | None = None
    execution_nonce: str | None = None
    stopped: ExecutionStopped | None = None

    @model_validator(mode="after")
    def _evidence(self) -> Self:
        if self.terminal is not None and (
            (self.terminal.job_id, self.terminal.assignment_token)
            != (self.job_id, self.assignment_token)
            or terminal_digest(self.terminal) != self.result_digest
            or self.terminal.type != self.state
        ):
            raise ValueError("journal result does not match its assignment, state or digest")
        if self.terminal is None and (
            self.result_digest is not None or self.state in {"succeeded", "failed"}
        ):
            raise ValueError("terminal journal evidence is missing")
        if self.stopped is not None and (
            self.stopped.job_id,
            self.stopped.flow_id,
            self.stopped.assignment_token,
            self.stopped.execution_nonce,
        ) != (self.job_id, self.flow_id, self.assignment_token, self.execution_nonce):
            raise ValueError("journal stop receipt does not match its execution")
        return self


class ExecutionRecord(_RecordFields):
    """Current durable business data; no frame, epoch or message identity is persisted."""

    version: Annotated[Literal[5], BeforeValidator(_INTEGER.validate_python)] = 5


class _LegacyRecord(_RecordFields):
    version: Annotated[Literal[4], BeforeValidator(_INTEGER.validate_python)]
    terminal: LegacyTerminal | None = None
    stopped: LegacyStopped | None = None

    @model_validator(mode="after")
    def _receipt_versions(self) -> Self:
        if (
            self.terminal is not None
            and self.stopped is not None
            and self.terminal.protocol_version != self.stopped.protocol_version
        ):
            raise ValueError("mixed journal receipt versions")
        return self

    def current(self) -> ExecutionRecord:
        # Explicit projection preserves every execution field and strips only the validated
        # historical receipt header. No unknown data is discarded to make validation pass.
        return ExecutionRecord(
            job_id=self.job_id,
            flow_id=self.flow_id,
            assignment_token=self.assignment_token,
            state=self.state,
            result_digest=self.result_digest,
            terminal=self.terminal.terminal() if self.terminal is not None else None,
            revision_phase=self.revision_phase,
            fallback_consumed=self.fallback_consumed,
            invocations=self.invocations,
            archive_task=self.archive_task,
            root_identity=self.root_identity,
            acknowledged=self.acknowledged,
            cleanup_verified=self.cleanup_verified,
            workspace=self.workspace,
            execution_nonce=self.execution_nonce,
            stopped=self.stopped.receipt() if self.stopped is not None else None,
        )


def decode_journal(raw: str, *, root_identity: tuple[str, int, int]) -> ExecutionRecord:
    """Validate an existing v4/v5 record without changing its bytes or retirement state."""
    data = _JSON.validate_json(raw)
    version = data.get("version")
    if type(version) is not int:
        raise ValueError("journal version must be an explicit integer")
    if version == 5:
        record = ExecutionRecord.model_validate(data)
    elif version == 4:
        record = _LegacyRecord.model_validate(data).current()
    else:
        raise ValueError("unsupported worker journal version")
    if record.root_identity != root_identity:
        raise ValueError("worker journal belongs to another worker root")
    return record
