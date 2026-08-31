"""Durable single-assignment journal owned by one worker root."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from app.models import JobState
from app.protocol import Assignment, Failed, Succeeded, terminal_digest

TerminalMessage = Succeeded | Failed
_TERMINAL: TypeAdapter[TerminalMessage] = TypeAdapter(TerminalMessage)


class JournalError(RuntimeError):
    """The persisted assignment journal cannot be safely interpreted."""


class JournalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    job_id: int
    flow_id: int
    assignment_token: str
    state: Literal["received", "started", "succeeded", "failed"]
    result_digest: str | None = None
    terminal: TerminalMessage | None = None


class WorkerJournal:
    """Persist assignment progress atomically until the serve acknowledges a result."""

    def __init__(self, worker_root: Path) -> None:
        self.path = worker_root / "assignment-journal.json"

    def load(self) -> JournalRecord | None:
        if not self.path.exists():
            return None
        try:
            return JournalRecord.model_validate_json(self.path.read_text())
        except (OSError, ValidationError, ValueError) as exc:
            raise JournalError(f"invalid worker journal at {self.path}") from exc

    def record_received(self, assignment: Assignment) -> JournalRecord:
        record = JournalRecord(
            job_id=assignment.job_id,
            flow_id=assignment.flow_id,
            assignment_token=assignment.assignment_token,
            state="received",
        )
        self._write(record)
        return record

    def record_started(self) -> JournalRecord:
        record = self._required()
        if record.state != "received":
            raise JournalError(f"cannot start journal in {record.state} state")
        record.state = "started"
        self._write(record)
        return record

    def record_terminal(self, terminal: TerminalMessage) -> JournalRecord:
        record = self._required()
        if record.state not in ("started", "succeeded", "failed"):
            raise JournalError(f"cannot finish journal in {record.state} state")
        if (terminal.job_id, terminal.assignment_token) != (
            record.job_id,
            record.assignment_token,
        ):
            raise JournalError("terminal result does not match the journal assignment")
        payload = terminal.model_dump(mode="json")
        record.state = "succeeded" if isinstance(terminal, Succeeded) else "failed"
        record.result_digest = terminal_digest(terminal)
        record.terminal = _TERMINAL.validate_python(payload)
        self._write(record)
        return record

    def acknowledge(self, job_id: int, state: JobState) -> None:
        record = self._required()
        if record.job_id != job_id or record.state not in ("succeeded", "failed"):
            raise JournalError("result acknowledgement does not match terminal journal")
        if state not in (JobState.DONE, JobState.FAILED):
            raise JournalError("result acknowledgement is not terminal")
        self.path.unlink()

    def clear_rejected(self, job_id: int) -> None:
        record = self._required()
        if record.job_id != job_id:
            raise JournalError("recovery rejection does not match journal")
        self.path.unlink()

    def _required(self) -> JournalRecord:
        record = self.load()
        if record is None:
            raise JournalError("worker journal is missing")
        return record

    def _write(self, record: JournalRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = record.model_dump_json()
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                temporary = handle.name
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
