from datetime import datetime

import pytest
from pydantic import ValidationError

from app.models import (
    CodexResult,
    DeliveryState,
    FlowDelivery,
    FlowExecution,
    FlowState,
    InitialCodexResult,
    Job,
    JobKind,
    JobState,
    TriageVerdict,
    Verdict,
)


def test_persisted_model_validates_database_values() -> None:
    job = Job.model_validate(
        {
            "id": 7,
            "flow_id": 3,
            "kind": "initial",
            "state": "queued",
            "created_at": "2026-08-28T09:30:00+00:00",
            "updated_at": "2026-08-28T09:31:00+00:00",
        }
    )

    assert job.kind is JobKind.INITIAL
    assert job.state is JobState.QUEUED
    assert job.created_at == datetime.fromisoformat("2026-08-28T09:30:00+00:00")


def test_triage_verdict_validates_known_verdicts() -> None:
    verdict = TriageVerdict.model_validate(
        {"verdict": "revise", "confidence": 0.8, "instruction": "Add a timeout."}
    )

    assert verdict.verdict is Verdict.REVISE
    assert verdict.instruction == "Add a timeout."

    with pytest.raises(ValidationError):
        TriageVerdict.model_validate({"verdict": "merge"})


def test_codex_result_parses_review_outcomes() -> None:
    result = CodexResult.model_validate_json(
        """{
            "summary": "Updated the retry policy.",
            "addressed": [{"item_id": 1, "version": 1, "comment_id": 12, "note": "Added a bound."}],
            "deferred": [{"item_id": 2, "version": 1, "comment_id": 13, "reason": "Needs product input."}],
            "needs_clarification": ["Which timeout should be the default?"]
        }"""
    )

    assert result.addressed[0].comment_id == 12
    assert result.deferred[0].comment_id == 13
    assert result.needs_clarification == ["Which timeout should be the default?"]


def test_initial_codex_result_has_a_strict_required_schema() -> None:
    schema = InitialCodexResult.model_json_schema()

    assert set(schema["required"]) == {"summary", "vcs_backend_used"}
    assert schema["additionalProperties"] is False


def test_flow_lifecycle_is_separate_from_execution_and_delivery() -> None:
    assert set(FlowState) == {
        FlowState.ACTIVE,
        FlowState.SUCCEEDED,
        FlowState.FAILED,
        FlowState.CANCELLED,
    }
    execution = FlowExecution(
        flow_id=3,
        worker_id="worker-a",
        repo_identity="worker-a:owner/repo",
    )
    delivery = FlowDelivery(flow_id=3, branch="flow/3", state=DeliveryState.NONE)

    assert execution.released_at is None
    assert delivery.pr_number is None
    assert delivery.state is DeliveryState.NONE
