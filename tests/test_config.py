from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.controler.config import ServerSettings
from app.worker.config import WorkerSettings
from app.worker.execution.codex import _codex_environment
from app.worker.runtime import validate_worker_environment


@pytest.mark.parametrize("changes", [
    {"access_owner_email": None},
    {"access_issuer": "http://test.cloudflareaccess.com"},
    {"access_issuer": "https://attacker.example"},
    {"access_issuer": "https://test.cloudflareaccess.com/path"},
    {"access_issuer": "https://user@test.cloudflareaccess.com"},
    {"access_audience": "app,other"},
    {"access_owner_email": "owner"},
    {"access_owner_email": "a@example.com,b@example.com"},
])
def test_access_requires_complete_single_owner_configuration(changes: dict[str, str | None],
                                                           monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    values: dict[str, str | None] = {"access_issuer": "https://test.cloudflareaccess.com", "access_audience": "app",
              "access_owner_email": "owner@example.com"}
    values.update(changes)
    with pytest.raises(ValidationError, match="Access"):
        ServerSettings(database_url="postgresql://localhost/test",
                       worker_tokens={"worker": SecretStr("secret")},
                       access_issuer=values["access_issuer"], access_audience=values["access_audience"],
                       access_owner_email=values["access_owner_email"])


def test_blank_access_configuration_disables_user_api(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    settings = ServerSettings(database_url="postgresql://localhost/test",
                              worker_tokens={"worker": SecretStr("secret")}, access_issuer="",
                              access_audience=" ", access_owner_email="")
    assert settings.access_issuer is settings.access_audience is settings.access_owner_email is None
    assert "control_api_token" not in ServerSettings.model_fields


@pytest.mark.parametrize("labels", [
    {"in_review_label": "  "}, {"done_label": "FLOW"},
], ids=["blank-label", "label-collision"])
def test_archive_status_labels_cannot_be_empty_or_conflict(labels):
    with pytest.raises(ValidationError, match="label"):
        ServerSettings(_env_file=None, database_url="postgresql://localhost/test",
                      worker_tokens={"worker": SecretStr("secret")}, **labels)


def test_judge_credentials_stay_out_of_config_serialization_and_code_agent_environment(monkeypatch):
    monkeypatch.setenv("FLOWTOOL_JUDGE_MODEL", "openai:test")
    monkeypatch.setenv("FLOWTOOL_JUDGE_API_KEY", "judge-secret")
    configured = ServerSettings(database_url="postgresql://localhost/test",
                               worker_tokens={"worker": SecretStr("worker-secret")})
    assert "judge_model" not in WorkerSettings.model_fields
    assert configured.judge_model == "openai:test"
    assert configured.judge_api_key.get_secret_value() == "judge-secret"
    assert "judge-secret" not in configured.model_dump_json()
    assert "FLOWTOOL_JUDGE_API_KEY" not in _codex_environment()


@pytest.mark.parametrize("value,expected", [
    ("alice, bob ,carol", ["alice", "bob", "carol"]),
    ('["alice", "bob"]', ["alice", "bob"]),
], ids=["csv", "json"])
def test_settings_parse_allowlist_environment_formats(monkeypatch, tmp_path, value, expected) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLOWTOOL_ALLOWLIST", value)
    monkeypatch.setenv(
        "FLOWTOOL_DATABASE_URL", "postgresql://flowtool:secret@db.example/flowtool"
    )

    monkeypatch.setenv("FLOWTOOL_WORKER_TOKENS", '{"worker-1":"secret"}')

    settings = ServerSettings()  # type: ignore[call-arg]

    assert settings.allowlist == expected
    assert settings.database_url == "postgresql://flowtool:secret@db.example/flowtool"


def test_reviewers_are_configured_independently_from_trigger_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("FLOWTOOL_ALLOWLIST", "alice,bob")
    monkeypatch.setenv("FLOWTOOL_REVIEWERS", "carol,dave")
    monkeypatch.setenv("FLOWTOOL_DATABASE_URL", "postgresql://localhost/flowtool")
    monkeypatch.setenv("FLOWTOOL_WORKER_TOKENS", '{"worker-1":"secret"}')

    settings = ServerSettings()  # type: ignore[call-arg]

    assert settings.allowlist == ["alice", "bob"]
    assert settings.reviewers == ["carol", "dave"]
    assert settings.in_review_label == "flow:in-review"


def test_serve_settings_require_unique_nonempty_worker_credentials(monkeypatch) -> None:
    monkeypatch.setenv("FLOWTOOL_DATABASE_URL", "postgresql://localhost/flowtool")

    with pytest.raises(ValidationError):
        ServerSettings(
            database_url="postgresql://localhost/flowtool",
            worker_tokens={"": SecretStr("secret")},
        )
    with pytest.raises(ValidationError):
        ServerSettings(
            database_url="postgresql://localhost/flowtool",
            worker_tokens={"worker-1": SecretStr("")},
        )
    with pytest.raises(ValidationError):
        ServerSettings(
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
    monkeypatch.delenv("FLOWTOOL_WORKER_REPO_WORKSPACE_LIMIT", raising=False)
    monkeypatch.delenv("FLOWTOOL_ALLOW_INSECURE_WORKER_WS", raising=False)

    settings = WorkerSettings(
        worker_id="worker-1",
        worker_token=SecretStr("secret"),
        serve_ws_url="wss://control.example/internal/workers/connect",
    )

    assert settings.worker_root == Path("~/.flowtool/workers/worker-1").expanduser()
    assert settings.worker_repo_workspace_limit == 2
    assert settings.trusted_host_execution is False
    assert not hasattr(settings, "database_url")

    monkeypatch.setenv("FLOWTOOL_WORKER_REPO_WORKSPACE_LIMIT", "4")
    assert WorkerSettings(**settings.model_dump(exclude={"worker_repo_workspace_limit"})).worker_repo_workspace_limit == 4
    for value in ("0", "-1", "invalid"):
        monkeypatch.setenv("FLOWTOOL_WORKER_REPO_WORKSPACE_LIMIT", value)
        with pytest.raises(ValidationError, match="worker_repo_workspace_limit"):
            WorkerSettings(**settings.model_dump(exclude={"worker_repo_workspace_limit"}))


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
        ServerSettings(
            database_url="postgresql://localhost/flowtool",
            worker_tokens={"worker-1": SecretStr("secret")},
            worker_heartbeat_interval=10,
            worker_lease_duration=20,
        )
    with pytest.raises(ValidationError, match="recovery grace"):
        ServerSettings(
            database_url="postgresql://localhost/flowtool",
            worker_tokens={"worker-1": SecretStr("secret")},
            worker_heartbeat_interval=10,
            worker_heartbeat_timeout=30,
            worker_lease_duration=31,
            worker_recovery_grace=30,
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
