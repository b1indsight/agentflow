"""Durable pages must not lose feedback or overwrite newer webhook versions."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.controler.reconciliation.feedback import FeedbackReconciler
from app.domain.models import (
    DeliveryState,
    FlowState,
    JobKind,
    ReviewItemKind,
    TriageVerdict,
    Verdict,
)
from app.github import (
    Batch,
    GitHubError,
    GitHubErrorKind,
    Issue,
    PullRequest,
    RemoteFeedback,
)
from app.persistence.reconciliation import ReconcileStore
from app.persistence.store import Store
from tests.github import RecordingGitHub, record_call
from tests.test_github_client import AsyncEndpoint, app_client, response
from tests.test_review_batches import batch_for, claim, feedback, ingest, run_triage, settle
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
    return FeedbackReconciler(
        github,
        settle_seconds=75,
        **options,
        database=store.database,
    )


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


async def test_direct_feedback_skips_issue_seed_and_stale_issue_check_but_keeps_pr_sources(store, feedback_resources):
    # A stale or manually inserted Issue scan must also be inert, not just absent from new seeds.
    flow = store.flows.create_flow("owner/repo", None, 42, task_content="Direct")
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id, flow.state = attempt.id, FlowState.REVIEW
    store.flows.update_flow(flow)
    delivery = store.deliveries.get_or_create_delivery(flow.id, flow.initial_branch)
    delivery.pr_number, delivery.state = 17, DeliveryState.OPEN
    store.deliveries.update_delivery(delivery)
    clock = SimpleNamespace(now=store.now())
    github = GitHub(clock)
    for i, source in enumerate(("pr_comments", "reviews")):
        github.pages[source, None] = page(clock, 101 + i)
    reconciler = poller(store, github)
    reconciler.reconcile_store.feedback_seed(64)
    checks = reconciler.reconcile_store.feedback_due(64)
    assert {check.source for check in checks} == {"pr_comments", "reviews", "threads"}
    with store.connection() as connection:
        connection.execute("INSERT INTO feedback_reconcile(flow_id,source,next_at) VALUES(%s,'issue_comments',%s)",
                           (flow.id, store.now()))
    stale = reconciler.reconcile_store.feedback_get(flow.id, "issue_comments")
    await reconciler._check(stale)
    assert github.calls == []
    assert "issue_comments" not in {check.source for check in reconciler.reconcile_store.feedback_due(64)}
    await reconciler._sweep()
    assert {(number, source) for number, source, _ in github.calls} == {(17, "pr_comments"), (17, "reviews")}
    assert {item.github_id for item in store.reviews.pending(flow.id)} == {101, 102}
    assert any(call["operation"] == "fetch_threads" for call in github.resource_calls)


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
                                         ("state", "pending"), ("author", "Flowtool-bot[bot]")])
async def test_old_commands_and_bots_never_wake_or_control(
    store, review_flow, field, value, feedback_resources, monkeypatch,
):
    flow, clock = review_flow
    github = GitHub(clock)
    github._bot_login = None
    get_app = AsyncEndpoint(response(SimpleNamespace(id=1, slug="flowtool-bot")))
    app_client(monkeypatch, lambda **kwargs: SimpleNamespace(
        rest=SimpleNamespace(apps=SimpleNamespace(async_get_authenticated=get_app)),
    ))
    item = page(clock).items[0].model_copy(update={field: value})
    github.pages["reviews", None] = Batch(items=[item])
    await poller(store, github)._sweep()
    assert store.reconcile.feedback_get(flow.id, "reviews").last_success_at is not None
    assert store.reviews.pending(flow.id) == []
    assert store.flows.get_flow(flow.id).state is FlowState.REVIEW


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
        delivery = store.deliveries.get_delivery(flow.id)
        delivery.state = DeliveryState.CLOSED
        store.deliveries.update_delivery(delivery)

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
    assert store.flows.get_flow(flow.id).state is FlowState.REVIEW
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
    assert store.flows.get_flow(flow.id).state is FlowState.REVIEW
    clock.now += timedelta(seconds=301)
    github.pages["issue_comments", None] = page(clock)
    await poller(store, github)._sweep()
    assert store.reviews.pending(flow.id)[0].body == "Please add a bound."


@pytest.mark.parametrize("verdict", ["ignore", "wait"])
async def test_blank_backlog_cannot_hide_text_or_repeat_forever(store, review_flow, verdict, recording_resources):
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
    await run_triage(store, flow, first, TriageVerdict(verdict=Verdict(verdict), reason="Await clarification"), github=github)
    # No new webhook is needed to deliver feedback omitted from the bounded first batch.
    second = claim(store, clock)[0]
    assert len(batch_for(store, second).feedback) <= 60
    assert any(item.github_id == 61 for item in batch_for(store, second).feedback)
    await run_triage(store, flow, second, TriageVerdict(verdict=Verdict(verdict), reason="Await clarification"), github=github)
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


@pytest.mark.parametrize("source", ["poll", "webhook"])
async def test_resumed_round_rejects_old_issue_comments_even_if_first_seen_or_edited(
    store, review_flow, feedback_resources, source,
):
    # Cursor reset must not attach unseen historical Issue comments to the new Attempt.
    from types import SimpleNamespace

    from app.controler.events.router import EventRouter
    from app.controler.workflow.service import ControlService

    flow, clock = review_flow
    service = ControlService(database=store.database)
    service.cancel(flow)
    clock.now += timedelta(seconds=10, microseconds=500_000)
    started = clock.now
    resumed = service.resume(flow, expected_round_no=1)
    # An INITIAL retry / late preparation must not move the round's feedback boundary.
    clock.now += timedelta(seconds=10)
    store.jobs.cancel_jobs(flow.id)
    resumed.current_attempt_id = store.flows.create_attempt(flow.id).id
    resumed.state = FlowState.REVIEW
    store.flows.update_flow(resumed)
    delivery = store.deliveries.get_delivery(flow.id)
    if delivery is None:
        delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}-round-2")
    delivery.state, delivery.pr_number = DeliveryState.OPEN, 18
    store.deliveries.update_delivery(delivery)
    created = [
        started - timedelta(days=1),  # Old, never ingested; updated after resume below.
        started.replace(microsecond=0),  # GitHub cannot disambiguate this second.
        None,  # Missing creation evidence must not fall back to updated_at.
        started.replace(microsecond=0) + timedelta(seconds=1),
    ]
    if source == "poll":
        github = GitHub(clock)
        github.pages["issue_comments", None] = Batch(items=[
            RemoteFeedback(github_id=101 + i, author="alice", body="newly observed",
                           updated_at=clock.now, created_at=timestamp)
            for i, timestamp in enumerate(created)
        ])
        await poller(store, github)._sweep()
        assert store.reconcile.feedback_get(flow.id, "issue_comments").last_success_at is not None
    else:
        router = EventRouter(service, SimpleNamespace(
            allowlist={"alice"}, settle_seconds=75, submit_grace_seconds=15,
        ), bot_login="flowtool[bot]", database=store.database)
        for i, timestamp in enumerate(created):
            router.dispatch("issue_comment", f"round-comment-{i}", {
                "repository": {"full_name": flow.repo}, "issue": {"number": flow.issue_number},
                "sender": {"login": "alice"}, "action": "edited",
                "comment": {"id": 101 + i, "user": {"login": "alice"}, "body": "newly observed",
                            "created_at": timestamp.isoformat() if timestamp else None,
                            "updated_at": clock.now.isoformat()},
            })
            assert store.webhooks.event_seen(f"round-comment-{i}")
    assert [item.github_id for item in store.reviews.pending(flow.id)] == [104]
    clock.now += timedelta(seconds=76)
    batch = store.reviews.claim_due()[0]
    assert [item.github_id for item in batch.feedback] == [104]
