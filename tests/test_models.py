import pytest
from pydantic import ValidationError

from app.models import CodexResult, TriageVerdict, Verdict


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
            "addressed": [{"comment_id": 12, "note": "Added a bound."}],
            "deferred": [{"comment_id": 13, "reason": "Needs product input."}],
            "needs_clarification": ["Which timeout should be the default?"]
        }"""
    )

    assert result.addressed[0].comment_id == 12
    assert result.deferred[0].comment_id == 13
    assert result.needs_clarification == ["Which timeout should be the default?"]
