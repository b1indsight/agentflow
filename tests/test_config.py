from pathlib import Path

from app.config import Settings


def test_settings_parse_csv_allowlist_and_expand_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLOWTOOL_ALLOWLIST", "alice, bob ,carol")
    monkeypatch.setenv("FLOWTOOL_DB_PATH", "~/agentflow-test.sqlite3")

    settings = Settings()

    assert settings.allowlist == ["alice", "bob", "carol"]
    assert settings.db_path == Path.home() / "agentflow-test.sqlite3"


def test_settings_preserve_json_allowlist_environment_values(monkeypatch) -> None:
    # pydantic-settings historically accepted JSON arrays for list-valued env vars.
    monkeypatch.setenv("FLOWTOOL_ALLOWLIST", '["alice", "bob"]')

    settings = Settings()

    assert settings.allowlist == ["alice", "bob"]
