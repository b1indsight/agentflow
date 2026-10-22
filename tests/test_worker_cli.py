"""Manual updates never control a running Worker or replace its recovery evidence."""

import json
import os
from unittest.mock import AsyncMock, Mock

import pytest
from typer.testing import CliRunner

from app import cli, releases
from app.domain.models import JobState
from app.releases import ReleaseError, ReleaseEvent
from app.worker import config
from app.worker.runtime import WorkerRootLock
from app.worker.state.session import WorkerSession
from app.worker.updates import release
from app.worker.updates.state import PreparedRelease
from tests.test_worker_session import _assignment

SHA = "2" * 40


@pytest.fixture
def publication(monkeypatch):
    event = ReleaseEvent(sha=SHA, run_id=1, attempt=1, run_number=1)
    monkeypatch.setattr(releases, "release_for_sha", Mock(return_value=event))
    return event


@pytest.fixture
def worker_root(tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith("FLOWTOOL_"):
            monkeypatch.delenv(key)
    root = tmp_path / "worker"
    monkeypatch.setenv("FLOWTOOL_WORKER_ID", "worker-1")
    monkeypatch.setenv("FLOWTOOL_WORKER_TOKEN", "private-token")
    monkeypatch.setenv("FLOWTOOL_SERVE_WS_URL", "wss://unused")
    monkeypatch.setenv("FLOWTOOL_WORKER_ROOT", str(root))
    monkeypatch.setitem(vars(config), "settings", config.WorkerSettings())
    lock = WorkerRootLock(root, "worker-1")
    lock.acquire()
    lock.release()
    return root


def test_worker_without_subcommand_still_starts_runtime(worker_root, monkeypatch):
    runtime = Mock(run=AsyncMock())
    monkeypatch.setattr("app.worker.runtime.WorkerRuntime", Mock(return_value=runtime))
    result = CliRunner().invoke(cli.app, ["worker"])
    assert result.exit_code == 0, result.output
    runtime.run.assert_awaited_once()


@pytest.mark.parametrize("arguments", [["worker", "--help"], ["worker", "update", "--help"]])
def test_worker_help_does_not_load_credentials_or_start_runtime(monkeypatch, arguments):
    monkeypatch.setattr(config, "WorkerSettings", lambda: pytest.fail("must not load credentials"))
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 0, result.output
    assert "update" in result.output


def test_worker_update_check_is_read_only_and_needs_no_credentials(
    publication, tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "WorkerSettings", lambda: pytest.fail("must not load credentials"))
    monkeypatch.setattr(release, "prepare", lambda *args: pytest.fail("check must not prepare"))
    result = CliRunner().invoke(cli.app, ["worker", "update", "--sha", SHA])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"state": "checked", **publication.model_dump()}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "arguments",
    [
        ["--sha", "ABC"],
        ["--sha", "A" * 40],
        ["--sha", SHA, "--apply"],
        ["--sha", SHA, "--root", "/unused"],
    ],
)
def test_invalid_update_arguments_do_not_query_publication(monkeypatch, arguments):
    monkeypatch.setattr(releases, "release_for_sha", lambda *args: pytest.fail("invalid CLI"))
    result = CliRunner().invoke(cli.app, ["worker", "update", *arguments])
    assert result.exit_code == 2, result.output


@pytest.mark.parametrize("journal", ["absent", "acknowledged"])
def test_manual_apply_prepares_under_root_lock_and_preserves_journal(
    publication, worker_root, tmp_path, monkeypatch, journal
):
    session = WorkerSession(worker_root)
    if journal == "acknowledged":
        assignment = _assignment()
        session.record_received(assignment)
        session.record_started()
        session.complete(RuntimeError("test failure"))
        session.acknowledge(assignment.job_id, JobState.FAILED)
    before = session.path.read_bytes() if session.path.exists() else None
    destination = tmp_path / "updates"
    prepared = PreparedRelease(release=publication, path=destination / "releases" / SHA)

    def prepare(root, event):
        assert root == destination and event == publication
        with pytest.raises(RuntimeError, match="already in use"):
            WorkerRootLock(worker_root).acquire()
        return prepared

    monkeypatch.setattr(release, "prepare", prepare)
    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("must not signal a Worker"))
    result = CliRunner().invoke(
        cli.app, ["worker", "update", "--sha", SHA, "--apply", "--root", str(destination)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "state": "prepared",
        **prepared.model_dump(mode="json"),
        "python": str(prepared.path / ".venv/bin/python"),
    }
    assert (session.path.read_bytes() if session.path.exists() else None) == before
    lock = WorkerRootLock(worker_root, "worker-1")
    lock.acquire()
    lock.release()


@pytest.mark.parametrize("blocker", ["running", "pending", "corrupt", "identity", "missing"])
def test_manual_apply_refuses_unsafe_worker_without_preparing(
    publication, worker_root, tmp_path, monkeypatch, blocker
):
    session = WorkerSession(worker_root)
    lock = WorkerRootLock(worker_root)
    if blocker == "running":
        lock.acquire()
    elif blocker == "pending":
        session.record_received(_assignment())
    elif blocker == "corrupt":
        session.path.write_text("invalid journal")
    elif blocker == "identity":
        (worker_root / ".worker-id").write_text("other-worker\n")
    else:
        (worker_root / ".worker-id").unlink()
    prepare = Mock(side_effect=AssertionError("unsafe Worker must not prepare"))
    monkeypatch.setattr(release, "prepare", prepare)
    try:
        result = CliRunner().invoke(
            cli.app,
            ["worker", "update", "--sha", SHA, "--apply", "--root", str(tmp_path / "updates")],
        )
        assert result.exit_code == 1, result.output
        assert "private-token" not in result.output
        prepare.assert_not_called()
        assert not (tmp_path / "updates").exists()
    finally:
        lock.release()


def test_failed_manual_preparation_releases_worker_lock(
    publication, worker_root, tmp_path, monkeypatch
):
    monkeypatch.setattr(release, "prepare", Mock(side_effect=ReleaseError("preparation failed")))
    result = CliRunner().invoke(
        cli.app,
        ["worker", "update", "--sha", SHA, "--apply", "--root", str(tmp_path / "updates")],
    )
    assert result.exit_code == 1 and "preparation failed" in result.output
    lock = WorkerRootLock(worker_root, "worker-1")
    lock.acquire()
    lock.release()
