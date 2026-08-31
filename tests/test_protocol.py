import pytest
from pydantic import ValidationError

from app.models import TriageVerdict, Verdict
from app.protocol import (
    PROTOCOL_VERSION,
    Assignment,
    InitialResult,
    InitialTask,
    Register,
    Succeeded,
    parse_server_message,
    parse_worker_message,
)


def test_assignment_and_result_round_trip_with_discriminated_payloads() -> None:
    assignment = Assignment(
        job_id=41,
        flow_id=7,
        assignment_token="assignment-1",
        task=InitialTask(
            attempt_id=13,
            repo="owner/repo",
            issue_number=9,
            issue_title="Add a bound",
            issue_body="Keep retries finite.",
            branch="flow/issue-9",
        ),
    )

    parsed = parse_server_message(assignment.model_dump_json())

    assert isinstance(parsed, Assignment)
    assert isinstance(parsed.task, InitialTask)
    assert parsed.task.branch == "flow/issue-9"

    succeeded = Succeeded(
        job_id=41,
        assignment_token="assignment-1",
        result=InitialResult(session_id="session-1", last_message="done", exit_code=0),
    )
    worker_message = parse_worker_message(succeeded.model_dump_json())
    assert isinstance(worker_message, Succeeded)
    assert isinstance(worker_message.result, InitialResult)


def test_protocol_rejects_unknown_versions_and_result_shapes() -> None:
    with pytest.raises(ValidationError):
        parse_worker_message(
            Register(worker_id="worker-1").model_dump_json().replace(
                f'"protocol_version":{PROTOCOL_VERSION}', '"protocol_version":99'
            )
        )

    verdict = TriageVerdict(verdict=Verdict.WAIT, confidence=0.2)
    with pytest.raises(ValidationError):
        Succeeded.model_validate(
            {
                "job_id": 1,
                "assignment_token": "token",
                "result": {"kind": "triage", "verdict": verdict.model_dump(), "extra": True},
            }
        )
