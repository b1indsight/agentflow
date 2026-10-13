"""Durable receipt must survive a crash on either side of business deduplication."""

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest
from psycopg import OperationalError

from app.controler.events.webhooks import WebhookConsumer
from app.controler.workflow.service import ControlService
from app.domain.models import DeliveryState, FlowState
from app.persistence.webhooks import WebhookStore


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
    inbox = store.webhooks
    receive(inbox)
    receive(inbox)
    assert inbox.list()[0].state == "pending"
    assert consumer(store).sweep() == 1
    flow = store.flows.get_active_flow("owner/repo", 7)
    assert flow is not None
    receive(inbox)
    assert consumer(store).sweep() == 0
    assert store.flows.get_active_flow("owner/repo", 7).id == flow.id
    assert inbox.list()[0].state == "done"


def test_failure_after_event_reservation_rolls_back_business_and_replays(store, monkeypatch):
    inbox = store.webhooks
    receive(inbox)
    trigger = ControlService.on_trigger

    def fail_after_trigger(self, *args):
        trigger(self, *args)
        raise RuntimeError("sensitive error must not persist")

    monkeypatch.setattr(ControlService, "on_trigger", fail_after_trigger)
    consumer(store).sweep()
    assert not store.webhooks.event_seen("one")
    assert store.flows.get_active_flow("owner/repo", 7) is None
    assert inbox.list()[0].error_code == "RuntimeError"
    monkeypatch.setattr(ControlService, "on_trigger", trigger)
    with store.connection() as conn:
        conn.execute("UPDATE webhook_inbox SET next_at=%s", (store.now(),))
    consumer(store).sweep()
    assert store.webhooks.event_seen("one")
    assert store.flows.get_active_flow("owner/repo", 7) is not None


def test_expired_claim_is_replayed_but_old_ack_cannot_complete_it(store, monkeypatch):
    inbox = store.webhooks
    now = store.now()
    store._db._clock = lambda: now
    for number in range(1, 51):
        receive(inbox, str(number), payload(number))
    claim = WebhookStore.claim

    def pause_after_last_claim(self):
        nonlocal now
        item = claim(self)
        # A process suspension after the cycle's final claim expires its 60-second lease.
        if item is not None and item.delivery_id == "50" and item.attempts == 1:
            now += timedelta(seconds=61)
        return item

    monkeypatch.setattr(WebhookStore, "claim", pause_after_last_claim)
    worker = consumer(store)
    assert worker.sweep() == 49
    assert not store.webhooks.event_seen("50")
    assert worker.sweep() == 1
    assert store.webhooks.event_seen("50")
    assert all(item.state == "done" for item in inbox.list())


def test_poison_delivery_does_not_block_following_event_and_can_retry(store, monkeypatch):
    trigger = ControlService.on_trigger

    def unavailable(self, repo, number, installation):
        if repo == "owner/repo":
            raise OperationalError("write unavailable")
        return trigger(self, repo, number, installation)

    monkeypatch.setattr(ControlService, "on_trigger", unavailable)
    inbox = store.webhooks
    receive(inbox, "bad")
    receive(inbox, "later", payload(9))
    following = payload(8)
    following["repository"]["full_name"] = "owner/other"
    receive(inbox, "good", following)
    for _ in range(10):
        with store.connection() as conn:
            conn.execute("UPDATE webhook_inbox SET next_at=%s WHERE delivery_id='bad'", (store.now(),))
        consumer(store).sweep()
    rows = {row.delivery_id: row for row in inbox.list()}
    assert rows["bad"].state == "blocked" and rows["bad"].attempts == 10
    assert rows["later"].state == "pending"
    assert rows["good"].state == "done"
    assert store.flows.get_active_flow("owner/other", 8) is not None
    assert store.flows.get_active_flow("owner/repo", 9) is None
    monkeypatch.setattr(ControlService, "on_trigger", trigger)
    assert inbox.retry("bad")
    assert not inbox.retry("good")
    assert consumer(store).sweep() == 2
    assert store.flows.get_active_flow("owner/repo", 7) is not None
    assert store.flows.get_active_flow("owner/repo", 9) is not None


def test_failed_completion_rolls_back_business_and_notifications(store, monkeypatch):
    inbox = store.webhooks
    receive(inbox)
    notifications = []
    service = ControlService(store, jobs_available=lambda: notifications.append(True))
    def unavailable(*args):
        raise OperationalError("connection lost at completion")

    monkeypatch.setattr(WebhookStore, "complete", unavailable)
    WebhookConsumer(store, settings(), service).sweep()
    assert store.flows.get_active_flow("owner/repo", 7) is None
    assert not notifications


def test_cleanup_preserves_pending_and_dedup_identity(store):
    inbox = store.webhooks
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


def test_concurrent_duplicate_receipts_are_consumed_once(store):
    inbox = store.webhooks
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: receive(inbox), range(2)))
    assert consumer(store).sweep() == 1
    assert consumer(store).sweep() == 0
    assert store.flows.get_active_flow("owner/repo", 7) is not None


def test_distinct_receipt_waits_for_older_repository_transaction(store):
    inbox = store.webhooks
    backend: Queue[int] = Queue()

    def second_receipt():
        with store.transaction() as tx, tx.connection() as conn:
            backend.put(conn.info.backend_pid)
            receive(tx.webhooks, "second")

    # Hold the first insert uncommitted: sequence order alone cannot prevent a
    # concurrent, distinct receipt from becoming visible and claimable first.
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.transaction() as first:
            receive(WebhookStore(first), "first")
            pending = pool.submit(second_receipt)
            pid = backend.get(timeout=5)
            deadline = monotonic() + 1.5
            with store.connection() as observer:
                while True:
                    blocked = observer.execute(
                        "SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' AND NOT granted",
                        (pid,),
                    ).fetchone()
                    if blocked:
                        break
                    assert not pending.done(), "later receipt committed before the older transaction"
                    assert monotonic() < deadline, "later receipt never waited on the repository lock"
                    Event().wait(0.01)
            assert inbox.claim() is None
        pending.result(timeout=5)
    claim = inbox.claim()
    assert claim is not None and claim.delivery_id == "first"
    assert inbox.complete(claim)
    claim = inbox.claim()
    assert claim is not None and claim.delivery_id == "second"


@pytest.mark.parametrize("event,action", [
    ("pull_request", "closed"), ("issues", "unlabeled"),
    ("pull_request_review_comment", "created"),
])
def test_all_business_routes_rollback_with_receipt(store, monkeypatch, event, action):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    delivery = store.deliveries.get_or_create_delivery(flow.id, "flow/7")
    delivery.state, delivery.pr_number = DeliveryState.OPEN, 17
    delivery.head_sha = "b" * 40
    store.deliveries.update_delivery(delivery)
    body = payload()
    body.update(action=action, pull_request={"number": 17, "merged": True,
                "merged_at": "2026-09-18T01:00:00Z", "merge_commit_sha": "a" * 40})
    item = {"id": 101, "body": "Please improve validation", "user": {"login": "alice"},
            "state": "commented", "updated_at": "2026-09-18T01:00:00Z"}
    body.update(comment=item, review=item)
    inbox = store.webhooks
    inbox.receive("route", event, body, hashlib.sha256(json.dumps(body).encode()).hexdigest())

    def unavailable(*args):
        raise OperationalError("connection lost at completion")

    with monkeypatch.context() as patch:
        patch.setattr(WebhookStore, "complete", unavailable)
        consumer(store).sweep()
    assert not store.webhooks.event_seen("route")
    assert store.flows.get_flow(flow.id).state == FlowState.ACTIVE
    assert store.deliveries.get_delivery(flow.id).state == DeliveryState.OPEN
    assert not store.reviews.pending(flow.id)
    with store.connection() as conn:
        conn.execute("UPDATE webhook_inbox SET next_at=%s", (store.now(),))
    consumer(store).sweep()
    assert inbox.list()[0].state == "done"
    assert store.webhooks.event_seen("route")


@pytest.mark.asyncio
async def test_consumer_shutdown_waits_for_db_transaction_without_blocking_loop(store, monkeypatch):
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


def test_retry_cannot_be_overtaken_by_cancellation(store, monkeypatch):
    inbox = store.webhooks
    trigger = ControlService.on_trigger
    receive(inbox, 'trigger')
    def fail(*args):
        raise RuntimeError('transient failure')
    monkeypatch.setattr(ControlService, 'on_trigger', fail)
    consumer(store).sweep()
    cancelled = payload()
    cancelled['action'] = 'unlabeled'
    receive(inbox, 'cancel', cancelled)
    # Cancellation must wait for the older accepted trigger, including its backoff.
    assert consumer(store).sweep() == 0
    monkeypatch.setattr(ControlService, 'on_trigger', trigger)
    with store.connection() as conn:
        conn.execute("UPDATE webhook_inbox SET next_at=%s", (store.now(),))
    assert consumer(store).sweep() == 2
    assert store.flows.get_active_flow('owner/repo', 7) is None
