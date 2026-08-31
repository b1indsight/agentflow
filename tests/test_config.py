from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_parse_csv_allowlist_and_expand_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLOWTOOL_ALLOWLIST", "alice, bob ,carol")
    monkeypatch.setenv(
        "FLOWTOOL_DATABASE_URL", "postgresql://flowtool:secret@db.example/flowtool"
    )

    settings = Settings()

    assert settings.allowlist == ["alice", "bob", "carol"]
    assert settings.database_url == "postgresql://flowtool:secret@db.example/flowtool"


def test_settings_preserve_json_allowlist_environment_values(monkeypatch) -> None:
    # pydantic-settings historically accepted JSON arrays for list-valued env vars.
    monkeypatch.setenv("FLOWTOOL_ALLOWLIST", '["alice", "bob"]')
    monkeypatch.setenv("FLOWTOOL_DATABASE_URL", "postgresql://localhost/flowtool")

    settings = Settings()

    assert settings.allowlist == ["alice", "bob"]


def test_settings_require_database_url(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLOWTOOL_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings()
