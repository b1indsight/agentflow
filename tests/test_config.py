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

    monkeypatch.setenv("FLOWTOOL_WORKER_ID", "worker-1")
    monkeypatch.setenv("FLOWTOOL_WORKER_TOKEN", "secret")

    settings = ServeSettings()  # type: ignore[call-arg]

    assert settings.allowlist == ["alice", "bob", "carol"]
    assert settings.database_url == "postgresql://flowtool:secret@db.example/flowtool"


def test_settings_preserve_json_allowlist_environment_values(monkeypatch) -> None:
    # pydantic-settings historically accepted JSON arrays for list-valued env vars.
    monkeypatch.setenv("FLOWTOOL_ALLOWLIST", '["alice", "bob"]')
    monkeypatch.setenv("FLOWTOOL_DATABASE_URL", "postgresql://localhost/flowtool")

    monkeypatch.setenv("FLOWTOOL_WORKER_ID", "worker-1")
    monkeypatch.setenv("FLOWTOOL_WORKER_TOKEN", "secret")

    settings = ServeSettings()  # type: ignore[call-arg]

    assert settings.allowlist == ["alice", "bob"]


def test_settings_require_database_url(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLOWTOOL_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        ServeSettings(  # type: ignore[call-arg]
            worker_id="worker-1", worker_token=SecretStr("secret")
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

    assert settings.worker_root == Path("~/.flowtool/worker").expanduser()
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
