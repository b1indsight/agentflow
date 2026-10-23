"""Exercise the signed HTTP → durable local queue → deployment boundary, without PostgreSQL."""

import hashlib
import hmac
import importlib.util
import json
import multiprocessing
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).parents[1]
SHA = "a" * 40
SECRET = "test-only-webhook-secret"


@pytest.fixture
def events(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "image_event", ROOT / "scripts/server_update/image_event.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    module.initialize(tmp_path / "target")
    queue = module.Queue(tmp_path / "target")
    monkeypatch.delenv("FLOWTOOL_DATABASE_URL", raising=False)
    return module, queue


def payload(run_id=2, sha=SHA):
    return {
        "action": "completed",
        "repository": {"full_name": "b1indsight/agentflow"},
        "workflow": {"path": ".github/workflows/images.yml"},
        "workflow_run": {
            "id": run_id,
            "run_attempt": 1,
            "head_sha": sha,
            "head_branch": "main",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
        },
    }


def post(client, data, event="workflow_run", signature=None):
    raw = json.dumps(data).encode()
    return client.post(
        "/webhook",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-github-event": event,
            "x-hub-signature-256": signature
            or "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest(),
        },
    )


def candidate(run_id, attempt, sha):
    return {
        "run_id": run_id,
        "attempt": attempt,
        "sha": sha,
        "run_number": run_id,
        "image": "ghcr.io/b1indsight/agentflow-serve@sha256:" + "b" * 64,
    }


def test_signed_delivery_is_durable_deduplicated_and_survives_restart(events):
    module, queue = events
    client = TestClient(module.create_app(queue, SECRET))
    assert post(client, payload()).status_code == 202
    assert post(client, payload()).status_code == 202
    assert post(client, payload(sha="c" * 40)).status_code == 409
    files = list((queue.directory / "events").glob("*.json"))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600
    reopened = module.Queue(queue.directory)
    assert [(row.run_id, row.state) for row in reopened.events()] == [(2, "queued")]
    assert SECRET not in files[0].read_text()


@pytest.mark.parametrize(
    "field,value",
    [("conclusion", "failure"), ("head_branch", "feature"), ("event", "pull_request")],
)
def test_unrelated_publications_do_not_enter_queue(events, field, value):
    module, queue = events
    data = payload()
    data["workflow_run"][field] = value
    assert post(TestClient(module.create_app(queue, SECRET)), data).status_code == 204
    assert not queue.events()


def test_untrusted_inputs_are_rejected(events):
    module, queue = events
    client = TestClient(module.create_app(queue, SECRET))
    assert post(client, payload(), signature="sha256=" + "0" * 64).status_code == 401
    data = payload()
    data["workflow_run"]["id"] = True
    assert post(client, data).status_code == 400
    assert post(client, {}, event="ping").status_code == 200
    assert post(client, payload(), event="issues").status_code == 204
    wrong = payload()
    wrong["repository"]["full_name"] = "other/repo"
    assert post(client, wrong).status_code == 204
    wrong["repository"]["full_name"] = "b1indsight/agentflow"
    wrong["workflow"]["path"] = ".github/workflows/other.yml"
    assert post(client, wrong).status_code == 204
    assert client.post("/webhook", content=b"x" * (65536 + 1)).status_code == 413
    assert not queue.events()


def test_corruption_is_not_erased_by_redelivery(events):
    module, queue = events
    queue.enqueue(2, 1, SHA)
    file = queue.directory / "events/2-1.json"
    file.write_text("{broken")
    assert post(TestClient(module.create_app(queue, SECRET)), payload()).status_code == 503
    assert file.read_text() == "{broken"


def test_interrupted_authorized_event_resumes_before_newer_event_without_reresolving(
    events, monkeypatch
):
    module, queue = events
    queue.enqueue(2, 1, SHA)
    row = queue.events()[0].model_copy(update={"state": "running", **candidate(2, 1, SHA)})
    queue.save(row)
    queue.enqueue(3, 1, "c" * 40)
    monkeypatch.setattr(module, "resolve", lambda *args: pytest.fail("must keep pinned image"))
    called = []
    consumer = module.Consumer(queue, lambda row, apply: called.append(row.run_id))
    assert consumer.run(apply=True, once=True)
    assert called == [2]
    assert [row.state for row in queue.events()] == ["deployed", "queued"]


def test_failed_delivery_can_retry_without_deleting_history(events, monkeypatch):
    module, queue = events
    queue.enqueue(2, 1, SHA)
    monkeypatch.setattr(module, "resolve", candidate)
    monkeypatch.setattr(module, "github", lambda _: {"object": {"sha": SHA}})
    consumer = module.Consumer(
        queue, lambda row, apply: (_ for _ in ()).throw(RuntimeError("failed"))
    )
    assert not consumer.run(apply=True, once=True)
    assert queue.events()[0].state == "failed"
    assert post(TestClient(module.create_app(queue, SECRET)), payload()).status_code == 202
    consumer.deploy = lambda row, apply: None
    assert consumer.run(apply=True, once=True)
    assert queue.events()[0].state == "deployed"


def hold_lock(directory, ready, release):
    spec = importlib.util.spec_from_file_location(
        "image_event_process", ROOT / "scripts/server_update/image_event.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with module.Queue(Path(directory)).lock("consumer", timeout=0):
        ready.set()
        release.wait(10)


def test_consumer_exclusion_is_cross_process(events):
    module, queue = events
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=hold_lock, args=(str(queue.directory), ready, release))
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(module.Rejected, match="lock"):
            module.Consumer(queue, lambda row, apply: None).run(apply=True, once=True)
    finally:
        release.set()
        process.join(10)
    assert process.exitcode == 0


def test_missing_authoritative_directory_and_empty_secret_fail_closed(events, tmp_path):
    module, queue = events
    with pytest.raises(module.Rejected):
        module.Queue(tmp_path / "missing")
    with pytest.raises(module.Rejected):
        module.create_app(queue, "")


def test_listener_consumes_signed_request_while_next_request_remains_responsive(
    events, monkeypatch
):
    module, queue = events
    monkeypatch.setattr(module, "resolve", candidate)
    monkeypatch.setattr(module, "github", lambda _: {"object": {"sha": SHA}})
    started, release = threading.Event(), threading.Event()

    def deploy(row, apply):
        assert apply and row.image == candidate(2, 1, SHA)["image"]
        assert queue.events()[0].image == row.image
        started.set()
        assert release.wait(10)

    app = module.create_app(queue, SECRET, module.Consumer(queue, deploy))
    with TestClient(app) as client:
        try:
            assert post(client, payload()).status_code == 202
            assert started.wait(10)
            assert post(client, payload(3)).status_code == 202
            assert [r.state for r in queue.events()] == ["running", "queued"]
        finally:
            release.set()
    assert queue.events()[0].state == "deployed"


@pytest.mark.parametrize("newer_run,newer_attempt", [(3, 1), (2, 2)])
def test_pinned_failed_event_cannot_downgrade_a_newer_deployment(events, newer_run, newer_attempt):
    # A preflight failure pins an image; redelivery after a newer success must still
    # obey the deployment history, even though it no longer resolves against main.
    module, queue = events
    queue.save(module.Event(**candidate(2, 1, SHA), state="failed"))
    queue.save(module.Event(**candidate(newer_run, newer_attempt, SHA), state="deployed"))
    queue.enqueue(2, 1, SHA)
    consumer = module.Consumer(queue, lambda *_: pytest.fail("must not downgrade"))
    assert consumer.run(apply=True, once=True)
    assert [r.state for r in queue.events()] == ["skipped", "deployed"]


def test_terminal_event_is_not_automatically_redeployed_from_completed_journal(events):
    module, queue = events
    row = module.Event(**candidate(2, 1, SHA), state="deployed")
    queue.save(row)
    (queue.directory / "operation.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "stop_intent": True,
                "publication": candidate(2, 1, SHA),
                "new_image": row.image,
            }
        )
    )
    assert post(TestClient(module.create_app(queue, SECRET)), payload()).status_code == 202
    assert module.Consumer(queue, lambda *_: pytest.fail("terminal event")).run(True, once=True)
    assert queue.events()[0].state == "deployed"


def test_stop_intent_resumes_failed_image_before_a_newer_queued_image(events):
    module, queue = events
    queue.save(module.Event(**candidate(2, 1, SHA), state="failed"))
    queue.enqueue(3, 1, "c" * 40)
    (queue.directory / "operation.json").write_text(
        json.dumps(
            {
                "status": "incomplete",
                "stop_intent": True,
                "publication": candidate(2, 1, SHA),
                "new_image": candidate(2, 1, SHA)["image"],
            }
        )
    )
    called = []
    assert module.Consumer(queue, lambda row, _: called.append(row.run_id)).run(True, once=True)
    assert called == [2]
    assert [r.state for r in queue.events()] == ["deployed", "queued"]


@pytest.mark.parametrize("stage", ["replace", "directory"])
def test_failed_persistence_is_not_acknowledged_and_redelivery_recovers(events, monkeypatch, stage):
    module, queue = events
    client = TestClient(module.create_app(queue, SECRET))
    sync = module.os.fsync
    calls = 0

    def fail_sync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        sync(fd)

    with monkeypatch.context() as failure:
        if stage == "replace":
            failure.setattr(Path, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk")))
        else:
            failure.setattr(module.os, "fsync", fail_sync)
        assert post(client, payload()).status_code == 503
    assert post(client, payload()).status_code == 202
    assert len(queue.events()) == 1
