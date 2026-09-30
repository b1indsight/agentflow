"""Thread polling recovers input and manual facts without issuing resolution writes."""

from datetime import timedelta

import pytest

from app.control.reconcile_feedback import FeedbackReconciler
from app.domain.github_models import FeedbackPage, ReviewThreadPage, ReviewThreads
from app.domain.models import DeliveryState
from app.persistence.reconcile_store import ReconcileStore
from app.persistence.store import Store
from tests.test_review_batches import feedback, ingest
from tests.test_review_batches import (
    review_flow as review_flow,  # noqa: PLC0414 - fixture re-export
)
from tests.test_review_resolution import human, snapshot


class GitHub:
    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.pages = {}
        self.before_return = None

    async def list_feedback_page(self, *args):
        return FeedbackPage(items=[])

    async def list_review_thread_page(self, installation, repo, number, cursor):
        self.calls.append(cursor)
        if self.before_return:
            self.before_return()
        value = self.pages.get(cursor, page(self.clock))
        if isinstance(value, Exception):
            raise value
        return value


def page(clock, *comments, cursor=None, **changes):
    thread = snapshot(*(comments or [human(updated_at=clock.now)])).model_copy(update=changes)
    return ReviewThreadPage(snapshot=ReviewThreads(
        repo="owner/repo", pr_number=17, fetched_at=clock.now, threads=[thread],
    ), next_cursor=cursor)


async def sweep(store, github):
    await FeedbackReconciler(store, github, settle_seconds=75)._sweep()


def observed(store):
    with store.connection() as connection:
        return connection.execute("SELECT snapshot FROM review_thread_observations").fetchone()["snapshot"]


async def test_resolved_thread_still_collects_missed_human_input(store, review_flow):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages[None] = page(clock, human(updated_at=clock.now), human(2, is_bot=True), resolved=True)
    await sweep(store, github)
    assert len(store.reviews.pending(flow.id)) == 1
    assert observed(store)["resolved"] is True
    clock.now += timedelta(seconds=301)
    github.pages[None] = page(clock, human(updated_at=clock.now), human(3, updated_at=clock.now),
                              resolved=False, outdated=True)
    await sweep(store, github)
    assert observed(store)["resolved"] is False and observed(store)["outdated"] is True
    assert len(store.reviews.pending(flow.id)) == 2
    assert store.reviews.pending(flow.id)[0].version == 1
    assert store.reconcile.feedback_get(flow.id, "threads").last_success_at is not None


async def test_nested_continuation_and_partial_thread_survive_restart(store, review_flow):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages[None] = page(clock, cursor="nested", complete=False)
    github.pages["nested"] = RuntimeError("interrupted")
    await sweep(store, github)
    assert observed(store)["complete"] is False
    check = store.reconcile.feedback_get(flow.id, "threads")
    assert check.cursor == "nested" and check.last_success_at is None
    clock.now += timedelta(seconds=6)
    await sweep(store, github)
    check = store.reconcile.feedback_get(flow.id, "threads")
    assert check.cursor == "nested" and check.failures == 1
    github.pages["nested"] = page(clock, human(2, updated_at=clock.now), complete=False)
    clock.now = check.next_at + timedelta(seconds=1)
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        await sweep(restarted, github)
    finally:
        restarted.close()
    assert len(store.reviews.pending(flow.id)) == 2
    assert observed(store)["complete"] is False
    assert store.reconcile.feedback_get(flow.id, "threads").cursor is None


@pytest.mark.parametrize("deleted", [False, True])
async def test_new_webhook_version_wins_over_slow_thread_page(store, review_flow, deleted):
    flow, clock = review_flow
    github = GitHub(clock)

    def webhook():
        ingest(store, flow, feedback(clock, 1, body="new content", deleted=deleted))

    github.before_return = webhook
    await sweep(store, github)
    items = store.reviews.pending(flow.id)
    assert items == [] if deleted else items[0].body == "new content"


async def test_thread_observations_and_progress_roll_back_together(store, review_flow, monkeypatch):
    flow, clock = review_flow
    original = ReconcileStore.feedback_finish

    def crash(self, check, *args, **kwargs):
        original(self, check, *args, **kwargs)
        if check.source == "threads":
            raise RuntimeError("before commit")

    monkeypatch.setattr(ReconcileStore, "feedback_finish", crash)
    await sweep(store, GitHub(clock))
    assert store.reviews.pending(flow.id) == []
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) n FROM review_thread_observations").fetchone()["n"] == 0
    assert store.reconcile.feedback_get(flow.id, "threads").last_success_at is None


@pytest.mark.parametrize("change", ["merged", "attempt", "head"])
async def test_retired_or_changed_delivery_rejects_inflight_page(store, review_flow, change):
    flow, clock = review_flow
    github = GitHub(clock)

    def changed():
        if change == "attempt":
            flow.current_attempt_id = store.create_attempt(flow.id).id
            store.update_flow(flow)
        else:
            delivery = store.get_delivery(flow.id)
            if change == "merged":
                delivery.state = DeliveryState.MERGED
            else:
                delivery.head_sha = "c" * 40
            store.update_delivery(delivery)

    github.before_return = changed
    await sweep(store, github)
    assert store.reviews.pending(flow.id) == []


async def test_later_pages_cannot_erase_an_incomplete_scan(store, review_flow):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages[None] = ReviewThreadPage(snapshot=ReviewThreads(
        repo=flow.repo, pr_number=17, fetched_at=clock.now, limitations=["thread:missing-node"],
    ), next_cursor="later")
    github.pages["later"] = page(clock)
    await sweep(store, github)
    assert store.reconcile.feedback_get(flow.id, "threads").scan_incomplete
    clock.now += timedelta(seconds=6)
    await sweep(store, github)
    check = store.reconcile.feedback_get(flow.id, "threads")
    assert check.cursor is None and check.last_success_at is None
    assert "incomplete" in check.last_error
    github.pages[None] = page(clock)
    clock.now += timedelta(seconds=301)
    await sweep(store, github)
    assert store.reconcile.feedback_get(flow.id, "threads").last_success_at is not None
