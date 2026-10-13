"""Thread polling recovers input and manual facts without issuing resolution writes."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.controler.reconciliation.feedback import FeedbackReconciler, ThreadScanState
from app.domain.models import DeliveryState
from app.github import Batch, GitHubError, GitHubErrorKind, PullRequest, ReviewComment, ReviewThread
from app.persistence.reconciliation import ReconcileStore
from app.persistence.store import Store
from tests.github import RecordingGitHub, record_call
from tests.test_github_client import install_client
from tests.test_github_thread_pages import node, outer
from tests.test_review_batches import feedback, ingest
from tests.test_review_batches import (
    review_flow as review_flow,  # noqa: PLC0414 - fixture re-export
)
from tests.test_review_resolution import human, snapshot

NATIVE_FETCH_THREADS = PullRequest.fetch_threads
NATIVE_FETCH_COMMENTS = ReviewThread.fetch_comments


class GitHub(RecordingGitHub):
    def __init__(self, clock):
        super().__init__()
        self.clock, self.calls = clock, []
        self.pages = {}
        self.before_return = None


@pytest.fixture
def thread_page_resources(recording_resources, monkeypatch):
    def result(pr, cursor):
        state = record_call(pr.repository, "fetch_threads_or_comments", number=pr.number)
        state.calls.append(cursor)
        if state.before_return:
            state.before_return()
        value = state.pages.get(cursor, page(state.clock))
        if isinstance(value, Exception):
            raise value
        return value

    async def read(pr, *, cursor=None, limit=100):
        batch = result(pr, cursor)
        return batch.model_copy(update={"items": tuple(
            ReviewThread(pr, **thread.model_dump(exclude_unset=True)) for thread in batch.items
        )})

    async def comments(thread, *, cursor=None, limit=20):
        return result(thread.pull_request, cursor).items[0].comments

    monkeypatch.setattr(PullRequest, "fetch_threads", read)
    monkeypatch.setattr(ReviewThread, "fetch_comments", comments)


def page(clock, *comments, cursor=None, comments_cursor=None, **changes):
    thread = snapshot(*(comments or [human(updated_at=clock.now)])).model_copy(update=changes)
    thread = thread.model_copy(update={"comments": Batch[ReviewComment](
        items=thread.comments.items, next_cursor=comments_cursor,
    )})
    return Batch[ReviewThread](items=(thread,), next_cursor=cursor)


async def sweep(store, github):
    await FeedbackReconciler(
        github,
        settle_seconds=75,
        database=store.database,
        deliveries=store.deliveries,
        reconcile_store=store.reconcile,
        reviews=store.reviews,
    )._sweep()


def observed(store):
    with store.connection() as connection:
        return connection.execute("SELECT snapshot FROM review_thread_observations").fetchone()["snapshot"]


async def test_resolved_thread_still_collects_missed_human_input(store, review_flow, thread_page_resources):
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


@pytest.mark.parametrize("deleted", [False, True])
async def test_new_webhook_version_wins_over_slow_thread_page(store, review_flow, deleted, thread_page_resources):
    flow, clock = review_flow
    github = GitHub(clock)

    def webhook():
        ingest(store, flow, feedback(clock, 1, body="new content", deleted=deleted))

    github.before_return = webhook
    await sweep(store, github)
    items = store.reviews.pending(flow.id)
    assert items == [] if deleted else items[0].body == "new content"


async def test_thread_observations_and_progress_roll_back_together(store, review_flow, monkeypatch, thread_page_resources):
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


@pytest.mark.parametrize("change", ["merged", "head"])
async def test_retired_or_changed_delivery_rejects_inflight_page(store, review_flow, change, thread_page_resources):
    flow, clock = review_flow
    github = GitHub(clock)

    def changed():
        delivery = store.deliveries.get_delivery(flow.id)
        if change == "merged":
            delivery.state = DeliveryState.MERGED
        else:
            delivery.head_sha = "c" * 40
        store.deliveries.update_delivery(delivery)

    github.before_return = changed
    await sweep(store, github)
    assert store.reviews.pending(flow.id) == []


async def test_later_pages_cannot_erase_an_incomplete_scan(store, review_flow, thread_page_resources):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages[None] = page(clock, comments_cursor="missing", cursor="later", complete=False)
    github.pages["missing"] = GitHubError(GitHubErrorKind.NOT_FOUND, "review_thread_comments")
    github.pages["later"] = page(clock)
    await sweep(store, github)
    clock.now += timedelta(seconds=6)
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


async def test_nested_scan_retries_and_resumes_after_restart(
    store, review_flow, recording_resources, monkeypatch,
):
    # A failed suffix request must leave the saved position intact across a process restart.
    flow, clock = review_flow
    first = node(following="comments-20")
    first["comments"]["nodes"] = [node(number=i)["comments"]["nodes"][0] for i in range(1, 21)]
    responses = iter([outer(first, following="threads-next"), RuntimeError("interrupted"),
                      {"node": node(number=21)}, outer(node("T2", number=22))])
    calls = []

    async def graphql(query, variables):
        calls.append(variables)
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    install_client(monkeypatch, lambda *args, **kwargs: SimpleNamespace(async_graphql=graphql))
    monkeypatch.setattr(PullRequest, "fetch_threads", NATIVE_FETCH_THREADS)
    monkeypatch.setattr(ReviewThread, "fetch_comments", NATIVE_FETCH_COMMENTS)
    github = GitHub(clock)
    await sweep(store, github)
    check = store.reconcile.feedback_get(flow.id, "threads")
    state = ThreadScanState.model_validate_json(check.cursor)
    assert state.cursor == "threads-next" and state.thread.comments.items == ()
    assert len(store.reviews.pending(flow.id)) == 20
    saved = check.cursor
    clock.now += timedelta(seconds=6)
    await sweep(store, github)
    check = store.reconcile.feedback_get(flow.id, "threads")
    assert check.cursor == saved and check.failures == 1 and check.last_success_at is None
    clock.now = check.next_at + timedelta(seconds=1)
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        await sweep(restarted, github)
        assert observed(store)["complete"] is False
        clock.now += timedelta(seconds=6)
        await sweep(restarted, github)
    finally:
        restarted.close()
    assert len(store.reviews.pending(flow.id)) == 22
    assert [call["cursor"] for call in calls] == [None, "comments-20", "comments-20", "threads-next"]
    assert store.reconcile.feedback_get(flow.id, "threads").cursor is None
