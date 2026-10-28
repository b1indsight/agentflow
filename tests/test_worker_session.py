import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import JsonValue, SecretStr, ValidationError

from app.contracts.protocol import (
    Assignment,
    AssignmentSecret,
    InitialTask,
    InitialTaskResult,
    PushSecret,
    StopAccepted,
    Succeeded,
    parse_worker_message,
    terminal_digest,
)
from app.domain.models import JobState
from app.worker.state.execution import ExecutionState, ExecutionStateError


def _assignment() -> Assignment:
    return Assignment(
        job_id=5,
        flow_id=3,
        assignment_token="assignment-1",
        connection_epoch="epoch-1",
        task=InitialTask(
            flow_id=3,
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
    journal = ExecutionState(tmp_path)
    assignment = _assignment()
    journal.record_received(assignment)
    assert "push-secret" not in journal.path.read_text()
    journal.record_started()
    terminal = Succeeded(
        job_id=assignment.job_id,
        assignment_token=assignment.assignment_token,
        connection_epoch=assignment.connection_epoch,
        result=InitialTaskResult(session_id="session-1", last_message="done", exit_code=0),
    )
    journal.record_terminal(terminal)

    recovered = ExecutionState(tmp_path).load()
    assert recovered is not None
    assert recovered.state == "succeeded"
    assert recovered.result_digest
    assert recovered.terminal == terminal

    journal.acknowledge(assignment.job_id, JobState.DONE)
    retained = journal.load()
    assert retained is not None and retained.acknowledged


def test_corrupt_journal_is_preserved_for_diagnosis(tmp_path: Path) -> None:
    path = tmp_path / "assignment-journal.json"
    path.write_text("not-json")

    with pytest.raises(ExecutionStateError, match="invalid worker journal"):
        ExecutionState(tmp_path).load()

    assert path.read_text() == "not-json"


def _previous_journal(
    root: Path, *, success: bool = False, stopped: bool = False,
) -> dict[str, JsonValue]:
    # Literal v14 payloads keep the upgrade fixture independent of current message serializers.
    content: dict[str, JsonValue] = {"type": "failed", "error": "previous failure"}
    if success:
        content = {"type": "succeeded", "result": {
            "kind": "initial", "session_id": "session-1", "last_message": "done", "exit_code": 0,
            "failure_stage": None, "failure_detail": None, "vcs_backend": None, "branch": None,
            "commit_sha": None, "remote_sha": None, "pushed": False, "open_pr_call_id": None,
            "delivery_receipt": None,
        }}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode())
    return {
        "version": 4, "job_id": 5, "flow_id": 3, "assignment_token": "assignment-1",
        "state": "succeeded" if success else "failed", "result_digest": digest.hexdigest(),
        "terminal": {**content, "protocol_version": 14, "job_id": 5,
                     "assignment_token": "assignment-1", "connection_epoch": "old-epoch"},
        "root_identity": [str(root.resolve()), root.stat().st_dev, root.stat().st_ino],
        "execution_nonce": "execution-1", "acknowledged": False,
        "workspace": {"repository": "owner/repo", "slot_id": "a" * 32,
                      "flow_id": 3, "generation": 2},
        "stopped": {"protocol_version": 14, "type": "execution_stopped", "request_id": "stop-1",
                    "job_id": 5, "flow_id": 3, "assignment_token": "assignment-1",
                    "execution_nonce": "execution-1", "connection_epoch": "old-epoch"}
        if stopped else None,
    }


@pytest.mark.parametrize("success", [False, True])
@pytest.mark.parametrize("stopped", [False, True])
def test_predecessor_journal_recovers_and_upgrades_on_ack(
    tmp_path: Path, success: bool, stopped: bool,
) -> None:
    # An ordinary package upgrade leaves v14 results/stop receipts inside a still-v4 journal.
    data = _previous_journal(tmp_path, success=success, stopped=stopped)
    original = json.dumps(data)
    session = ExecutionState(tmp_path)
    session.path.write_text(original)

    recovery = session.recovery(executor_active=False)
    assert recovery is not None
    assert (recovery.job_id, recovery.assignment_token, recovery.result_digest) == (
        5, "assignment-1", data["result_digest"],
    )
    assert recovery.stop_request_id == ("stop-1" if stopped else None)
    replay = session.replay("new-epoch")
    assert parse_worker_message(replay.model_dump_json()) == replay
    assert replay.connection_epoch == "new-epoch"
    assert terminal_digest(replay) == data["result_digest"]
    assert session.path.read_text() == original
    with pytest.raises(ValidationError):
        parse_worker_message(json.dumps(data["terminal"]))

    if stopped:
        session.acknowledge_stop(StopAccepted(
            request_id="stop-1", job_id=5, flow_id=3, assignment_token="assignment-1",
            connection_epoch="new-epoch",
        ))
    else:
        session.acknowledge(5, JobState.DONE if success else JobState.FAILED)
    saved = session.load()
    assert saved is not None and saved.acknowledged and saved.terminal is not None
    assert saved.terminal.protocol_version == 15
    if stopped:
        assert saved.stopped is not None and saved.stopped.protocol_version == 15
    persisted = json.loads(session.path.read_text())
    assert persisted["terminal"]["protocol_version"] == 15
    for key in ("version", "job_id", "flow_id", "assignment_token", "root_identity",
                "workspace", "execution_nonce", "result_digest", "state"):
        assert persisted[key] == data[key]


@pytest.mark.parametrize(("field", "value"), [
    ("version", 3), ("version", 5),
    ("terminal.protocol_version", 13), ("terminal.protocol_version", 15),
    ("stopped.protocol_version", 16), ("stopped.protocol_version", None),
    ("terminal.job_id", 6), ("terminal.assignment_token", "other"),
    ("stopped.flow_id", 4), ("stopped.execution_nonce", "other"),
    ("result_digest", "changed"), ("result_digest", None),
    ("root_identity", ["other-root", 0, 0]), ("unexpected", True),
])
def test_invalid_predecessor_journal_is_preserved(
    tmp_path: Path, field: str, value: JsonValue,
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
    session = ExecutionState(tmp_path)
    session.path.write_text(original)

    with pytest.raises(ExecutionStateError):
        session.load()

    assert session.path.read_text() == original
