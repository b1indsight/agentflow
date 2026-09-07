from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from pydantic.types import SecretStr

from app.models import TriageVerdict, VcsKind, Verdict
from app.protocol import (
    PROTOCOL_VERSION,
    Assignment,
    AssignmentSecret,
    FlowToolErrorKind,
    FlowToolRequest,
    FlowToolResponse,
    InitialResult,
    InitialTask,
    PushSecret,
    RecoveryDescriptor,
    Register,
    ResumeAssignment,
    Succeeded,
    parse_server_message,
    parse_worker_message,
)


def test_assignment_and_result_round_trip_with_discriminated_payloads() -> None:
    assert PROTOCOL_VERSION == 7
    assignment = Assignment(
        job_id=41,
        flow_id=7,
        assignment_token="assignment-1",
        connection_epoch="epoch-1",
        task=InitialTask(
            flow_id=7,
            attempt_id=13,
            repo="owner/repo",
            issue_number=9,
            issue_title="Add a bound",
            issue_body="Keep retries finite.",
            branch="flow/7",
            base_branch="trunk",
            remote="origin",
            repo_identity="worker-1:owner/repo",
        ),
        secret=AssignmentSecret(
            push=PushSecret(
                token=SecretStr("push-token"),
                repository="owner/repo",
                expires_at=datetime(2026, 9, 5, 1, tzinfo=UTC),
            )
        ),
    )

    parsed = parse_server_message(assignment.model_dump_json())

    assert isinstance(parsed, Assignment)
    assert isinstance(parsed.task, InitialTask)
    assert parsed.task.branch == "flow/7"
    assert parsed.task.base_branch == "trunk"
    assert parsed.secret is not None
    assert parsed.secret.push.token.get_secret_value() == "push-token"
    assert "push-token" not in repr(parsed)

    succeeded = Succeeded(
        job_id=41,
        assignment_token="assignment-1",
        connection_epoch="epoch-1",
        result=InitialResult(session_id="session-1", last_message="done", exit_code=0),
    )
    worker_message = parse_worker_message(succeeded.model_dump_json())
    assert isinstance(worker_message, Succeeded)
    assert isinstance(worker_message.result, InitialResult)


def test_flow_tool_messages_are_strict_and_fenced() -> None:
    request = FlowToolRequest(
        job_id=5,
        flow_id=3,
        attempt_id=7,
        assignment_token="assignment-1",
        connection_epoch="epoch-1",
        call_id="call-1",
        name="flow.open_pr",
        arguments={"title": "Fix it", "body": "Implemented."},
        vcs_backend=VcsKind.GIT,
        branch="flow/3",
        commit_sha="abc",
        remote_sha="abc",
    )
    parsed = parse_worker_message(request.model_dump_json())

    assert isinstance(parsed, FlowToolRequest)
    response = FlowToolResponse(
        call_id="call-1",
        result={
            "pr_number": 17,
            "url": "https://github.com/owner/repo/pull/17",
            "head_sha": "abc",
            "delivery_version": 1,
        },
    )
    assert isinstance(parse_server_message(response.model_dump_json()), FlowToolResponse)

    failure = FlowToolResponse(
        call_id="call-2",
        error="GitHub throttled the request",
        error_kind=FlowToolErrorKind.RATE_LIMITED,
        retryable=True,
        retry_after="30",
    )
    assert parse_server_message(failure.model_dump_json()) == failure


def test_protocol_rejects_unknown_versions_and_result_shapes() -> None:
    with pytest.raises(ValidationError):
        parse_worker_message(
            Register(worker_id="worker-1").model_dump_json().replace(
                f'"protocol_version":{PROTOCOL_VERSION}', '"protocol_version":99'
            )
        )

    with pytest.raises(ValidationError, match="error kind"):
        FlowToolResponse(call_id="call-1", error="unclassified")

    with pytest.raises(ValidationError, match="exactly one"):
        FlowToolResponse(
            call_id="call-1",
            result={"pr_number": 17},
            error="conflict",
            error_kind=FlowToolErrorKind.CONFLICT,
        )

    verdict = TriageVerdict(verdict=Verdict.WAIT, confidence=0.2)
    with pytest.raises(ValidationError):
        Succeeded.model_validate(
            {
                "job_id": 1,
                "assignment_token": "token",
                "connection_epoch": "epoch-1",
                "result": {"kind": "triage", "verdict": verdict.model_dump(), "extra": True},
            }
        )


def test_recovery_descriptor_and_resume_message_round_trip() -> None:
    register = Register(
        worker_id="worker-1",
        recovery=RecoveryDescriptor(
            job_id=41,
            assignment_token="assignment-1",
            state="started",
            executor_active=True,
        ),
    )

    parsed_register = parse_worker_message(register.model_dump_json())
    assert isinstance(parsed_register, Register)
    assert parsed_register.recovery is not None
    assert parsed_register.recovery.executor_active is True

    resumed = parse_server_message(
        ResumeAssignment(
            job_id=41,
            assignment_token="assignment-1",
            connection_epoch="epoch-2",
            heartbeat_interval=10,
        ).model_dump_json()
    )
    assert isinstance(resumed, ResumeAssignment)
    assert resumed.connection_epoch == "epoch-2"
