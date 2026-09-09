"""Durable pages must not lose feedback or overwrite newer webhook versions."""

import asyncio
from datetime import timedelta

import pytest

from app.control.reconcile_feedback import FeedbackReconciler
from app.github_models import FeedbackPage, GitHubAdapterError, GitHubErrorKind, RemoteFeedback
from app.models import DeliveryState, FlowState, ReviewItemKind
from app.reconcile_store import ReconcileStore
from app.store import Store
from tests.test_review_batches import feedback, ingest
from tests.test_review_batches import (
    review_flow as review_flow,  # noqa: PLC0414 - fixture re-export
)


class GitHub:
    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.pages = {}
        self.before_return = None

    async def list_feedback_page(self, installation, repo, number, source, cursor):
        self.calls.append((number, source, cursor))
        if self.before_return:
            self.before_return()
        result = self.pages.get((source, cursor), FeedbackPage(items=[]))
        if isinstance(result, Exception):
            raise result
        return result


def page(clock, github_id=101, **changes):
    return FeedbackPage(items=[RemoteFeedback(
        github_id=github_id, author="alice", body="Please add a bound.", updated_at=clock.now,
        **changes,
    )])


def poller(store, github, **options):
    return FeedbackReconciler(store, github, settle_seconds=75, **options)


async def test_three_missing_sources_are_ingested_once_without_webhook_or_strong_signal(
    store, review_flow,
):
    flow, clock = review_flow
    github = GitHub(clock)
    for i, source in enumerate(("issue_comments", "pr_comments", "reviews")):
        github.pages[source, None] = page(clock, 101 + i, state="changes_requested")
    await poller(store, github)._sweep()
    clock.now += timedelta(seconds=301)
    await poller(store, github)._sweep()
    items = store.reviews.pending(flow.id)
    assert len(items) == 3
    assert {item.version for item in items} == {1}
    assert {item.signal for item in items} == {"discussion"}
    assert {number for number, _, _ in github.calls} == {7, 17}
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) n FROM events").fetchone()["n"] == 0
        assert connection.execute("SELECT generation FROM review_buffers").fetchone()["generation"] == 3


async def test_continuation_survives_failure_and_restart(store, review_flow):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages["issue_comments", None] = page(clock).model_copy(update={"next_cursor": "2"})
    github.pages["issue_comments", "2"] = RuntimeError("private token")
    await poller(store, github)._sweep()
    check = store.reconcile.feedback_get(flow.id, "issue_comments")
    assert check.cursor == "2" and check.last_success_at is None
    clock.now += timedelta(seconds=6)
    await poller(store, github)._sweep()
    check = store.reconcile.feedback_get(flow.id, "issue_comments")
    assert check.cursor == "2" and check.failures == 1
    assert "private token" not in check.last_error
    github.pages["issue_comments", "2"] = page(clock, 102)
    clock.now = check.next_at + timedelta(seconds=1)
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        await poller(restarted, github)._sweep()
        check = restarted.reconcile.feedback_get(flow.id, "issue_comments")
    finally:
        restarted.close()
    assert check.cursor is None and check.last_success_at is not None
    assert len(store.reviews.pending(flow.id)) == 2


async def test_page_and_cursor_commit_atomically(store, review_flow, monkeypatch):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages["issue_comments", None] = page(clock)
    original = ReconcileStore.feedback_finish

    def crash(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(ReconcileStore, "feedback_finish", crash)
        await poller(store, github)._sweep()
    assert store.reviews.pending(flow.id) == []
    check = store.reconcile.feedback_get(flow.id, "issue_comments")
    assert check.last_success_at is None and check.cursor is None
    clock.now = check.next_at + timedelta(seconds=1)
    await poller(store, github)._sweep()
    assert len(store.reviews.pending(flow.id)) == 1


@pytest.mark.parametrize("deleted", [False, True])
async def test_slow_same_second_page_cannot_overwrite_webhook(store, review_flow, deleted):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages["issue_comments", None] = page(clock)

    def webhook():
        ingest(store, flow, feedback(clock, body="newer webhook", deleted=deleted,
                                    kind=ReviewItemKind.CONVERSATION))

    github.before_return = webhook
    await poller(store, github)._sweep()
    items = store.reviews.pending(flow.id)
    assert items == [] if deleted else items[0].body == "newer webhook"


async def test_review_edits_and_invalidation_preserve_versions(store, review_flow):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages["reviews", None] = page(clock)
    await poller(store, github)._sweep()
    clock.now += timedelta(seconds=301)
    github.pages["reviews", None] = FeedbackPage(items=[RemoteFeedback(
        github_id=101, author="alice", body="Edited after submission", updated_at=clock.now,
    )])
    await poller(store, github)._sweep()
    assert store.reviews.pending(flow.id)[0].version == 2
    clock.now += timedelta(seconds=301)
    github.pages["reviews", None] = page(clock, state="approved")
    await poller(store, github)._sweep()
    assert store.reviews.pending(flow.id) == []
    with store.connection() as connection:
        assert connection.execute("SELECT generation FROM review_buffers").fetchone()["generation"] == 2


@pytest.mark.parametrize("field,value", [("is_bot", True), ("body", "/flow stop"),
                                         ("state", "pending"), ("author", "flowtool[bot]")])
async def test_old_commands_and_bots_never_wake_or_control(store, review_flow, field, value):
    flow, clock = review_flow
    github = GitHub(clock)
    item = page(clock).items[0].model_copy(update={field: value})
    github.pages["reviews", None] = FeedbackPage(items=[item])
    await poller(store, github)._sweep()
    assert store.reviews.pending(flow.id) == []
    assert store.get_flow(flow.id).state is FlowState.ACTIVE


async def test_page_absence_never_infers_deletion(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, kind=ReviewItemKind.CONVERSATION))
    await poller(store, GitHub(clock))._sweep()
    assert len(store.reviews.pending(flow.id)) == 1


async def test_changed_delivery_discards_the_page(store, review_flow):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages["issue_comments", None] = page(clock)

    def close():
        delivery = store.get_delivery(flow.id)
        delivery.state = DeliveryState.CLOSED
        store.update_delivery(delivery)

    github.before_return = close
    await poller(store, github)._sweep()
    assert store.reviews.pending(flow.id) == []


async def test_repeated_review_does_not_downgrade_an_authorized_signal(store, review_flow):
    flow, clock = review_flow
    update = feedback(clock, kind=ReviewItemKind.REVIEW_BODY, signal="request_changes", submitted=True)
    ingest(store, flow, update)
    github = GitHub(clock)
    github.pages["reviews", None] = FeedbackPage(items=[RemoteFeedback(
        github_id=update.github_id, author=update.author, body=update.body, updated_at=clock.now,
        state="changes_requested",
    )])
    await poller(store, github)._sweep()
    item = store.reviews.pending(flow.id)[0]
    assert item.version == 1 and item.signal == "request_changes" and item.submitted


async def test_rate_limit_preserves_cursor_and_deadline(store, review_flow):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages["reviews", None] = GitHubAdapterError(
        GitHubErrorKind.RATE_LIMITED, "feedback_page", retry_after="600",
    )
    await poller(store, github)._sweep()
    check = store.reconcile.feedback_get(flow.id, "reviews")
    assert check.next_at >= clock.now + timedelta(seconds=600)
    assert check.cursor is None and check.last_success_at is None
    clock.now += timedelta(seconds=301)
    await poller(store, github)._sweep()
    assert sum(source == "reviews" for _, source, _ in github.calls) == 1


async def test_source_pool_is_bounded_and_cancellation_joins_reads(store, review_flow):
    _, clock = review_flow
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(GitHub):
        active = 0
        maximum = 0

        async def list_feedback_page(self, *args):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active == 2:
                entered.set()
            try:
                await release.wait()
                return FeedbackPage(items=[])
            finally:
                self.active -= 1

    github = Slow(clock)
    task = asyncio.create_task(poller(store, github, concurrency=2)._sweep())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert github.maximum == 2 and github.active == 0
