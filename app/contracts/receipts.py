"""Durable receipts and finite historical readers, deliberately separate from wire parsing.

Decoding is read-only. The caller retains corrupt/unknown data and controls normal writes,
application transactions and acknowledgements; a decoded receipt grants none of those.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, JsonValue, StrictInt, TypeAdapter, model_validator

from app.contracts.protocol import (
    ExecutionStopped,
    Failed,
    ProtocolModel,
    Succeeded,
    TerminalPayload,
    terminal_digest,
)

_JSON = TypeAdapter(dict[str, JsonValue])
_INTEGER = TypeAdapter(StrictInt)


class _LegacyHeader(ProtocolModel):
    protocol_version: Annotated[Literal[14, 15], BeforeValidator(_INTEGER.validate_python)]
    connection_epoch: str


class LegacySucceeded(Succeeded, _LegacyHeader):
    """Known stored v14/v15 success; required metadata is checked before projection."""

    type: Literal["succeeded"]

    def terminal(self) -> Succeeded:
        return Succeeded(
            job_id=self.job_id, assignment_token=self.assignment_token, result=self.result
        )


class LegacyFailed(Failed, _LegacyHeader):
    type: Literal["failed"]

    def terminal(self) -> Failed:
        return Failed(job_id=self.job_id, assignment_token=self.assignment_token, error=self.error)


LegacyTerminal = Annotated[LegacySucceeded | LegacyFailed, Field(discriminator="type")]
_LEGACY_TERMINAL: TypeAdapter[LegacyTerminal] = TypeAdapter(LegacyTerminal)


class LegacyStopped(ExecutionStopped, _LegacyHeader):
    type: Literal["execution_stopped"]

    def receipt(self) -> ExecutionStopped:
        return ExecutionStopped(
            request_id=self.request_id,
            flow_id=self.flow_id,
            job_id=self.job_id,
            assignment_token=self.assignment_token,
            execution_nonce=self.execution_nonce,
        )


class ResultInbox(ProtocolModel):
    """One stored business outcome and its origin, with no delivery metadata."""

    version: Annotated[Literal[1], BeforeValidator(_INTEGER.validate_python)] = 1
    source: Literal["worker", "control"]
    terminal: TerminalPayload

    @model_validator(mode="after")
    def _control_outcome(self) -> Self:
        if self.source == "control" and not isinstance(self.terminal, Failed):
            raise ValueError("control may only persist a failure outcome")
        return self


def decode_result_inbox(
    data: dict[str, JsonValue],
    *,
    job_id: int,
    assignment_token: str | None,
    result_digest: str,
) -> ResultInbox:
    """Read v1 or a known v14/v15 inbox and validate it against the owning database row.

    No input mutation, default-version inference or writeback occurs. An unassigned control
    failure alone can use the historical token='control' sentinel. Actual application and
    source-aware lifecycle SQL remain the caller's responsibility.
    """
    if "version" in data:
        inbox = ResultInbox.model_validate(data)
    else:
        previous = _LEGACY_TERMINAL.validate_python(data)
        inbox = ResultInbox(
            source="control" if previous.connection_epoch == "control" else "worker",
            terminal=previous.terminal(),
        )
    terminal = inbox.terminal
    control_token = (
        inbox.source == "control"
        and assignment_token is None
        and terminal.assignment_token == "control"
    )
    if (
        terminal.job_id != job_id
        or (terminal.assignment_token != assignment_token and not control_token)
        or terminal_digest(terminal) != result_digest
    ):
        raise ValueError("result inbox does not match its assignment or digest")
    return inbox


def _stop_digest(receipt: ExecutionStopped, version: Literal[14, 15]) -> str:
    # The v15 field order and Pydantic JSON encoding are a persisted hash contract,
    # independent of the current wire version. Do not hash a message or tool_result.
    projection: dict[str, JsonValue] = {
        "protocol_version": version,
        "request_id": receipt.request_id,
        "flow_id": receipt.flow_id,
        "job_id": receipt.job_id,
        "assignment_token": receipt.assignment_token,
        "type": "execution_stopped",
        "execution_nonce": receipt.execution_nonce,
    }
    return hashlib.sha256(_JSON.dump_json(projection)).hexdigest()


def stop_receipt_digest(receipt: ExecutionStopped) -> str:
    """Hash new stop evidence using the fixed historical v15 projection."""
    return _stop_digest(receipt, 15)


def matches_stop_receipt(receipt: ExecutionStopped, stored_digest: str) -> bool:
    """Check a real receipt against known v14/v15 hashes; never reconstruct evidence.

    A match is only a content check. The caller must still prove the original stop fence,
    execution cleanup and application/effect completion before acknowledging a stop.
    """
    return stored_digest in (stop_receipt_digest(receipt), _stop_digest(receipt, 14))
