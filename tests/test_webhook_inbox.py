"""Durable receipt must survive a crash on either side of business deduplication."""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.control.service import ControlService
from app.control.webhooks import WebhookConsumer
from app.webhook_store import DeliveryConflict, WebhookStore


def settings():
    return SimpleNamespace(
        allowlist=["alice"], bot_login="flowtool[bot]", trigger_label="flow",
        settle_seconds=75, submit_grace_seconds=15,
    )


def payload(number=7):
    return {"action": "labeled", "label": {"name": "flow"},
            "repository": {"full_name": "owner/repo"}, "issue": {"number": number},
            "sender": {"login": "alice"}, "installation": {"id": 42}}


def receive(inbox, delivery="one", body=None):
    body = payload() if body is None else body
    return inbox.receive(delivery, "issues", body, hashlib.sha256(json.dumps(body).encode()).hexdigest())


def consumer(store):
    return WebhookConsumer(store, settings(), ControlService(store))


def test_received_delivery_survives_new_consumer_and_duplicate(store):
    inbox = WebhookStore(store)
    receive(inbox)
    receive(inbox)
    assert inbox.list()[0].state == "pending"
    assert consumer(store).sweep() == 1
    flow = store.get_active_flow("owner/repo", 7)
    assert flow is not None
    receive(inbox)
    assert consumer(store).sweep() == 0
    assert store.get_active_flow("owner/repo", 7).id == flow.id
    assert inbox.list()[0].state == "done"


def test_same_identity_different_content_cannot_overwrite(store):
    inbox = WebhookStore(store)
    receive(inbox)
    with pytest.raises(DeliveryConflict):
        receive(inbox, body=payload(8))
    consumer(store).sweep()
    assert store.get_active_flow("owner/repo", 8) is None


def test_failure_after_event_reservation_rolls_back_business_and_replays(store, monkeypatch):
    inbox = WebhookStore(store)
    receive(inbox)
    trigger = ControlService.on_trigger

    def fail_after_trigger(self, *args):
        trigger(self, *args)
        raise RuntimeError("sensitive error must not persist")

    monkeypatch.setattr(ControlService, "on_trigger", fail_after_trigger)
    consumer(store).sweep()
    assert not store.event_seen("one")
    assert store.get_active_flow("owner/repo", 7) is None
    assert inbox.list()[0].error_code == "RuntimeError"
    monkeypatch.setattr(ControlService, "on_trigger", trigger)
    with store.connection() as conn:
        conn.execute("UPDATE webhook_inbox SET next_at=%s", (store.now(),))
    consumer(store).sweep()
    assert store.event_seen("one")
    assert store.get_active_flow("owner/repo", 7) is not None


def test_expired_claim_is_replayed_but_old_ack_cannot_complete_it(store):
    inbox = WebhookStore(store)
    receive(inbox)
    old = inbox.claim()
    assert old is not None
    with store.connection() as conn:
        conn.execute("UPDATE webhook_inbox SET claim_expires_at=%s", (store.now()-timedelta(seconds=1),))
    new = inbox.claim()
    assert new is not None and new.claim_token != old.claim_token
    assert not inbox.complete(old)
    assert inbox.complete(new)


def test_poison_delivery_does_not_block_following_event_and_can_retry(store):
    inbox = WebhookStore(store)
    receive(inbox, "bad", {"action": "labeled"})
    receive(inbox, "good")
    consumer(store).sweep()
    rows = {row.delivery_id: row for row in inbox.list()}
    assert rows["bad"].state == "blocked"
    assert rows["good"].state == "done"
    assert inbox.retry("bad")
    assert not inbox.retry("good")
    assert store.get_active_flow("owner/repo", 7) is not None


def test_failed_completion_rolls_back_business_and_notifications(store, monkeypatch):
    inbox = WebhookStore(store)
    receive(inbox)
    notifications = []
    service = ControlService(store, jobs_available=lambda: notifications.append(True))
    monkeypatch.setattr(WebhookStore, "complete", lambda *args: False)
    WebhookConsumer(store, settings(), service).sweep()
    assert store.get_active_flow("owner/repo", 7) is None
    assert not notifications


def test_cleanup_preserves_pending_and_dedup_identity(store):
    inbox = WebhookStore(store)
    receive(inbox)
    consumer(store).sweep()
    receive(inbox, "pending", payload(8))
    with store.connection() as conn:
        conn.execute("UPDATE webhook_inbox SET received_at=%s, processed_at=%s WHERE delivery_id='one'",
                     (store.now()-timedelta(days=8), store.now()-timedelta(days=8)))
    inbox.cleanup()
    rows = {row.delivery_id: row for row in inbox.list()}
    assert rows["one"].payload is None
    assert rows["pending"].payload is not None
    receive(inbox)
    assert consumer(store).sweep() == 1  # only the pending second Issue


def test_pre_inbox_marker_is_blocked_instead_of_silently_losing_business(store):
    store.record_event("one", "issues", None, payload())
    inbox = WebhookStore(store)
    receive(inbox)
    consumer(store).sweep()
    assert inbox.list()[0].state == "blocked"
    assert inbox.list()[0].error_code == "LegacyDelivery"


def test_retry_budget_is_bounded(store, monkeypatch):
    def unavailable(*args):
        raise RuntimeError("temporary")

    monkeypatch.setattr(ControlService, "on_trigger", unavailable)
    inbox = WebhookStore(store)
    receive(inbox)
    for _ in range(12):
        with store.connection() as conn:
            conn.execute("UPDATE webhook_inbox SET next_at=%s", (store.now(),))
        consumer(store).sweep()
    assert inbox.list()[0].state == "blocked"
    assert inbox.list()[0].attempts == 10


def test_concurrent_duplicate_receipts_and_claims(store):
    from concurrent.futures import ThreadPoolExecutor

    inbox = WebhookStore(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: receive(inbox), range(2)))
        claims = list(pool.map(lambda _: inbox.claim(), range(2)))
    assert sum(item is not None for item in claims) == 1


@pytest.mark.parametrize("event,action", [
    ("pull_request", "closed"), ("issues", "unlabeled"),
    ("issue_comment", "created"), ("pull_request_review", "submitted"),
    ("pull_request_review_comment", "created"),
])
def test_all_business_routes_rollback_with_receipt(store, monkeypatch, event, action):
    from app.models import DeliveryState, FlowState

    flow = store.create_flow("owner/repo", 7, 42)
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    delivery = store.get_or_create_delivery(flow.id, "flow/7")
    delivery.state, delivery.pr_number = DeliveryState.OPEN, 17
    delivery.head_sha = "b" * 40
    store.update_delivery(delivery)
    body = payload()
    body.update(action=action, pull_request={"number": 17, "merged": True,
                "merged_at": "2026-09-18T01:00:00Z", "merge_commit_sha": "a" * 40})
    if event == "issue_comment":
        body["issue"]["number"] = 17
    item = {"id": 101, "body": "Please improve validation", "user": {"login": "alice"},
            "state": "commented", "updated_at": "2026-09-18T01:00:00Z"}
    body.update(comment=item, review=item)
    inbox = WebhookStore(store)
    inbox.receive("route", event, body, hashlib.sha256(json.dumps(body).encode()).hexdigest())
    with monkeypatch.context() as patch:
        patch.setattr(WebhookStore, "complete", lambda *args: False)
        consumer(store).sweep()
    assert not store.event_seen("route")
    assert store.get_flow(flow.id).state == FlowState.ACTIVE
    assert store.get_delivery(flow.id).state == DeliveryState.OPEN
    assert not store.reviews.pending(flow.id)
    with store.connection() as conn:
        conn.execute("UPDATE webhook_inbox SET next_at=%s", (store.now(),))
    consumer(store).sweep()
    assert inbox.list()[0].state == "done"
    assert store.event_seen("route")


@pytest.mark.asyncio
async def test_consumer_shutdown_waits_for_db_transaction_without_blocking_loop(store, monkeypatch):
    import asyncio
    from threading import Event

    started, release = Event(), Event()
    worker = consumer(store)

    def cycle(loop):
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(worker, "_cycle", cycle)
    task = asyncio.create_task(worker.run())
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
