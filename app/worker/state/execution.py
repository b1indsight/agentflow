"""Execution state and durable recovery evidence, independent of a connection's lifetime."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from app.contracts.conversation import HistoryPage, HistoryQuery
from app.contracts.protocol import (
    ArchiveTask,
    AssignmentArguments,
    ExecutionResult,
    ExecutionStopped,
    Failed,
    RecoveryDescriptor,
    RevisionExecutionRecord,
    StopFence,
    Succeeded,
    TerminalPayload,
    terminal_digest,
)
from app.domain.models import JobState
from app.worker.execution.workspaces import RepositoryWorkspaces, WorkspaceBinding
from app.worker.state.amp_history import AmpHistory
from app.worker.state.files import sync_directory, write_private_record
from app.worker.state.history import CodexHistory
from app.worker.state.record import ExecutionRecord, JournalSelection, decode_journal

_TERMINAL: TypeAdapter[TerminalPayload] = TypeAdapter(TerminalPayload)


class ExecutionStateError(RuntimeError):
    """The persisted assignment journal cannot be safely interpreted."""


class ExecutionState:
    """Own exactly one Flow's durable evidence, independent of other concurrent executions.

    Construction and reads never migrate. Call scan under the process-lifetime worker-root
    lock before accepting work; it validates every record and migrates the legacy layout.
    All mutations likewise require that caller-held lock.
    """

    def __init__(self, worker_root: Path, flow_id: int) -> None:
        if type(flow_id) is not int or flow_id <= 0:
            raise ValueError("journal Flow must be a positive integer")
        self.worker_root = worker_root
        self.flow_id = flow_id
        self.directory = worker_root / "journals"
        self._legacy = worker_root / "assignment-journal.json"
        self._selection_path = self.directory / "current.json"
        self.assignment: AssignmentArguments | None = None
        self._history = CodexHistory()
        self._amp_history = AmpHistory()

    @property
    def path(self) -> Path:
        """The explicit Flow path; current.json is never an execution router."""
        return self._flow_path(self.flow_id)

    @classmethod
    def scan(cls, root: Path) -> list[int]:
        """Validate all journal evidence and migrate old selection under the caller's root lock.

        A failed rename or pointer removal is retryable. No journal is overwritten, dropped,
        or treated as acknowledged merely because it was not selected by the old runtime.
        """
        directory = root / "journals"
        if root.is_symlink() or directory.is_symlink() or (
            directory.exists() and not directory.is_dir()
        ):
            raise ExecutionStateError("worker journal root/directory is not a real directory")
        legacy = root / "assignment-journal.json"
        selection_path = directory / "current.json"
        records: dict[int, ExecutionRecord] = {}
        paths = sorted(directory.iterdir()) if directory.exists() else []
        selection: JournalSelection | None = None
        for path in paths:
            if path.is_symlink() or not path.is_file():
                raise ExecutionStateError(f"invalid worker journal at {path}")
            if re.fullmatch(r"\.(?:flow-[1-9][0-9]*|current)\.json\.[a-z0-9_]{8}", path.name):
                continue  # Only known unpublished private-record temporary names are ignored.
            if path == selection_path:
                try:
                    selection = JournalSelection.model_validate_json(path.read_text())
                except (OSError, ValueError) as exc:
                    raise ExecutionStateError(f"invalid journal selection at {path}") from exc
                continue
            match = re.fullmatch(r"flow-([1-9][0-9]*)\.json", path.name)
            if match is None:
                raise ExecutionStateError(f"invalid worker journal filename at {path}")
            state = cls(root, int(match[1]))
            record = state._read_path(path)
            assert record is not None
            records[record.flow_id] = record
        if legacy.is_symlink():
            raise ExecutionStateError(f"invalid worker journal at {legacy}")
        legacy_record: ExecutionRecord | None = None
        if legacy.exists():
            # Decode without selecting a Flow; migration must preserve the original bytes.
            identity = root.stat()
            try:
                legacy_record = decode_journal(
                    legacy.read_text(),
                    root_identity=(str(root.resolve()), identity.st_dev, identity.st_ino),
                )
            except (OSError, ValueError) as exc:
                raise ExecutionStateError(f"invalid worker journal at {legacy}") from exc
            if legacy_record.flow_id in records or (
                selection is not None and selection.flow_id != legacy_record.flow_id
            ):
                raise ExecutionStateError("legacy worker journal conflicts with journals directory")
            if legacy_record.flow_id <= 0:
                raise ExecutionStateError("invalid legacy journal Flow")
            records[legacy_record.flow_id] = legacy_record
        # A dangling old pointer is legitimate after archive ACK removed the selected file.
        if legacy_record is not None:
            if not directory.exists():
                directory.mkdir(mode=0o700, parents=True)
                sync_directory(root)
            legacy.rename(directory / f"flow-{legacy_record.flow_id}.json")
            sync_directory(directory)
            sync_directory(root)
        if selection is not None:
            selection_path.unlink()
            sync_directory(directory)
        return sorted(records)

    def read(self, query: HistoryQuery) -> HistoryPage:
        """Read the session selected by control, independent of the active assignment."""
        return (self._amp_history if query.session_id.startswith("T-") else self._history).query(
            query
        )

    def recovery(self, *, executor_active: bool) -> RecoveryDescriptor | None:
        record = self.load()
        if record is None:
            return None
        return RecoveryDescriptor(
            job_id=record.job_id,
            assignment_token=record.assignment_token,
            state=record.state,
            result_digest=record.result_digest,
            stop_request_id=record.stopped.request_id if record.stopped else None,
            archive_resume=record.archive_task is not None and record.state == "started",
            executor_active=record.state == "started" and executor_active,
        )

    def validate_recovery(self, job_id: int, token: str) -> None:
        record = self._required()
        if (record.job_id, record.assignment_token) != (job_id, token):
            raise ExecutionStateError("recovery response does not match the worker session")

    def resume(self, *, executor_active: bool) -> AssignmentArguments:
        """Rebind a live execution; only archive cleanup can resume after process loss."""
        record = self._required()
        assignment = self.assignment
        if assignment is not None and (
            assignment.job_id,
            assignment.flow_id,
            assignment.assignment_token,
            assignment.workspace,
            assignment.generation,
        ) != (record.job_id, record.flow_id, record.assignment_token,
              record.selected_workspace, record.selected_generation):
            raise ExecutionStateError("recovery response does not match the worker session")
        # A live task may finish after Register but before the resume decision. Its durable
        # result can rebind only with the original in-memory assignment, never after restart.
        if (
            assignment is not None
            and record.terminal is not None
            and record.state in {"succeeded", "failed"}
            and not record.acknowledged
            and record.stopped is None
            and (record.terminal.job_id, record.terminal.assignment_token)
            == (record.job_id, record.assignment_token)
        ):
            return assignment
        if self.assignment is None or not executor_active:
            if record.state != "started" or record.archive_task is None:
                raise ExecutionStateError("serve resumed an assignment without a live executor")
            if record.selected_workspace is None or record.selected_generation is None:
                raise ExecutionStateError("archive recovery has no selected workspace identity")
            self.assignment = AssignmentArguments(
                job_id=record.job_id,
                flow_id=record.flow_id,
                assignment_token=record.assignment_token,
                workspace=record.selected_workspace,
                generation=record.selected_generation,
                task=record.archive_task,
            )
        return self.assignment

    def replay(self) -> TerminalPayload:
        record = self._required()
        if record.terminal is None:
            raise ExecutionStateError("serve requested a result missing from the session")
        return record.terminal

    def complete(self, outcome: ExecutionResult | Exception) -> TerminalPayload:
        """Redact the ephemeral push capability before persisting any execution outcome."""
        assignment = self.assignment
        if assignment is None:
            raise ExecutionStateError("active assignment is missing")
        if isinstance(outcome, Exception):
            terminal: TerminalPayload = Failed(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                error=str(outcome),
            )
        else:
            terminal = Succeeded(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                result=outcome,
            )
        if assignment.secret is not None:
            terminal = type(terminal).model_validate_json(
                terminal.model_dump_json().replace(
                    assignment.secret.push.token.get_secret_value(),
                    "[REDACTED]",
                )
            )
        self.record_terminal(terminal)
        return terminal

    def load(self) -> ExecutionRecord | None:
        """Read this Flow only; legacy evidence remains readable before locked scan/migration."""
        if self.worker_root.is_symlink() or self.directory.is_symlink():
            raise ExecutionStateError("worker journal root/directory is a symlink")
        record = self._read_path(self.path)
        legacy = self._read_path(self._legacy)
        if legacy is not None and legacy.flow_id == self.flow_id:
            if record is not None:
                raise ExecutionStateError("legacy worker journal conflicts with journals directory")
            return legacy
        return record

    def _flow_path(self, flow_id: int) -> Path:
        return self.directory / f"flow-{flow_id}.json"

    def _read_path(self, path: Path) -> ExecutionRecord | None:
        if path.is_symlink():
            raise ExecutionStateError(f"invalid worker journal at {path}")
        if not path.exists():
            return None
        try:
            record = decode_journal(path.read_text(), root_identity=self._root_identity())
            if path != self._legacy and path != self._flow_path(record.flow_id):
                raise ValueError("worker journal filename does not match its Flow")
            return record
        except (OSError, ValidationError, ValueError) as exc:
            raise ExecutionStateError(f"invalid worker journal at {path}") from exc

    def record_received(self, assignment: AssignmentArguments) -> ExecutionRecord:
        if assignment.flow_id != self.flow_id:
            raise ExecutionStateError("assignment does not match this Flow journal")
        previous = self.load()
        archive = assignment.task if isinstance(assignment.task, ArchiveTask) else None
        if previous is not None and not previous.acknowledged:
            if (previous.job_id, previous.assignment_token, previous.selected_workspace,
                previous.selected_generation, previous.archive_task) == (
                    assignment.job_id, assignment.assignment_token, assignment.workspace,
                    assignment.generation, archive,
            ) and previous.state == "received" and previous.stopped is None:
                self.assignment = assignment
                return previous
            raise ExecutionStateError("cannot replace unacknowledged execution evidence")
        if previous is not None and not previous.pool_acknowledged:
            raise ExecutionStateError("workspace acknowledgement is still pending reconciliation")
        if previous is not None and previous.job_id == assignment.job_id and (
            previous.assignment_token == assignment.assignment_token
            or previous.state != "received"
        ):
            raise ExecutionStateError("settled assignment cannot execute again")
        if archive is not None and (
            previous is None
            or previous.flow_id != archive.flow_id
            or not previous.acknowledged
        ):
            raise ExecutionStateError("archive requires the original Flow's worker journal")
        if previous is not None and previous.archive_task is not None and (
            (previous.stopped is None and previous.state not in {"received", "failed"})
            or previous.archive_task != archive
        ):
            raise ExecutionStateError("cannot replace unacknowledged archive cleanup evidence")
        binding = (previous.workspace if previous is not None
                   and (previous.stopped is None or archive is not None) else None)
        if (binding is not None and archive is not None and previous is not None
                and previous.stopped is not None and assignment.workspace == binding.slot_id
                and assignment.generation == binding.generation + 1):
            # Stop ACK already recycled this directory. The merged Flow keeps its
            # original location; the pool must independently verify the cleanup witness.
            binding = binding.model_copy(update={"generation": assignment.generation})
        if binding is not None and (binding.slot_id, binding.generation) != (
            assignment.workspace, assignment.generation
        ):
            raise ExecutionStateError("assignment target differs from original workspace")
        record = ExecutionRecord(
            job_id=assignment.job_id,
            flow_id=assignment.flow_id,
            assignment_token=assignment.assignment_token,
            state="received",
            execution_nonce=uuid4().hex,
            archive_task=archive,
            root_identity=self._root_identity(),
            workspace=binding,
            selected_repository=assignment.task.repo,
            selected_workspace=assignment.workspace,
            selected_generation=assignment.generation,
            cleanup_verified=bool(
                previous and previous.flow_id == assignment.flow_id and previous.cleanup_verified
            ),
        )
        self._write(record)
        self.assignment = assignment
        return record

    def record_stopped(self, receipt: ExecutionStopped) -> None:
        record = self._required()
        if (record.job_id, record.flow_id, record.assignment_token, record.execution_nonce) != (
            receipt.job_id,
            receipt.flow_id,
            receipt.assignment_token,
            receipt.execution_nonce,
        ):
            raise ExecutionStateError("stop receipt does not match owned execution")
        record.stopped = receipt
        record.acknowledged = False
        record.pool_acknowledged = False
        record.retire_after_ack = False
        self._write(record)

    def acknowledge_stop(self, accepted: StopFence) -> None:
        record = self._required()
        if record.stopped is None or (
            record.stopped.request_id,
            record.stopped.flow_id,
            record.stopped.job_id,
            record.stopped.assignment_token,
        ) != (accepted.request_id, accepted.flow_id, accepted.job_id, accepted.assignment_token):
            raise ExecutionStateError("stop acknowledgement does not match receipt")
        if record.acknowledged:
            self.assignment = None
            return
        record.acknowledged = True
        record.pool_acknowledged = False
        self._write(record)
        self.assignment = None

    def bind_workspace(self, binding: WorkspaceBinding) -> None:
        """Fix the allocation fence before coding; the same Flow may not silently move."""
        record = self._required()
        if (
            record.state != "started"
            or binding.flow_id != record.flow_id
            or (binding.slot_id, binding.generation) != (
                record.selected_workspace, record.selected_generation
            )
            or (record.workspace is not None and record.workspace != binding)
        ):
            raise ExecutionStateError("workspace does not match the journal allocation")
        record.workspace = binding
        self._write(record)

    def verify_cleanup(self, task: ArchiveTask) -> WorkspaceBinding:
        """Persist the exact release fence; the pool separately proves filesystem safety."""
        record = self._required()
        if (
            record.state != "started"
            or record.archive_task != task
            or record.workspace is None
            or (record.workspace.flow_id, record.workspace.repository) != (task.flow_id, task.repo)
        ):
            raise ExecutionStateError("archive cleanup does not match the journal assignment")
        if not record.cleanup_verified:
            record.cleanup_verified = True
            self._write(record)
        return record.workspace

    def _root_identity(self) -> tuple[str, int, int]:
        if self.worker_root.is_symlink():
            raise ExecutionStateError("worker journal root is a symlink")
        root = self.worker_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        stat = root.stat()
        return str(root), stat.st_dev, stat.st_ino

    def record_started(self) -> ExecutionRecord:
        record = self._required()
        if record.state != "received":
            raise ExecutionStateError(f"cannot start journal in {record.state} state")
        record.state = "started"
        self._write(record)
        return record

    def begin_revision(self, phase: Literal["resume", "fallback"]) -> None:
        """Durably consume the single fallback before starting another process."""
        record = self._required()
        if record.state != "started" or record.fallback_consumed:
            raise ExecutionStateError("revision invocation cannot be safely restarted")
        if phase == "resume" and record.revision_phase is not None:
            raise ExecutionStateError("resume has already been attempted")
        record.revision_phase = phase
        record.fallback_consumed = phase == "fallback"
        self._write(record)

    def finish_invocation(
        self, invocation: RevisionExecutionRecord
    ) -> list[RevisionExecutionRecord]:
        record = self._required()
        if record.state != "started" or record.revision_phase != invocation.mode:
            raise ExecutionStateError("invocation does not match durable revision phase")
        record.invocations.append(invocation)
        self._write(record)
        return record.invocations

    def record_terminal(self, terminal: TerminalPayload) -> ExecutionRecord:
        record = self._required()
        if record.state not in ("started", "succeeded", "failed"):
            raise ExecutionStateError(f"cannot finish journal in {record.state} state")
        if (terminal.job_id, terminal.assignment_token) != (
            record.job_id,
            record.assignment_token,
        ):
            raise ExecutionStateError("terminal result does not match the journal assignment")
        payload = terminal.model_dump(mode="json")
        record.state = "succeeded" if isinstance(terminal, Succeeded) else "failed"
        record.result_digest = terminal_digest(terminal)
        record.terminal = _TERMINAL.validate_python(payload)
        self._write(record)
        return record

    def acknowledge(self, job_id: int, state: JobState) -> None:
        record = self._required()
        if record.job_id != job_id or record.state not in ("succeeded", "failed"):
            raise ExecutionStateError("result acknowledgement does not match terminal journal")
        if state not in (JobState.DONE, JobState.FAILED):
            raise ExecutionStateError("result acknowledgement is not terminal")
        if record.acknowledged:
            self.assignment = None
            return
        # Keep even successful archives until the pool receipt is durable. Deleting here would
        # strand a cleaned allocation if the process died before the following manifest write.
        record.acknowledged = True
        record.pool_acknowledged = False
        record.retire_after_ack = record.archive_task is not None and state is JobState.DONE
        self._write(record)
        self.assignment = None

    def reconcile_acknowledgement(self, workspaces: RepositoryWorkspaces) -> None:
        """Finish ACK publication after receipt and at startup, before accepting more work.

        Call after on_result_accepted/on_stop_accepted, and for every scanned Flow at startup.
        This is idempotent across crashes before/after either durable write. Archive deletion
        occurs last; a failed publication preserves the acknowledged journal for another try.
        """
        record = self.load()
        if record is None or not record.acknowledged:
            return
        if not record.pool_acknowledged:
            if record.state == "received" and record.stopped is None:
                # Explicit rejection of an unstarted command has no pool execution to settle.
                record.pool_acknowledged = True
            else:
                if (record.selected_repository is None or record.selected_workspace is None
                        or record.selected_generation is None):
                    raise ExecutionStateError("acknowledged legacy journal has no workspace target")
                workspaces.acknowledge_target(
                    record.selected_repository, record.flow_id, record.selected_workspace,
                    record.selected_generation, record.job_id, record.assignment_token,
                )
                record.pool_acknowledged = True
            self._write(record)
        if record.retire_after_ack:
            self.path.unlink()
            sync_directory(self.directory)

    def clear_rejected(self, job_id: int) -> None:
        record = self._required()
        if record.job_id != job_id:
            raise ExecutionStateError("recovery rejection does not match journal")
        if record.state == "received":
            # Explicit controller rejection, not mere receipt, permits a later token to replace it.
            record.acknowledged = True
            record.pool_acknowledged = True
            self._write(record)
            self.assignment = None
            return
        raise ExecutionStateError("recovery rejected; retain execution evidence for reconciliation")

    def _required(self) -> ExecutionRecord:
        record = self.load()
        if record is None:
            raise ExecutionStateError("worker journal is missing")
        return record

    def _write(self, record: ExecutionRecord) -> None:
        if record.flow_id != self.flow_id:
            raise ExecutionStateError("cannot write another Flow's journal")
        if self._legacy.exists() or self._selection_path.exists():
            self.scan(self.worker_root)
        self.load()  # Reject symlinks and changed root identity before replacing any evidence.
        if not self.directory.exists():
            self.directory.mkdir(mode=0o700, parents=True)
            sync_directory(self.worker_root)
        write_private_record(self._flow_path(record.flow_id), record.model_dump_json())
