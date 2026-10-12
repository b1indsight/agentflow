"""Thread polling recovers input and manual facts without issuing resolution writes."""

from datetime import timedelta

import pytest

from app.control.reconcile_feedback import FeedbackReconciler, ThreadScanState
from app.domain.models import DeliveryState
from app.github import Batch, GitHubError, GitHubErrorKind, PullRequest, ReviewComment, ReviewThread
from app.persistence.reconcile_store import ReconcileStore
from app.persistence.store import Store
from tests.github import RecordingGitHub, record_call
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
    await FeedbackReconciler(store, github, settle_seconds=75)._sweep()


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


async def test_nested_continuation_and_partial_thread_survive_restart(store, review_flow, thread_page_resources):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages[None] = page(clock, comments_cursor="nested", complete=False)
    github.pages["nested"] = RuntimeError("interrupted")
    await sweep(store, github)
    assert observed(store)["complete"] is False
    check = store.reconcile.feedback_get(flow.id, "threads")
    saved = check.cursor
    state = ThreadScanState.model_validate_json(saved)
    assert state.thread.comments.next_cursor == "nested" and state.thread.comments.items == ()
    assert check.last_success_at is None
    clock.now += timedelta(seconds=6)
    await sweep(store, github)
    check = store.reconcile.feedback_get(flow.id, "threads")
    assert check.cursor == saved and check.failures == 1
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


@pytest.mark.parametrize("change", ["merged", "attempt", "head"])
async def test_retired_or_changed_delivery_rejects_inflight_page(store, review_flow, change, thread_page_resources):
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


async def test_native_nested_scan_resumes_without_repeating_initial_comments(
    store, review_flow, recording_resources, monkeypatch,
):
    # Forty-five comments require two suffix reads, then the saved outer cursor selects T2.
    from types import SimpleNamespace

    from tests.test_github_client import install_client
    from tests.test_github_thread_pages import node, outer

    flow, clock = review_flow
    first = node(following="comments-20")
    first["comments"]["nodes"] = [node(number=i)["comments"]["nodes"][0] for i in range(1, 21)]
    second = node(following="comments-40")
    second["comments"]["nodes"] = [node(number=i)["comments"]["nodes"][0] for i in range(21, 41)]
    third = node()
    third["comments"]["nodes"] = [node(number=i)["comments"]["nodes"][0] for i in range(41, 46)]
    responses = iter([outer(first, following="threads-next"), {"node": second},
                      {"node": third}, outer(node("T2", number=46))])
    calls = []

    async def graphql(query, variables):
        calls.append(variables)
        return next(responses)

    install_client(monkeypatch, lambda *args, **kwargs: SimpleNamespace(async_graphql=graphql))
    monkeypatch.setattr(PullRequest, "fetch_threads", NATIVE_FETCH_THREADS)
    monkeypatch.setattr(ReviewThread, "fetch_comments", NATIVE_FETCH_COMMENTS)
    github = GitHub(clock)
    await sweep(store, github)
    check = store.reconcile.feedback_get(flow.id, "threads")
    state = ThreadScanState.model_validate_json(check.cursor)
    assert state.cursor == "threads-next" and state.thread.comments.items == ()
    assert len(store.reviews.pending(flow.id)) == 20
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        for _ in range(3):
            clock.now += timedelta(seconds=6)
            await sweep(restarted, github)
    finally:
        restarted.close()
    assert len(store.reviews.pending(flow.id)) == 46
    assert [call["cursor"] for call in calls] == [None, "comments-20", "comments-40", "threads-next"]
    assert [call["limit"] for call in calls] == [1, 20, 20, 1]
    assert store.reconcile.feedback_get(flow.id, "threads").cursor is None
