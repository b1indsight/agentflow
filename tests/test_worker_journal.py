from pathlib import Path

import pytest

from app.models import JobState
from app.protocol import Assignment, InitialResult, InitialTask, Succeeded
from app.worker.journal import JournalError, WorkerJournal


def _assignment() -> Assignment:
    return Assignment(
        job_id=5,
        flow_id=3,
        assignment_token="assignment-1",
        connection_epoch="epoch-1",
        task=InitialTask(
            attempt_id=7,
            repo="owner/repo",
            issue_number=8,
            issue_title="Issue",
            issue_body="Body",
            branch="flow/issue-8",
        ),
    )


def test_journal_persists_terminal_result_until_ack(tmp_path: Path) -> None:
    journal = WorkerJournal(tmp_path)
    assignment = _assignment()
    journal.record_received(assignment)
    journal.record_started()
    terminal = Succeeded(
        job_id=assignment.job_id,
        assignment_token=assignment.assignment_token,
        connection_epoch=assignment.connection_epoch,
        result=InitialResult(session_id="session-1", last_message="done", exit_code=0),
    )
    journal.record_terminal(terminal)

    recovered = WorkerJournal(tmp_path).load()
    assert recovered is not None
    assert recovered.state == "succeeded"
    assert recovered.result_digest
    assert recovered.terminal == terminal

    journal.acknowledge(assignment.job_id, JobState.DONE)
    assert journal.load() is None


def test_corrupt_journal_is_preserved_for_diagnosis(tmp_path: Path) -> None:
    path = tmp_path / "assignment-journal.json"
    path.write_text("not-json")

    with pytest.raises(JournalError, match="invalid worker journal"):
        WorkerJournal(tmp_path).load()

    assert path.read_text() == "not-json"
