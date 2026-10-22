"""Server events persist in PostgreSQL independently of Worker cutovers."""

import importlib.util
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from psycopg.pq import TransactionStatus

from app.controler.workers.updates import WorkerUpdateSession
from tests.test_maintenance import idle_binding
from tests.test_worker_updates import request

ROOT = Path(__file__).parents[1]
SHA = "a" * 40


@pytest.fixture
def image_event(store, monkeypatch):
    spec = importlib.util.spec_from_file_location("image_event", ROOT / "scripts/image_event.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setenv("FLOWTOOL_DATABASE_URL", store.database.database_url)
    monkeypatch.setattr(module, "resolve", candidate)
    monkeypatch.setattr(module, "github", lambda _: {"object": {"sha": SHA}})
    return module


def candidate(run_id, attempt, sha):
    return {
        "run_id": run_id,
        "attempt": attempt,
        "sha": sha,
        "run_number": run_id,
        "image": "ghcr.io/example@sha256:" + "b" * 64,
    }


@pytest.mark.parametrize("apply", [False, True])
def test_server_consumes_deduplicated_event_during_worker_cutover(
    store, image_event, monkeypatch, apply
):
    # A Worker can remain in cutover while the Server deploys. Neither its update
    # state nor a database transaction may be held across the external updater.
    idle_binding(store)
    worker = WorkerUpdateSession(store.workers, "w", "epoch")
    assert worker.handle(request()).idle
    assert worker.handle(request("commit")).accepted
    image_event.enqueue(2, 1, SHA)
    image_event.enqueue(2, 1, SHA)
    with pytest.raises(image_event.Rejected, match="different commit"):
        image_event.enqueue(2, 1, "c" * 40)
    opened = []
    connect = image_event.database

    @contextmanager
    def observed_database():
        with connect() as db:
            opened.append(db)
            yield db

    calls = []

    def run(command, check):
        assert opened[-1].info.transaction_status == TransactionStatus.IDLE
        with connect() as db:
            assert (
                db.execute("SELECT state FROM server_update_events").fetchone()["state"]
                == "running"
            )
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(image_event, "database", observed_database)
    monkeypatch.setattr(image_event.subprocess, "run", run)
    assert image_event.consume(apply, True)
    assert calls == [
        [str(image_event.UPDATER), "--image", candidate(2, 1, SHA)["image"]]
        + (["--apply"] if apply else [])
    ]
    image_event.enqueue(2, 1, SHA)  # Duplicate delivery cannot reset a terminal event.
    with connect() as db:
        rows = db.execute("SELECT * FROM server_update_events").fetchall()
        assert len(rows) == 1
        assert rows[0]["state"] == ("deployed" if apply else "checked")
        assert rows[0]["run_number"] == 2
        assert (
            db.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
                " AND tablename IN ('worker_releases','worker_updates')"
            ).fetchall()
            == []
        )
    assert worker.paused


@pytest.mark.parametrize("stale", [False, True])
def test_failure_and_stale_events_do_not_erase_deployment_history(image_event, monkeypatch, stale):
    image_event.enqueue(1, 1, SHA)
    with image_event.database() as db:
        db.execute("UPDATE server_update_events SET state='deployed',run_number=1")
    image_event.enqueue(2, 1, SHA)
    calls = []
    monkeypatch.setattr(
        image_event.subprocess,
        "run",
        lambda command, check: calls.append(command) or subprocess.CompletedProcess(command, 1),
    )
    if stale:
        monkeypatch.setattr(image_event, "github", lambda _: {"object": {"sha": "c" * 40}})
    assert image_event.consume(True, True) is stale
    with image_event.database() as db:
        states = db.execute("SELECT state FROM server_update_events ORDER BY run_id").fetchall()
        assert [row["state"] for row in states] == ["deployed", "skipped" if stale else "failed"]
    assert len(calls) == int(not stale)


def test_consumer_lock_and_interrupted_event_guard(image_event):
    image_event.enqueue(1, 1, SHA)
    with (
        image_event.database() as first,
        image_event.consumer_lock(first),
        pytest.raises(RuntimeError, match="already running"),
    ):
        image_event.consume(True, True)
    with image_event.database() as db:
        db.execute("UPDATE server_update_events SET state='running'")
    # A crashed consumer releases its lock, but the durable running event prevents
    # silently repeating a possibly completed image switch after process restart.
    with pytest.raises(RuntimeError, match="Interrupted Server update"):
        image_event.consume(True, True)
