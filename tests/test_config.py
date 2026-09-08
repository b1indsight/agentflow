from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.config import ServeSettings, WorkerSettings
from app.worker.runtime import validate_worker_environment


@pytest.mark.parametrize("labels", [
    {"done_label": ""}, {"in_review_label": "  "}, {"trigger_label": ""},
    {"done_label": "FLOW"}, {"done_label": "Flow:In-Review"},
    {"in_review_label": "FLOW"},
])
def test_archive_status_labels_cannot_be_empty_or_conflict(labels):
    with pytest.raises(ValidationError, match="label"):
        ServeSettings(_env_file=None, database_url="postgresql://localhost/test",
                      worker_tokens={"worker": SecretStr("secret")}, **labels)


def test_judge_credentials_stay_out_of_config_serialization_and_code_agent_environment(monkeypatch):
    from app.codex_driver import _codex_environment

    monkeypatch.setenv("FLOWTOOL_JUDGE_MODEL", "openai:test")
    monkeypatch.setenv("FLOWTOOL_JUDGE_API_KEY", "judge-secret")
    configured = WorkerSettings(worker_id="worker", worker_token=SecretStr("worker-secret"),
                                serve_ws_url="wss://example.test/ws")
    assert configured.judge_model == "openai:test"
    assert configured.judge_api_key.get_secret_value() == "judge-secret"
    assert "judge-secret" not in configured.model_dump_json()
    assert "FLOWTOOL_JUDGE_API_KEY" not in _codex_environment()


def test_settings_parse_csv_allowlist_and_expand_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLOWTOOL_ALLOWLIST", "alice, bob ,carol")
    monkeypatch.setenv(
        "FLOWTOOL_DATABASE_URL", "postgresql://flowtool:secret@db.example/flowtool"
    )

    monkeypatch.setenv("FLOWTOOL_WORKER_TOKENS", '{"worker-1":"secret"}')

    settings = ServeSettings()  # type: ignore[call-arg]

    assert settings.allowlist == ["alice", "bob", "carol"]
    assert settings.database_url == "postgresql://flowtool:secret@db.example/flowtool"


def test_settings_preserve_json_allowlist_environment_values(monkeypatch) -> None:
    # pydantic-settings historically accepted JSON arrays for list-valued env vars.
    monkeypatch.setenv("FLOWTOOL_ALLOWLIST", '["alice", "bob"]')
    monkeypatch.setenv("FLOWTOOL_DATABASE_URL", "postgresql://localhost/flowtool")

    monkeypatch.setenv("FLOWTOOL_WORKER_TOKENS", '{"worker-1":"secret"}')

    settings = ServeSettings()  # type: ignore[call-arg]

    assert settings.allowlist == ["alice", "bob"]


def test_reviewers_are_configured_independently_from_trigger_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("FLOWTOOL_ALLOWLIST", "alice,bob")
    monkeypatch.setenv("FLOWTOOL_REVIEWERS", "carol,dave")
    monkeypatch.setenv("FLOWTOOL_DATABASE_URL", "postgresql://localhost/flowtool")
    monkeypatch.setenv("FLOWTOOL_WORKER_TOKENS", '{"worker-1":"secret"}')

    settings = ServeSettings()  # type: ignore[call-arg]

    assert settings.allowlist == ["alice", "bob"]
    assert settings.reviewers == ["carol", "dave"]
    assert settings.in_review_label == "flow:in-review"


def test_settings_require_database_url(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLOWTOOL_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        ServeSettings(worker_tokens={"worker-1": SecretStr("secret")})  # type: ignore[call-arg]


def test_serve_settings_require_unique_nonempty_worker_credentials(monkeypatch) -> None:
    monkeypatch.setenv("FLOWTOOL_DATABASE_URL", "postgresql://localhost/flowtool")

    with pytest.raises(ValidationError):
        ServeSettings(
            database_url="postgresql://localhost/flowtool",
            worker_tokens={"": SecretStr("secret")},
        )
    with pytest.raises(ValidationError):
        ServeSettings(
            database_url="postgresql://localhost/flowtool",
            worker_tokens={"worker-1": SecretStr("")},
        )
    with pytest.raises(ValidationError):
        ServeSettings(
            database_url="postgresql://localhost/flowtool",
            worker_tokens={
                "worker-1": SecretStr("shared"),
                "worker-2": SecretStr("shared"),
            }
        )


def test_worker_settings_do_not_require_control_plane_secrets(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLOWTOOL_DATABASE_URL", raising=False)
    monkeypatch.delenv("FLOWTOOL_GITHUB_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("FLOWTOOL_WORKER_ROOT", raising=False)
    monkeypatch.delenv("FLOWTOOL_ALLOW_INSECURE_WORKER_WS", raising=False)

    settings = WorkerSettings(
        worker_id="worker-1",
        worker_token=SecretStr("secret"),
        serve_ws_url="wss://control.example/internal/workers/connect",
    )

    assert settings.worker_root == Path("~/.flowtool/workers/worker-1").expanduser()
    assert settings.trusted_host_execution is False
    assert not hasattr(settings, "database_url")


def test_worker_trusted_host_execution_requires_explicit_opt_in() -> None:
    settings = WorkerSettings(
        worker_id="worker-1",
        worker_token=SecretStr("secret"),
        serve_ws_url="wss://control.example/internal/workers/connect",
        trusted_host_execution=True,
        execution_isolation="dedicated-account",
    )

    assert settings.trusted_host_execution is True


def test_worker_rejects_full_access_without_external_isolation() -> None:
    with pytest.raises(ValidationError, match="execution isolation"):
        WorkerSettings(
            worker_id="worker-1",
            worker_token=SecretStr("secret"),
            serve_ws_url="wss://control.example/internal/workers/connect",
            trusted_host_execution=True,
        )


def test_full_access_worker_rejects_control_plane_environment() -> None:
    settings = WorkerSettings(
        worker_id="worker-1",
        worker_token=SecretStr("secret"),
        serve_ws_url="wss://control.example/internal/workers/connect",
        trusted_host_execution=True,
        execution_isolation="container",
    )

    with pytest.raises(RuntimeError, match="control-plane secrets"):
        validate_worker_environment(
            settings,
            {"FLOWTOOL_DATABASE_URL": "postgresql://secret"},
        )


def test_worker_settings_do_not_read_the_control_plane_dotenv(
    monkeypatch, tmp_path: Path
) -> None:
    for key in ("FLOWTOOL_WORKER_ID", "FLOWTOOL_WORKER_TOKEN", "FLOWTOOL_SERVE_WS_URL"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "FLOWTOOL_WORKER_ID=worker-1\n"
        "FLOWTOOL_WORKER_TOKEN=secret\n"
        "FLOWTOOL_SERVE_WS_URL=wss://control.example/internal/workers/connect\n"
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError):
        WorkerSettings()  # type: ignore[call-arg]


def test_worker_requires_explicit_opt_in_for_plaintext_websocket(monkeypatch) -> None:
    monkeypatch.delenv("FLOWTOOL_ALLOW_INSECURE_WORKER_WS", raising=False)

    with pytest.raises(ValidationError):
        WorkerSettings(
            worker_id="worker-1",
            worker_token=SecretStr("secret"),
            serve_ws_url="ws://localhost:8000/internal/workers/connect",
        )

    settings = WorkerSettings(
        worker_id="worker-1",
        worker_token=SecretStr("secret"),
        serve_ws_url="ws://localhost:8000/internal/workers/connect",
        allow_insecure_worker_ws=True,
    )
    assert settings.serve_ws_url.startswith("ws://")


def test_recovery_timing_settings_require_safe_ordering(monkeypatch) -> None:
    monkeypatch.setenv("FLOWTOOL_DATABASE_URL", "postgresql://localhost/flowtool")

    with pytest.raises(ValidationError, match="lease duration"):
        ServeSettings(
            database_url="postgresql://localhost/flowtool",
            worker_tokens={"worker-1": SecretStr("secret")},
            worker_heartbeat_interval=10,
            worker_lease_duration=20,
        )
    with pytest.raises(ValidationError, match="recovery grace"):
        ServeSettings(
            database_url="postgresql://localhost/flowtool",
            worker_tokens={"worker-1": SecretStr("secret")},
            worker_heartbeat_interval=10,
            worker_heartbeat_timeout=30,
            worker_lease_duration=31,
            worker_recovery_grace=30,
        )


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_github_request_timeout_rejects_nonpositive_or_nonfinite_values(
    timeout: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLOWTOOL_GITHUB_REQUEST_TIMEOUT_SECONDS", timeout)
    with pytest.raises(ValidationError, match="github_request_timeout_seconds"):
        ServeSettings(
            database_url="postgresql://localhost/flowtool",
            worker_tokens={"worker-1": SecretStr("secret")},
        )


def test_worker_reconnect_backoff_requires_a_valid_range() -> None:
    with pytest.raises(ValidationError, match="reconnect"):
        WorkerSettings(
            worker_id="worker-1",
            worker_token=SecretStr("secret"),
            serve_ws_url="ws://localhost:8000/internal/workers/connect",
            allow_insecure_worker_ws=True,
            reconnect_min_backoff=5,
            reconnect_max_backoff=1,
        )


def test_missing_worker_id_is_reported_as_a_settings_error(monkeypatch) -> None:
    monkeypatch.delenv("FLOWTOOL_WORKER_ID", raising=False)

    with pytest.raises(ValidationError):
        WorkerSettings(
            worker_token=SecretStr("secret"),
            serve_ws_url="wss://control.example/internal/workers/connect",
        )  # type: ignore[call-arg]
