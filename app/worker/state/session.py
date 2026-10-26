"""Persistent worker session: execution state, recovery evidence and conversation access."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from app.contracts.conversation import HistoryPage, HistoryQuery
from app.contracts.protocol import (
    ArchiveTask,
    Assignment,
    ExecutionResult,
    ExecutionStopped,
    Failed,
    RecoveryDescriptor,
    RevisionExecutionRecord,
    StopAccepted,
    Succeeded,
    terminal_digest,
)
from app.domain.models import JobState
from app.worker.execution.workspaces import WorkspaceBinding
from app.worker.state.amp_history import AmpHistory
from app.worker.state.files import write_private_record
from app.worker.state.history import CodexHistory

TerminalMessage = Succeeded | Failed
_TERMINAL: TypeAdapter[TerminalMessage] = TypeAdapter(TerminalMessage)
_JOURNAL_JSON: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


class SessionError(RuntimeError):
    """The persisted assignment journal cannot be safely interpreted."""


class SessionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[4] = 4
    job_id: int
    flow_id: int
    assignment_token: str
    state: Literal["received", "started", "succeeded", "failed"]
    result_digest: str | None = None
    terminal: TerminalMessage | None = None
    revision_phase: Literal["resume", "fallback"] | None = None
    fallback_consumed: bool = False
    invocations: list[RevisionExecutionRecord] = []
    archive_task: ArchiveTask | None = None
    root_identity: tuple[str, int, int]
    acknowledged: bool = False
    cleanup_verified: bool = False
    workspace: WorkspaceBinding | None = None
    execution_nonce: str | None = None
    stopped: ExecutionStopped | None = None


def _parse_journal(payload: str) -> SessionState:
    """Read v4 journals with v14 receipts without widening the wire protocol or writing files.

    Only receipt version tags changed in v15. Preserve their content and ownership; the next
    normal state write under the runtime's root lock will persist the current representation.
    """
    try:
        return SessionState.model_validate_json(payload)
    except ValidationError:
        pass

    data = _JOURNAL_JSON.validate_json(payload)
    if data.get("version") != 4:
        raise ValueError("unsupported worker journal version")
    for name in ("terminal", "stopped"):
        message = data.get(name)
        if message is None:
            continue
        if not isinstance(message, dict) or message.get("protocol_version") != 14:
            raise ValueError("unsupported or mixed journal message versions")
        message["protocol_version"] = 15

    record = SessionState.model_validate_json(json.dumps(data))
    if record.terminal is not None and (
        (record.terminal.job_id, record.terminal.assignment_token)
        != (record.job_id, record.assignment_token)
        or terminal_digest(record.terminal) != record.result_digest
    ):
        raise ValueError("previous journal result does not match its assignment or digest")
    if record.stopped is not None and (
        record.stopped.job_id, record.stopped.flow_id,
        record.stopped.assignment_token, record.stopped.execution_nonce,
    ) != (record.job_id, record.flow_id, record.assignment_token, record.execution_nonce):
        raise ValueError("previous journal stop receipt does not match its execution")
    return record


class WorkerSession:
    """Own local assignment state and its durable evidence across transport reconnects."""

    def __init__(self, worker_root: Path) -> None:
        self.path = worker_root / "assignment-journal.json"
        self.assignment: Assignment | None = None
        self._history = CodexHistory()
        self._amp_history = AmpHistory()

    def read(self, query: HistoryQuery) -> HistoryPage:
        """Read the session selected by control, independent of the active assignment."""
        return (self._amp_history if query.session_id.startswith("T-") else self._history).query(query)

    def recovery(self, *, executor_active: bool) -> RecoveryDescriptor | None:
        record = self.load()
        if record is None:
            return None
        return RecoveryDescriptor(
            job_id=record.job_id, assignment_token=record.assignment_token,
            state=record.state, result_digest=record.result_digest,
            stop_request_id=record.stopped.request_id if record.stopped else None,
            archive_resume=record.archive_task is not None and record.state == "started",
            executor_active=record.state == "started" and executor_active,
        )

    def validate_recovery(self, job_id: int, token: str) -> None:
        record = self._required()
        if (record.job_id, record.assignment_token) != (job_id, token):
            raise SessionError("recovery response does not match the worker session")

    def resume(self, connection_epoch: str, *, executor_active: bool) -> Assignment:
        """Rebind a live execution; only archive cleanup can resume after process loss."""
        if self.assignment is None or not executor_active:
            record = self._required()
            if record.state != "started" or record.archive_task is None:
                raise SessionError("serve resumed an assignment without a live executor")
            self.assignment = Assignment(
                job_id=record.job_id, flow_id=record.flow_id,
                assignment_token=record.assignment_token, connection_epoch=connection_epoch,
                task=record.archive_task,
            )
        self.assignment = self.assignment.model_copy(update={"connection_epoch": connection_epoch})
        return self.assignment

    def replay(self, connection_epoch: str) -> TerminalMessage:
        record = self._required()
        if record.terminal is None:
            raise SessionError("serve requested a result missing from the session")
        return record.terminal.model_copy(update={"connection_epoch": connection_epoch})

    def complete(self, outcome: ExecutionResult | Exception) -> TerminalMessage:
        """Redact the ephemeral push capability before persisting any execution outcome."""
        assignment = self.assignment
        if assignment is None:
            raise SessionError("active assignment is missing")
        if isinstance(outcome, Exception):
            terminal: TerminalMessage = Failed(
                job_id=assignment.job_id, assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch, error=str(outcome),
            )
        else:
            terminal = Succeeded(
                job_id=assignment.job_id, assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch, result=outcome,
            )
        if assignment.secret is not None:
            terminal = type(terminal).model_validate_json(
                terminal.model_dump_json().replace(
                    assignment.secret.push.token.get_secret_value(), "[REDACTED]",
                )
            )
        self.record_terminal(terminal)
        return terminal

    def load(self) -> SessionState | None:
        if not self.path.exists():
            return None
        try:
            record = _parse_journal(self.path.read_text())
        except (OSError, ValidationError, ValueError) as exc:
            raise SessionError(f"invalid worker journal at {self.path}") from exc
        if record.root_identity != self._root_identity():
            raise SessionError("worker journal belongs to another worker root")
        return record

    def record_received(self, assignment: Assignment) -> SessionState:
        previous = self.load()
        archive = assignment.task if isinstance(assignment.task, ArchiveTask) else None
        if archive is not None and (previous is None or previous.flow_id != archive.flow_id
                                    or not (previous.acknowledged or previous.state == "received")):
            raise SessionError("archive requires the original Flow's worker journal")
        if (previous is not None and previous.archive_task is not None
                and (previous.state not in {"received", "failed"} or previous.archive_task != archive)):
            raise SessionError("cannot replace unacknowledged archive cleanup evidence")
        record = SessionState(
            job_id=assignment.job_id,
            flow_id=assignment.flow_id,
            assignment_token=assignment.assignment_token,
            state="received",
            execution_nonce=uuid4().hex,
            archive_task=archive,
            root_identity=self._root_identity(),
            workspace=(previous.workspace if previous and previous.flow_id == assignment.flow_id
                       and (previous.stopped is None or archive is not None)
                       else None),
            cleanup_verified=bool(previous and previous.flow_id == assignment.flow_id
                                  and previous.cleanup_verified),
        )
        self._write(record)
        self.assignment = assignment
        return record

    def record_stopped(self, receipt: ExecutionStopped) -> None:
        record = self._required()
        if (record.job_id, record.flow_id, record.assignment_token, record.execution_nonce) != (
                receipt.job_id, receipt.flow_id, receipt.assignment_token, receipt.execution_nonce):
            raise SessionError("stop receipt does not match owned execution")
        record.stopped = receipt
        record.acknowledged = False
        self._write(record)

    def acknowledge_stop(self, accepted: StopAccepted) -> None:
        record = self._required()
        if record.stopped is None or any(getattr(record.stopped, field) != getattr(accepted, field)
                                        for field in ('request_id', 'flow_id', 'job_id', 'assignment_token')):
            raise SessionError("stop acknowledgement does not match receipt")
        record.acknowledged = True
        self._write(record)
        self.assignment = None

    def bind_workspace(self, binding: WorkspaceBinding) -> None:
        """Fix the allocation fence before coding; the same Flow may not silently move."""
        record = self._required()
        if (record.state != "started" or binding.flow_id != record.flow_id
                or (record.workspace is not None and record.workspace != binding)):
            raise SessionError("workspace does not match the journal allocation")
        record.workspace = binding
        self._write(record)

    def verify_cleanup(self, task: ArchiveTask) -> WorkspaceBinding:
        """Persist the exact release fence; the pool separately proves filesystem safety."""
        record = self._required()
        if (record.state != "started" or record.archive_task != task or record.workspace is None
                or (record.workspace.flow_id, record.workspace.repository) != (task.flow_id, task.repo)):
            raise SessionError("archive cleanup does not match the journal assignment")
        if not record.cleanup_verified:
            record.cleanup_verified = True
            self._write(record)
        return record.workspace

    def _root_identity(self) -> tuple[str, int, int]:
        root = self.path.parent.resolve()
        root.mkdir(parents=True, exist_ok=True)
        stat = root.stat()
        return str(root), stat.st_dev, stat.st_ino

    def record_started(self) -> SessionState:
        record = self._required()
        if record.state != "received":
            raise SessionError(f"cannot start journal in {record.state} state")
        record.state = "started"
        self._write(record)
        return record

    def begin_revision(self, phase: Literal["resume", "fallback"]) -> None:
        """Durably consume the single fallback before starting another process."""
        record = self._required()
        if record.state != "started" or record.fallback_consumed:
            raise SessionError("revision invocation cannot be safely restarted")
        if phase == "resume" and record.revision_phase is not None:
            raise SessionError("resume has already been attempted")
        record.revision_phase = phase
        record.fallback_consumed = phase == "fallback"
        self._write(record)

    def finish_invocation(self, invocation: RevisionExecutionRecord) -> list[RevisionExecutionRecord]:
        record = self._required()
        if record.state != "started" or record.revision_phase != invocation.mode:
            raise SessionError("invocation does not match durable revision phase")
        record.invocations.append(invocation)
        self._write(record)
        return record.invocations

    def record_terminal(self, terminal: TerminalMessage) -> SessionState:
        record = self._required()
        if record.state not in ("started", "succeeded", "failed"):
            raise SessionError(f"cannot finish journal in {record.state} state")
        if (terminal.job_id, terminal.assignment_token) != (
            record.job_id,
            record.assignment_token,
        ):
            raise SessionError("terminal result does not match the journal assignment")
        payload = terminal.model_dump(mode="json")
        record.state = "succeeded" if isinstance(terminal, Succeeded) else "failed"
        record.result_digest = terminal_digest(terminal)
        record.terminal = _TERMINAL.validate_python(payload)
        self._write(record)
        return record

    def acknowledge(self, job_id: int, state: JobState) -> None:
        record = self._required()
        if record.job_id != job_id or record.state not in ("succeeded", "failed"):
            raise SessionError("result acknowledgement does not match terminal journal")
        if state not in (JobState.DONE, JobState.FAILED):
            raise SessionError("result acknowledgement is not terminal")
        if record.archive_task is None or state is JobState.FAILED:
            # Preserve root/Flow provenance between jobs and cleanup proof after partial failure.
            record.acknowledged = True
            self._write(record)
            self.assignment = None
            return
        self.path.unlink()
        self.assignment = None

    def clear_rejected(self, job_id: int) -> None:
        record = self._required()
        if record.job_id != job_id:
            raise SessionError("recovery rejection does not match journal")
        if record.state == "received":
            # No executor ran. Keep the root/Flow provenance when merge supersedes this command.
            self.assignment = None
            return
        if record.archive_task is not None:
            raise SessionError("archive recovery rejected; retain cleanup evidence for reconciliation")
        self.path.unlink()
        self.assignment = None

    def _required(self) -> SessionState:
        record = self.load()
        if record is None:
            raise SessionError("worker journal is missing")
        return record

    def _write(self, record: SessionState) -> None:
        write_private_record(self.path, record.model_dump_json())
