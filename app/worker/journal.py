"""Durable single-assignment journal owned by one worker root."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from app.models import JobState
from app.protocol import (
    ArchiveTask,
    Assignment,
    Failed,
    RevisionInvocation,
    Succeeded,
    terminal_digest,
)

TerminalMessage = Succeeded | Failed
_TERMINAL: TypeAdapter[TerminalMessage] = TypeAdapter(TerminalMessage)


class JournalError(RuntimeError):
    """The persisted assignment journal cannot be safely interpreted."""


class JournalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[2] = 2
    job_id: int
    flow_id: int
    assignment_token: str
    state: Literal["received", "started", "succeeded", "failed"]
    result_digest: str | None = None
    terminal: TerminalMessage | None = None
    revision_phase: Literal["resume", "fallback"] | None = None
    fallback_consumed: bool = False
    invocations: list[RevisionInvocation] = []
    archive_task: ArchiveTask | None = None
    root_identity: tuple[str, int, int]
    acknowledged: bool = False
    cleanup_verified: bool = False


class WorkerJournal:
    """Persist assignment progress atomically until the serve acknowledges a result."""

    def __init__(self, worker_root: Path) -> None:
        self.path = worker_root / "assignment-journal.json"

    def load(self) -> JournalRecord | None:
        if not self.path.exists():
            return None
        try:
            record = JournalRecord.model_validate_json(self.path.read_text())
        except (OSError, ValidationError, ValueError) as exc:
            raise JournalError(f"invalid worker journal at {self.path}") from exc
        if record.root_identity != self._root_identity():
            raise JournalError("worker journal belongs to another worker root")
        return record

    def record_received(self, assignment: Assignment) -> JournalRecord:
        previous = self.load()
        archive = assignment.task if isinstance(assignment.task, ArchiveTask) else None
        if archive is not None and (previous is None or previous.flow_id != archive.flow_id
                                    or not (previous.acknowledged or previous.state == "received")):
            raise JournalError("archive requires the original Flow's worker journal")
        if (previous is not None and previous.archive_task is not None
                and (previous.state not in {"received", "failed"} or previous.archive_task != archive)):
            raise JournalError("cannot replace unacknowledged archive cleanup evidence")
        record = JournalRecord(
            job_id=assignment.job_id,
            flow_id=assignment.flow_id,
            assignment_token=assignment.assignment_token,
            state="received",
            archive_task=archive,
            root_identity=self._root_identity(),
            cleanup_verified=bool(previous and previous.cleanup_verified),
        )
        self._write(record)
        return record

    def verify_cleanup(self, task: ArchiveTask, *, repo_present: bool) -> None:
        """Persist ownership before deletion; only that proof permits an absent workspace."""
        record = self._required()
        if record.state != "started" or record.archive_task != task:
            raise JournalError("archive cleanup does not match the journal assignment")
        if not record.cleanup_verified:
            if not repo_present:
                raise JournalError("archive repository is absent without verified cleanup evidence")
            record.cleanup_verified = True
            self._write(record)

    def _root_identity(self) -> tuple[str, int, int]:
        root = self.path.parent.resolve()
        root.mkdir(parents=True, exist_ok=True)
        stat = root.stat()
        return str(root), stat.st_dev, stat.st_ino

    def record_started(self) -> JournalRecord:
        record = self._required()
        if record.state != "received":
            raise JournalError(f"cannot start journal in {record.state} state")
        record.state = "started"
        self._write(record)
        return record

    def begin_revision(self, phase: Literal["resume", "fallback"]) -> None:
        """Durably consume the single fallback before starting another process."""
        record = self._required()
        if record.state != "started" or record.fallback_consumed:
            raise JournalError("revision invocation cannot be safely restarted")
        if phase == "resume" and record.revision_phase is not None:
            raise JournalError("resume has already been attempted")
        record.revision_phase = phase
        record.fallback_consumed = phase == "fallback"
        self._write(record)

    def finish_invocation(self, invocation: RevisionInvocation) -> None:
        record = self._required()
        if record.state != "started" or record.revision_phase != invocation.mode:
            raise JournalError("invocation does not match durable revision phase")
        record.invocations.append(invocation)
        self._write(record)

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
        if record.archive_task is None or state is JobState.FAILED:
            # Preserve root/Flow provenance between jobs and cleanup proof after partial failure.
            record.acknowledged = True
            self._write(record)
            return
        self.path.unlink()

    def clear_rejected(self, job_id: int) -> None:
        record = self._required()
        if record.job_id != job_id:
            raise JournalError("recovery rejection does not match journal")
        if record.state == "received":
            # No executor ran. Keep the root/Flow provenance when merge supersedes this command.
            return
        if record.archive_task is not None:
            raise JournalError("archive recovery rejected; retain cleanup evidence for reconciliation")
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
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
