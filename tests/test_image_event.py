"""Regression coverage for independent release-event publication boundaries."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SHA = "a" * 40


@pytest.fixture
def image_event(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("image_event", ROOT / "scripts/image_event.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


def candidate(run_id, attempt, sha):
    return {
        "run_id": run_id,
        "attempt": attempt,
        "sha": sha,
        "run_number": run_id,
        "image": "ghcr.io/example@sha256:" + "b" * 64,
    }


def test_server_contract_and_history_are_unchanged_when_worker_publish_fails(
    image_event, monkeypatch
):
    image_event.enqueue(1, 1, SHA)
    with image_event.database() as db, db:
        db.execute("UPDATE events SET state='deployed', reason='old history' WHERE run_id=1")
        db.execute("DELETE FROM worker_events WHERE run_id=1")
    image_event.enqueue(2, 1, SHA)
    monkeypatch.setattr(image_event, "resolve", candidate)
    monkeypatch.setattr(image_event, "github", lambda _: {"object": {"sha": SHA}})
    calls = []
    monkeypatch.setattr(
        image_event.subprocess,
        "run",
        lambda command, check: calls.append(command) or subprocess.CompletedProcess(command, 1),
    )

    assert not image_event.worker_consume(True, True)
    with image_event.database() as db:
        assert db.execute("SELECT state FROM events WHERE run_id=1").fetchone()[0] == "deployed"
        assert db.execute("SELECT state FROM events WHERE run_id=2").fetchone()[0] == "queued"
        assert (
            db.execute("SELECT state FROM worker_events WHERE run_id=2").fetchone()[0] == "failed"
        )
        assert db.execute("SELECT count(*) FROM worker_events WHERE run_id=1").fetchone()[0] == 0
    assert calls == [[str(image_event.WORKER_NOTIFIER), "2", "1", SHA, "--apply"]]
    monkeypatch.setattr(
        image_event.subprocess,
        "run",
        lambda command, check: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )
    assert image_event.consume(True, True)
    assert calls[-1] == [
        str(image_event.UPDATER),
        "--image",
        candidate(2, 1, SHA)["image"],
        "--apply",
    ]
    with image_event.database() as db:
        assert db.execute("SELECT state FROM events WHERE run_id=2").fetchone()[0] == "deployed"


def test_worker_check_and_apply_use_fixed_notifier_arguments(image_event, monkeypatch):
    image_event.enqueue(3, 2, SHA)
    monkeypatch.setattr(image_event, "resolve", candidate)
    monkeypatch.setattr(image_event, "github", lambda _: {"object": {"sha": SHA}})
    calls = []
    monkeypatch.setattr(
        image_event.subprocess,
        "run",
        lambda command, check: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )

    assert image_event.worker_consume(False, True)
    assert calls == [[str(image_event.WORKER_NOTIFIER), "3", "2", SHA]]
    with image_event.database() as db, db:
        db.execute("UPDATE worker_events SET state='queued' WHERE run_id=3")
    assert image_event.worker_consume(True, True)
    assert calls[-1] == [str(image_event.WORKER_NOTIFIER), "3", "2", SHA, "--apply"]


def test_worker_duplicate_is_one_event_and_stale_event_does_not_touch_server(
    image_event, monkeypatch
):
    image_event.enqueue(4, 1, SHA)
    image_event.enqueue(4, 1, SHA)
    monkeypatch.setattr(image_event, "resolve", candidate)
    monkeypatch.setattr(image_event, "github", lambda _: {"object": {"sha": "c" * 40}})

    assert image_event.worker_consume(False, True)
    with image_event.database() as db:
        assert db.execute("SELECT count(*) FROM worker_events WHERE run_id=4").fetchone()[0] == 1
        assert (
            db.execute("SELECT state FROM worker_events WHERE run_id=4").fetchone()[0] == "skipped"
        )
        assert db.execute("SELECT state FROM events WHERE run_id=4").fetchone()[0] == "queued"


@pytest.fixture
def publisher():
    spec = importlib.util.spec_from_file_location(
        "publish_release", ROOT / "scripts/worker_update/publish-release.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("apply", [False, True])
def test_publisher_uses_one_owned_ready_pod_and_fixed_adapter(publisher, monkeypatch, apply):
    responses = iter(
        (
            {"metadata": {"uid": "set-uid"}},
            {
                "items": [
                    {
                        "metadata": {
                            "name": "server-0",
                            "uid": "pod-uid",
                            "ownerReferences": [
                                {"uid": "set-uid", "kind": "StatefulSet", "controller": True}
                            ],
                        },
                        "status": {"containerStatuses": [{"name": "app", "ready": True}]},
                    }
                ]
            },
            {"metadata": {"uid": "pod-uid"}},
        )
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if "exec" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, json.dumps(next(responses)), "")

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    arguments = [
        "publish-release.py",
        "--context",
        "ctx",
        "--namespace",
        "ns",
        "--workload",
        "statefulset/server",
        "--container",
        "app",
        "9",
        "2",
        SHA,
    ]
    monkeypatch.setattr(sys, "argv", arguments + (["--apply"] if apply else []))
    assert publisher.main() == 0
    command, kwargs = calls[-1]
    expected = ["-", "9", "2", SHA] + (["--apply"] if apply else [])
    assert command[-len(expected) :] == expected
    assert kwargs["input"] == publisher.ADAPTER.read_text()


def test_publisher_rejects_unowned_or_non_ready_pods(publisher, monkeypatch):
    monkeypatch.setattr(
        publisher,
        "read_json",
        lambda *args: (
            {"metadata": {"uid": "set-uid"}}
            if args[0][1] == "statefulset/server"
            else {"items": []}
        ),
    )
    with pytest.raises(publisher.PublishError, match="exactly one Ready"):
        publisher.ready_pod("ctx", "ns", "statefulset/server", "app")
