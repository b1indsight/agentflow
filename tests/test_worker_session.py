import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue, SecretStr, ValidationError

from app.contracts.protocol import (
    AssignmentArguments,
    AssignmentSecret,
    ExecutionResult,
    ExecutionStopped,
    FlowToolResponse,
    InitialTask,
    InitialTaskResult,
    PushSecret,
    RecoveryDecision,
    ResultAcceptance,
    StopFence,
    Succeeded,
    parse_worker_message,
    terminal_digest,
)
from app.domain.delivery_models import DeliveryReceipt, PreparedDelivery
from app.domain.models import JobState, VcsKind, VcsOutcome
from app.worker.execution.lifecycle import TaskExecutor, WorkerExecution, WorkerProtocolError
from app.worker.state.execution import ExecutionState, ExecutionStateError


def _assignment(
    *, job_id: int = 5, flow_id: int = 3, workspace: str = "a" * 32, generation: int = 1,
) -> AssignmentArguments:
    return AssignmentArguments(
        job_id=job_id,
        flow_id=flow_id,
        assignment_token="assignment-1",
        workspace=workspace,
        generation=generation,
        task=InitialTask(
            flow_id=flow_id,
            attempt_id=7,
            repo="owner/repo",
            issue_number=8,
            issue_title="Issue",
            issue_body="Body",
            branch="flow/issue-8",
        ),
        secret=AssignmentSecret(
            push=PushSecret(
                token=SecretStr("push-secret"),
                repository="owner/repo",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        ),
    )


def test_journal_persists_terminal_result_until_ack(tmp_path: Path) -> None:
    journal = ExecutionState(tmp_path, 3)
    assignment = _assignment()
    journal.record_received(assignment)
    assert "push-secret" not in journal.path.read_text()
    journal.record_started()
    terminal = Succeeded(
        job_id=assignment.job_id,
        assignment_token=assignment.assignment_token,
        result=InitialTaskResult(session_id="session-1", last_message="done", exit_code=0),
    )
    journal.record_terminal(terminal)

    recovered = ExecutionState(tmp_path, 3).load()
    assert recovered is not None
    assert recovered.state == "succeeded"
    assert recovered.result_digest
    assert recovered.terminal == terminal

    journal.acknowledge(assignment.job_id, JobState.DONE)
    retained = journal.load()
    assert retained is not None and retained.acknowledged


def test_flow_journals_preserve_previous_evidence_and_recover_current(tmp_path: Path) -> None:
    # Restart enumerates each Flow explicitly, regardless of job ordering or file timestamps.
    journal = ExecutionState(tmp_path, 3)
    journal.record_received(_assignment())
    journal.record_started()
    journal.complete(RuntimeError("first result"))
    journal.acknowledge(5, JobState.FAILED)
    first_path, first_bytes = journal.path, journal.path.read_bytes()
    assert first_path == tmp_path / "journals/flow-3.json"
    assert not (tmp_path / "assignment-journal.json").exists()

    journal = ExecutionState(tmp_path, 4)
    journal.record_received(_assignment(job_id=4, flow_id=4))
    journal.record_started()
    journal.complete(RuntimeError("second result"))
    journal.acknowledge(4, JobState.FAILED)
    assert first_path.read_bytes() == first_bytes
    first_path.write_bytes(first_bytes)  # Neither higher job ID nor newer mtime selects the owner.

    assert ExecutionState.scan(tmp_path) == [3, 4]
    restarted = ExecutionState(tmp_path, 4)
    recovery = restarted.recovery(executor_active=False)
    assert recovery is not None and recovery.job_id == 4
    assert restarted.path == tmp_path / "journals/flow-4.json"
    current = restarted.path.read_bytes()
    with pytest.raises(ExecutionStateError, match="acknowledgement"):
        restarted.acknowledge(5, JobState.FAILED)
    assert restarted.path.read_bytes() == current
    assert first_path.read_bytes() == first_bytes
    record = restarted.load()
    assert record is not None and record.root_identity[0] == str(tmp_path.resolve())


@pytest.mark.parametrize("phase", ["received", "started", "terminal", "stopped"])
@pytest.mark.parametrize("flow_id", [3, 4])
def test_pending_journal_cannot_be_replaced(tmp_path: Path, phase: str, flow_id: int) -> None:
    journal = ExecutionState(tmp_path, 3)
    record = journal.record_received(_assignment())
    if phase != "received":
        record = journal.record_started()
    if phase == "terminal":
        journal.complete(RuntimeError("result awaiting ACK"))
    elif phase == "stopped":
        assert record.execution_nonce is not None
        journal.record_stopped(ExecutionStopped(
            request_id="stop-1", job_id=5, flow_id=3, assignment_token="assignment-1",
            execution_nonce=record.execution_nonce,
        ))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    with pytest.raises(ExecutionStateError, match="unacknowledged" if flow_id == 3 else "Flow"):
        journal.record_received(_assignment(job_id=6, flow_id=flow_id))
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.json")} == before


@pytest.mark.parametrize("has_previous", [False, True])
def test_interrupted_flow_write_does_not_change_other_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_previous: bool,
) -> None:
    from app.worker.state import execution

    journal = ExecutionState(tmp_path, 3)
    if has_previous:
        journal.record_received(_assignment())
        journal.record_started()
        journal.complete(RuntimeError("previous result"))
        journal.acknowledge(5, JobState.FAILED)
    write = execution.write_private_record

    def fail_write(path: Path, payload: str) -> None:
        if path.name == "flow-4.json":
            raise OSError("journal write interrupted")
        write(path, payload)

    monkeypatch.setattr(execution, "write_private_record", fail_write)
    with pytest.raises(OSError, match="journal write interrupted"):
        ExecutionState(tmp_path, 4).record_received(_assignment(job_id=6, flow_id=4))
    restarted = ExecutionState(tmp_path, 3)
    recovery = restarted.recovery(executor_active=False)
    assert (recovery.job_id if recovery else None) == (5 if has_previous else None)
    monkeypatch.setattr(execution, "write_private_record", write)
    restarted = ExecutionState(tmp_path, 4)
    restarted.record_received(_assignment(job_id=6, flow_id=4))
    restarted.record_started()
    recovery = ExecutionState(tmp_path, 4).recovery(executor_active=False)
    assert recovery is not None and recovery.job_id == 6


@pytest.mark.parametrize("damage", ["corrupt", "filename", "pointer"])
def test_recovery_preserves_conflicting_journal_files(tmp_path: Path, damage: str) -> None:
    journal = ExecutionState(tmp_path, 3)
    journal.record_received(_assignment())
    journal.record_started()
    journal.complete(RuntimeError("first result"))
    journal.acknowledge(5, JobState.FAILED)
    first = journal.path
    journal = ExecutionState(tmp_path, 4)
    journal.record_received(_assignment(job_id=6, flow_id=4))
    journal.record_started()
    if damage == "corrupt":
        first.write_text("not-json")
    elif damage == "filename":
        first.rename(first.with_name("flow-03.json"))
    elif damage == "pointer":
        (tmp_path / "journals/current.json").write_text('{"flow_id": true}')
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    with pytest.raises(ExecutionStateError):
        ExecutionState.scan(tmp_path)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.json")} == before


def test_corrupt_journal_is_preserved_for_diagnosis(tmp_path: Path) -> None:
    path = tmp_path / "assignment-journal.json"
    path.write_text("not-json")

    with pytest.raises(ExecutionStateError, match="invalid worker journal"):
        ExecutionState(tmp_path, 3).load()

    assert path.read_text() == "not-json"


def _previous_journal(
    root: Path,
    *,
    success: bool = False,
    stopped: bool = False,
) -> dict[str, JsonValue]:
    # Literal v14 payloads keep the upgrade fixture independent of current message serializers.
    content: dict[str, JsonValue] = {"type": "failed", "error": "previous failure"}
    if success:
        content = {
            "type": "succeeded",
            "result": {
                "kind": "initial",
                "session_id": "session-1",
                "last_message": "done",
                "exit_code": 0,
                "failure_stage": None,
                "failure_detail": None,
                "vcs_backend": None,
                "branch": None,
                "commit_sha": None,
                "remote_sha": None,
                "pushed": False,
                "open_pr_call_id": None,
                "delivery_receipt": None,
            },
        }
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode())
    return {
        "version": 4,
        "job_id": 5,
        "flow_id": 3,
        "assignment_token": "assignment-1",
        "state": "succeeded" if success else "failed",
        "result_digest": digest.hexdigest(),
        "terminal": {
            **content,
            "protocol_version": 14,
            "job_id": 5,
            "assignment_token": "assignment-1",
            "connection_epoch": "old-epoch",
        },
        "root_identity": [str(root.resolve()), root.stat().st_dev, root.stat().st_ino],
        "execution_nonce": "execution-1",
        "acknowledged": False,
        "workspace": {
            "repository": "owner/repo",
            "slot_id": "a" * 32,
            "flow_id": 3,
            "generation": 2,
        },
        "stopped": {
            "protocol_version": 14,
            "type": "execution_stopped",
            "request_id": "stop-1",
            "job_id": 5,
            "flow_id": 3,
            "assignment_token": "assignment-1",
            "execution_nonce": "execution-1",
            "connection_epoch": "old-epoch",
        }
        if stopped
        else None,
    }


def test_legacy_layout_migration_preserves_previous_flow_bytes(tmp_path: Path) -> None:
    data = _previous_journal(tmp_path)
    data["acknowledged"] = True
    original = json.dumps(data)
    legacy = tmp_path / "assignment-journal.json"
    legacy.write_text(original)
    journal = ExecutionState(tmp_path, 3)
    assert journal.load() is not None
    assert legacy.read_text() == original and not (tmp_path / "journals").exists()

    journal = ExecutionState(tmp_path, 4)
    journal.record_received(_assignment(job_id=6, flow_id=4))
    assert not legacy.exists()
    assert (tmp_path / "journals/flow-3.json").read_text() == original
    assert journal.path == tmp_path / "journals/flow-4.json"
    recovery = ExecutionState(tmp_path, 4).recovery(executor_active=False)
    assert recovery is not None and recovery.job_id == 6


@pytest.mark.parametrize("conflict", [False, True])
def test_interrupted_legacy_move_can_resume_without_overwriting_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: bool,
) -> None:
    # Migration may be interrupted before rename; a pre-existing destination is never replaced.
    original = json.dumps(_previous_journal(tmp_path))
    legacy = tmp_path / "assignment-journal.json"
    legacy.write_text(original)
    rename = Path.rename

    def interrupt(source: Path, target: Path) -> Path:
        if source == legacy:
            raise OSError("migration interrupted")
        return rename(source, target)

    monkeypatch.setattr(Path, "rename", interrupt)
    with pytest.raises(OSError, match="migration interrupted"):
        ExecutionState(tmp_path, 3).acknowledge(5, JobState.FAILED)
    assert legacy.read_text() == original
    monkeypatch.setattr(Path, "rename", rename)
    resumed = ExecutionState(tmp_path, 3)
    if conflict:
        destination = tmp_path / "journals/flow-3.json"
        destination.write_text(original)
        with pytest.raises(ExecutionStateError, match="conflicts"):
            resumed.acknowledge(5, JobState.FAILED)
        assert legacy.read_text() == destination.read_text() == original
    else:
        assert resumed.load() is not None and legacy.read_text() == original
        resumed.acknowledge(5, JobState.FAILED)
        saved = resumed.load()
        assert saved is not None and saved.acknowledged and saved.version == 5
        assert not legacy.exists()


@pytest.mark.parametrize("success", [False, True])
@pytest.mark.parametrize("stopped", [False, True])
def test_predecessor_journal_recovers_and_upgrades_on_ack(
    tmp_path: Path,
    success: bool,
    stopped: bool,
) -> None:
    # An ordinary package upgrade leaves v14 results/stop receipts inside a still-v4 journal.
    data = _previous_journal(tmp_path, success=success, stopped=stopped)
    original = json.dumps(data)
    session = ExecutionState(tmp_path, 3)
    legacy = tmp_path / "assignment-journal.json"
    legacy.write_text(original)

    recovery = session.recovery(executor_active=False)
    assert recovery is not None
    assert (recovery.job_id, recovery.assignment_token, recovery.result_digest) == (
        5,
        "assignment-1",
        data["result_digest"],
    )
    assert recovery.stop_request_id == ("stop-1" if stopped else None)
    replay = session.replay()
    assert "connection_epoch" not in replay.model_dump()
    assert terminal_digest(replay) == data["result_digest"]
    assert legacy.read_text() == original
    with pytest.raises(ValidationError):
        parse_worker_message(json.dumps(data["terminal"]))

    if stopped:
        session.acknowledge_stop(
            StopFence(
                request_id="stop-1",
                job_id=5,
                flow_id=3,
                assignment_token="assignment-1",
            )
        )
    else:
        session.acknowledge(5, JobState.DONE if success else JobState.FAILED)
    saved = session.load()
    assert saved is not None and saved.acknowledged and saved.terminal is not None
    assert saved.version == 5
    if stopped:
        assert saved.stopped is not None
    persisted = json.loads(session.path.read_text())
    for name in ("terminal", "stopped"):
        if persisted[name] is not None:
            assert "protocol_version" not in persisted[name]
            assert "connection_epoch" not in persisted[name]
    assert persisted["version"] == 5
    for key in (
        "job_id",
        "flow_id",
        "assignment_token",
        "root_identity",
        "workspace",
        "execution_nonce",
        "result_digest",
        "state",
    ):
        assert persisted[key] == data[key]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 3),
        ("version", 5),
        ("terminal.protocol_version", 13),
        ("terminal.protocol_version", 15),
        ("stopped.protocol_version", 16),
        ("stopped.protocol_version", None),
        ("terminal.job_id", 6),
        ("terminal.assignment_token", "other"),
        ("stopped.flow_id", 4),
        ("stopped.execution_nonce", "other"),
        ("result_digest", "changed"),
        ("result_digest", None),
        ("root_identity", ["other-root", 0, 0]),
        ("unexpected", True),
    ],
)
def test_invalid_predecessor_journal_is_preserved(
    tmp_path: Path,
    field: str,
    value: JsonValue,
) -> None:
    data = _previous_journal(tmp_path, stopped=True)
    target = data
    *parents, key = field.split(".")
    for parent in parents:
        child = target[parent]
        assert isinstance(child, dict)
        target = child
    target[key] = value
    original = json.dumps(data)
    session = ExecutionState(tmp_path, 3)
    legacy = tmp_path / "assignment-journal.json"
    legacy.write_text(original)

    with pytest.raises(ExecutionStateError):
        session.load()

    assert legacy.read_text() == original


async def test_owner_reuses_tool_future_and_matches_original_item(tmp_path: Path) -> None:
    # A reconnect borrows the same item; an MCP retry may reuse call_id but not the item ID.
    state = ExecutionState(tmp_path, 3)
    executor = Mock(spec=TaskExecutor)
    blocked = asyncio.Event()

    async def execute(*args: object, **kwargs: object) -> ExecutionResult:
        await blocked.wait()
        return InitialTaskResult(session_id="s", last_message="done", exit_code=0)

    executor.execute.side_effect = execute
    owner = WorkerExecution(worker_id="worker", executor=executor, state=state)
    owner.receive(_assignment())
    owner.start()
    outcome = VcsOutcome(backend=VcsKind.GIT, branch="flow/3", commit_sha="abc", remote_sha="abc")
    first = asyncio.create_task(
        owner.call_tool("flow.open_pr", {}, call_id="call", vcs_outcome=outcome)
    )
    second: asyncio.Task[DeliveryReceipt] | None = None
    try:
        pending = await asyncio.wait_for(owner.next_tool_call(), 1)
        assert UUID(hex=pending.request.message_id).version == 4
        assert pending.request.reply_to is None
        owner.resume(RecoveryDecision(action="resume", job_id=5, assignment_token="assignment-1"))
        assert await owner.next_tool_call() is pending
        receipt = PreparedDelivery(head_sha="abc", delivery_version=1)
        response = FlowToolResponse(
            type="tool_result",
            message_id=uuid4().hex,
            reply_to=pending.request.message_id,
            method=pending.request.method,
            call_id="call",
            result=receipt,
        )
        for field, value in (
            ("method", "flow.other"),
            ("reply_to", uuid4().hex),
            ("call_id", "other"),
        ):
            with pytest.raises(WorkerProtocolError, match="does not match"):
                owner.finish_tool_call(pending, response.model_copy(update={field: value}))
            assert not pending.future.done()
            assert await owner.next_tool_call() is pending
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(
            owner.call_tool("flow.open_pr", {}, call_id="call", vcs_outcome=outcome)
        )
        retry = await asyncio.wait_for(owner.next_tool_call(), 1)
        assert retry.request.message_id != pending.request.message_id
        assert retry.request.arguments.call_id == pending.request.arguments.call_id
        owner.finish_tool_call(pending, response)
        assert not retry.future.done()
        with pytest.raises(WorkerProtocolError, match="does not match"):
            owner.finish_tool_call(retry, response)
        owner.finish_tool_call(
            retry, response.model_copy(update={"reply_to": retry.request.message_id})
        )
        assert await second == receipt
    finally:
        first.cancel()
        if second is not None:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        await asyncio.gather(first, return_exceptions=True)
        await owner.shutdown()


async def test_registration_result_is_withheld_during_stop_and_shutdown(tmp_path: Path) -> None:
    # A disconnected stop observer cannot abandon the join or accept a late result during it.
    state = ExecutionState(tmp_path, 3)
    executor = Mock(spec=TaskExecutor)
    entered, joining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            joining.set()
            await release.wait()

    executor.execute.side_effect = execute
    owner = WorkerExecution(worker_id="worker", executor=executor, state=state)
    owner.receive(_assignment())
    owner.start()
    await asyncio.wait_for(entered.wait(), 1)
    fence = StopFence(request_id="stop", flow_id=3, job_id=5, assignment_token="assignment-1")
    observer = asyncio.create_task(owner.stop(fence))
    try:
        await asyncio.wait_for(joining.wait(), 1)
        record = state.load()
        assert record is not None and record.stopped is None and owner.may_modify_workspace()
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert owner.result_for_registration() is None
        with pytest.raises(WorkerProtocolError, match="does not match"):
            owner.on_result_accepted(
                ResultAcceptance(job_id=5, assignment_token="assignment-1", state="failed")
            )
        with pytest.raises(WorkerProtocolError, match="conflicts"):
            await owner.stop(fence.model_copy(update={"request_id": "other"}))
        release.set()
        receipt = await owner.stop(StopFence.model_validate(fence.model_dump()))
        assert await owner.stop(fence) is receipt
        executor.execute.assert_awaited_once()
        record = state.load()
        assert record is not None and record.stopped == receipt and not owner.may_modify_workspace()
        restarted = WorkerExecution(
            worker_id="worker", executor=executor, state=ExecutionState(tmp_path, 3)
        )
        assert restarted.result_for_registration() is None
        owner.on_stop_accepted(fence)
        assert state.assignment is None
    finally:
        release.set()
        await asyncio.gather(observer, return_exceptions=True)
        await owner.shutdown()
    other = ExecutionState(tmp_path / "shutdown", 3)
    other.record_received(_assignment())
    other.record_started()
    other.complete(RuntimeError("finished"))
    shutting_down = WorkerExecution(worker_id="worker", executor=executor, state=other)
    await shutting_down.shutdown()
    assert shutting_down.result_for_registration() is None
