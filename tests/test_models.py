from app.domain.models import InitialCodingOutput


def test_initial_codex_result_has_a_strict_required_schema() -> None:
    schema = InitialCodingOutput.model_json_schema()

    assert set(schema["required"]) == {"summary", "vcs_backend_used"}
    assert schema["additionalProperties"] is False
