from datetime import datetime

from app.models import (
    CodexResult,
    InitialCodexResult,
    Job,
    JobKind,
    JobState,
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


def test_codex_result_parses_review_outcomes() -> None:
    result = CodexResult.model_validate_json(
        """{
            "summary": "Updated the retry policy.",
            "vcs_backend_used": "git",
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
