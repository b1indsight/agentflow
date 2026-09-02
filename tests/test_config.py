from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.config import ServeSettings, WorkerSettings


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
    assert not hasattr(settings, "database_url")


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
