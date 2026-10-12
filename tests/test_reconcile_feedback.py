"""Durable pages must not lose feedback or overwrite newer webhook versions."""

import asyncio
from datetime import timedelta

import pytest

from app.control.reconcile_feedback import FeedbackReconciler
from app.domain.models import DeliveryState, FlowState, ReviewItemKind
from app.github import (
    Batch,
    GitHubError,
    GitHubErrorKind,
    Issue,
    PullRequest,
    RemoteFeedback,
)
from app.persistence.reconcile_store import ReconcileStore
from app.persistence.store import Store
from tests.github import RecordingGitHub, record_call
from tests.test_review_batches import batch_for, feedback, ingest, settle
from tests.test_review_batches import (
    review_flow as review_flow,  # noqa: PLC0414 - fixture re-export
)


class GitHub(RecordingGitHub):
    def __init__(self, clock):
        super().__init__()
        self.clock, self.calls = clock, []
        self.pages = {}
        self.before_return = None


@pytest.fixture
def feedback_resources(recording_resources, monkeypatch):
    def read(resource, source, cursor):
        state = record_call(resource.repository, "list_" + source + "_page", number=resource.number)
        state.calls.append((resource.number, source, cursor))
        if state.before_return:
            state.before_return()
        value = state.pages.get((source, cursor), Batch(items=[]))
        if isinstance(value, Exception):
            raise value
        return value

    async def issue_comments(issue, *, cursor=None, limit=50):
        return read(issue, "issue_comments", cursor)

    async def pr_comments(pr, *, cursor=None, limit=50):
        return read(pr, "pr_comments", cursor)

    async def reviews(pr, *, cursor=None, limit=50):
        return read(pr, "reviews", cursor)

    monkeypatch.setattr(Issue, "fetch_comments", issue_comments)
    monkeypatch.setattr(PullRequest, "fetch_comments", pr_comments)
    monkeypatch.setattr(PullRequest, "fetch_reviews", reviews)


def page(clock, github_id=101, **changes):
    return Batch(items=[RemoteFeedback(
        github_id=github_id, author="alice", body="Please add a bound.", updated_at=clock.now,
        **changes,
    )])


def poller(store, github, **options):
    return FeedbackReconciler(store, github, settle_seconds=75, **options)


async def test_three_missing_sources_are_ingested_once_without_webhook_or_strong_signal(
    store, review_flow, feedback_resources):
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


async def test_continuation_survives_failure_and_restart(store, review_flow, feedback_resources):
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


async def test_page_and_cursor_commit_atomically(store, review_flow, monkeypatch, feedback_resources):
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
async def test_slow_same_second_page_cannot_overwrite_webhook(store, review_flow, deleted, feedback_resources):
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


async def test_review_edits_and_invalidation_preserve_versions(store, review_flow, feedback_resources):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages["reviews", None] = page(clock)
    await poller(store, github)._sweep()
    clock.now += timedelta(seconds=301)
    github.pages["reviews", None] = Batch(items=[RemoteFeedback(
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
async def test_old_commands_and_bots_never_wake_or_control(store, review_flow, field, value, feedback_resources):
    flow, clock = review_flow
    github = GitHub(clock)
    item = page(clock).items[0].model_copy(update={field: value})
    github.pages["reviews", None] = Batch(items=[item])
    await poller(store, github)._sweep()
    assert store.reviews.pending(flow.id) == []
    assert store.get_flow(flow.id).state is FlowState.ACTIVE


async def test_page_absence_never_infers_deletion(store, review_flow, feedback_resources):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, kind=ReviewItemKind.CONVERSATION))
    await poller(store, GitHub(clock))._sweep()
    assert len(store.reviews.pending(flow.id)) == 1


async def test_changed_delivery_discards_the_page(store, review_flow, feedback_resources):
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


async def test_repeated_review_does_not_downgrade_an_authorized_signal(store, review_flow, feedback_resources):
    flow, clock = review_flow
    update = feedback(clock, kind=ReviewItemKind.REVIEW_BODY, signal="request_changes", submitted=True)
    ingest(store, flow, update)
    github = GitHub(clock)
    github.pages["reviews", None] = Batch(items=[RemoteFeedback(
        github_id=update.github_id, author=update.author, body=update.body, updated_at=clock.now,
        state="changes_requested",
    )])
    await poller(store, github)._sweep()
    item = store.reviews.pending(flow.id)[0]
    assert item.version == 1 and item.signal == "request_changes" and item.submitted


async def test_observed_review_then_authorized_submission_preserves_request_changes(store, review_flow, feedback_resources):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages["reviews", None] = page(clock, state="changes_requested")
    await poller(store, github)._sweep()
    observed = store.reviews.pending(flow.id)[0]
    update = feedback(clock, kind=ReviewItemKind.REVIEW_BODY, body=observed.body,
                      signal="request_changes", submitted=True)
    assert ingest(store, flow, update, "authorized-submitted")
    assert store.reviews.pending(flow.id)[0].signal == "request_changes"
    clock.now += timedelta(seconds=16)
    assert settle(store)[0].kind.value == "revision"


async def test_polling_missing_predecessor_applies_the_queued_successor(store, review_flow, feedback_resources):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, kind=ReviewItemKind.CONVERSATION, body="A"))
    github = GitHub(clock)
    github.pages["issue_comments", None] = Batch(items=[page(clock).items[0].model_copy(update={"body": "B"})])

    def arriving():
        store.reviews.ingest(flow.id, "b-c", "issue_comment", {"changes": {"body": {"from": "B"}}},
            feedback(clock, kind=ReviewItemKind.CONVERSATION, body="C"), settle_seconds=75, submit_grace_seconds=15)

    github.before_return = arriving
    await poller(store, github)._sweep()
    assert store.reviews.pending(flow.id)[0].body == "C"


async def test_edit_to_command_supersedes_feedback_and_later_edit_recovers(store, review_flow, feedback_resources):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, kind=ReviewItemKind.CONVERSATION, body="Change old code"))
    github = GitHub(clock)
    github.pages["issue_comments", None] = Batch(items=[page(clock).items[0].model_copy(update={"body": "/flow stop"})])
    await poller(store, github)._sweep()
    assert store.reviews.pending(flow.id) == []
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    clock.now += timedelta(seconds=301)
    github.pages["issue_comments", None] = page(clock)
    await poller(store, github)._sweep()
    assert store.reviews.pending(flow.id)[0].body == "Please add a bound."


async def test_poll_marks_same_webhook_command_body_inert(store, review_flow, feedback_resources):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, kind=ReviewItemKind.CONVERSATION, body="/flow stop"))
    github = GitHub(clock)
    github.pages["issue_comments", None] = Batch(items=[
        page(clock).items[0].model_copy(update={"body": "/flow stop"}),
    ])
    # An edited command can reach storage before polling identifies it as inert.
    await poller(store, github)._sweep()
    assert store.reviews.pending(flow.id) == []
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    clock.now += timedelta(seconds=301)
    github.pages["issue_comments", None] = page(clock)
    await poller(store, github)._sweep()
    assert store.reviews.pending(flow.id)[0].body == "Please add a bound."


@pytest.mark.parametrize("verdict", ["ignore", "wait"])
async def test_blank_backlog_cannot_hide_text_or_repeat_forever(store, review_flow, verdict, recording_resources):
    from app.domain.models import TriageVerdict, Verdict
    from tests.test_review_batches import claim, run_triage

    flow, clock = review_flow
    for identity in range(1, 71):
        ingest(store, flow, feedback(clock, identity, body=" \n\t", kind=ReviewItemKind.REVIEW_BODY), str(identity))
    ingest(store, flow, feedback(clock, 71, kind=ReviewItemKind.CONVERSATION), "text")
    jobs = claim(store, clock)
    assert len(jobs) == 1
    # Reserve room for actual text even when the oldest 60 records have empty bodies.
    for _ in range(2):
        batch = batch_for(store, jobs[0])
        assert len(batch.feedback) <= 60 and any(item.github_id == 71 for item in batch.feedback)
        await run_triage(store, flow, jobs[0], TriageVerdict(verdict=Verdict(verdict), reason="Checked"))
        jobs = claim(store, clock)
        if not jobs:
            break
    assert jobs == []


def test_blank_authorized_signal_stays_with_bounded_text_batch(store, review_flow):
    from app.domain.models import JobKind
    from tests.test_review_batches import claim

    flow, clock = review_flow
    for identity in range(1, 61):
        ingest(store, flow, feedback(clock, identity, body="", kind=ReviewItemKind.REVIEW_BODY,
                                    signal="request_changes" if identity == 1 else "discussion"), str(identity))
    ingest(store, flow, feedback(clock, 61, kind=ReviewItemKind.CONVERSATION), "text")
    jobs = claim(store, clock)
    assert len(jobs) == 1 and jobs[0].kind is JobKind.REVISION
    assert {item.github_id for item in batch_for(store, jobs[0]).feedback} >= {1, 61}


@pytest.mark.parametrize("verdict", ["ignore", "wait"])
async def test_recovered_backlog_drains_without_oversized_or_repeated_wait_batches(store, review_flow, verdict, feedback_resources):
    from app.domain.models import TriageVerdict, Verdict
    from tests.test_review_batches import claim, run_triage

    flow, clock = review_flow
    github = GitHub(clock)
    comments = [page(clock, number).items[0] for number in range(1, 62)]
    github.pages["issue_comments", None] = Batch(items=comments[:50], next_cursor="2")
    github.pages["issue_comments", "2"] = Batch(items=comments[50:])
    await poller(store, github)._sweep()
    clock.now += timedelta(seconds=6)
    await poller(store, github)._sweep()
    first = claim(store, clock)[0]
    assert len(batch_for(store, first).feedback) == 60
    await run_triage(store, flow, first, TriageVerdict(verdict=Verdict(verdict), reason="Await clarification"))
    # No new webhook is needed to deliver feedback omitted from the bounded first batch.
    second = claim(store, clock)[0]
    assert len(batch_for(store, second).feedback) <= 60
    assert any(item.github_id == 61 for item in batch_for(store, second).feedback)
    await run_triage(store, flow, second, TriageVerdict(verdict=Verdict(verdict), reason="Await clarification"))
    assert claim(store, clock) == []
    if verdict == "ignore":
        assert store.reviews.pending(flow.id) == []


async def test_rate_limit_preserves_cursor_and_deadline(store, review_flow, feedback_resources):
    flow, clock = review_flow
    github = GitHub(clock)
    github.pages["reviews", None] = GitHubError(
        GitHubErrorKind.RATE_LIMITED, "feedback_page", retry_after="600",
    )
    await poller(store, github)._sweep()
    check = store.reconcile.feedback_get(flow.id, "reviews")
    assert check.next_at >= clock.now + timedelta(seconds=600)
    assert check.cursor is None and check.last_success_at is None
    clock.now += timedelta(seconds=301)
    await poller(store, github)._sweep()
    assert sum(source == "reviews" for _, source, _ in github.calls) == 1


async def test_source_pool_is_bounded_and_cancellation_joins_reads(store, review_flow, feedback_resources, monkeypatch):
    _, clock = review_flow
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(GitHub):
        active: int = 0
        maximum: int = 0

    github = Slow(clock)

    async def slow_page(resource, *, cursor=None, limit=50):
        record_call(resource.repository, "list_feedback_page", number=resource.number)
        github.active += 1
        github.maximum = max(github.maximum, github.active)
        if github.active == 2:
            entered.set()
        try:
            await release.wait()
            return Batch(items=[])
        finally:
            github.active -= 1

    monkeypatch.setattr(Issue, "fetch_comments", slow_page)
    monkeypatch.setattr(PullRequest, "fetch_comments", slow_page)
    monkeypatch.setattr(PullRequest, "fetch_reviews", slow_page)
    task = asyncio.create_task(poller(store, github, concurrency=2)._sweep())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert github.maximum == 2 and github.active == 0
