"""Durable feedback must survive crashes without consuming a newer discussion version."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.controler.events.router import EventRouter
from app.controler.workflow.service import ControlService
from app.domain.models import (
    DeliveryState,
    FlowState,
    JobKind,
    JobState,
    ReviewItemKind,
    TriageVerdict,
    Verdict,
)
from app.domain.review_models import BatchState, FeedbackUpdate, ReviewBatch
from app.github import GitHubError, GitHubErrorKind, PullRequest
from app.persistence.review_store import ReviewStore
from app.persistence.store import Store
from tests.github import RecordingGitHub
from tests.review_memory import judge_decision
from tests.test_coordinator import coordinator


@pytest.fixture
def review_flow(store, monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 9, 7, tzinfo=UTC))
    monkeypatch.setattr(store, "_clock", lambda: clock.now)
    flow = store.create_flow("owner/repo", 7, 42)
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    delivery = store.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.pr_number, delivery.state = 17, DeliveryState.OPEN
    store.update_delivery(delivery)
    return flow, clock


def feedback(clock, github_id=101, **changes):
    return FeedbackUpdate(
        kind=ReviewItemKind.INLINE,
        github_id=github_id,
        author="alice",
        body="Add a bound.",
        updated_at=clock.now,
    ).model_copy(update=changes)


def ingest(store, flow, update, delivery="delivery-1"):
    return store.reviews.ingest(
        flow.id,
        delivery,
        "pull_request_review_comment",
        {},
        update,
        settle_seconds=75,
        submit_grace_seconds=15,
    )


def batch_for(store, work):
    return store.reviews.get_batch(work.id) if isinstance(work, ReviewBatch) else store.reviews.for_job(work)


def settle(store):
    return [store.get_job(b.job_id) if b.job_id is not None else b for b in store.reviews.claim_due()]


def claim(store, clock):
    clock.now += timedelta(seconds=76)
    return settle(store)


def test_delivery_and_feedback_are_one_transaction(store, review_flow, monkeypatch):
    flow, clock = review_flow
    original = ReviewStore._append_version

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("interrupted before buffer update")

    # A webhook retry must still work when the first delivery dies after reserving its ID.
    with monkeypatch.context() as patch:
        patch.setattr(ReviewStore, "_append_version", crash)
        with pytest.raises(RuntimeError, match="interrupted"):
            ingest(store, flow, feedback(clock))
    assert not store.event_seen("delivery-1")
    assert ingest(store, flow, feedback(clock))
    assert len(claim(store, clock)) == 1


def test_settle_deadline_survives_new_store(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        assert settle(restarted) == []
        clock.now += timedelta(seconds=76)
        jobs = settle(restarted)
        assert len(jobs) == 1
        assert isinstance(jobs[0], ReviewBatch)
        batch = batch_for(restarted, jobs[0])
        assert batch.feedback[0].github_id == 101
        assert settle(restarted) == []
    finally:
        restarted.close()


def test_batch_completion_cannot_consume_new_comments_or_edits(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    job = claim(store, clock)[0]
    batch = batch_for(store, job)
    ingest(store, flow, feedback(clock, body="Add an upper and lower bound."), "edit")
    ingest(store, flow, feedback(clock, 102), "second")
    assert claim(store, clock) == []  # A running batch owns the Flow until result completion.
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.complete(batch.id, BatchState.COMPLETED, batch.feedback, [])
    next_job = settle(store)[0]
    next_batch = batch_for(store, next_job)
    assert [(f.github_id, f.version) for f in next_batch.feedback] == [(101, 2), (102, 1)]
    assert batch.feedback[0].body == "Add a bound."


def test_duplicate_and_stale_versions_do_not_extend_settle(store, review_flow):
    flow, clock = review_flow
    initial = feedback(clock)
    ingest(store, flow, initial)
    clock.now += timedelta(seconds=60)
    assert not ingest(store, flow, feedback(clock), "redelivery")
    assert not ingest(
        store,
        flow,
        initial.model_copy(
            update={
                "body": "stale",
                "updated_at": initial.updated_at + timedelta(seconds=30),
            }
        ),
        "older",
    )
    clock.now += timedelta(seconds=16)
    assert len(settle(store)) == 1


def test_same_second_edit_uses_the_github_predecessor_to_order_versions(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    update = feedback(clock, body="Use a strict bound.")
    assert store.reviews.ingest(
        flow.id,
        "same-second-edit",
        "pull_request_review_comment",
        {"changes": {"body": {"from": "Add a bound."}}},
        update,
        settle_seconds=75,
        submit_grace_seconds=15,
    )
    assert not ingest(store, flow, feedback(clock), "late-original")
    assert store.reviews.pending(flow.id)[0].body == "Use a strict bound."


def test_failed_batch_claim_rolls_back_deadline_and_job(store, review_flow, monkeypatch):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, signal="request_changes"))
    original = Store.enqueue_job

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("claim interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "enqueue_job", interrupted)
        with pytest.raises(RuntimeError, match="claim interrupted"):
            claim(store, clock)
    assert store.list_jobs(flow.id) == []
    assert len(settle(store)) == 1


def test_wait_needs_new_generation_and_carries_questions(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = batch_for(store, claim(store, clock)[0])
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.complete(batch.id, BatchState.WAITING, [], ["Which limit?"])
    assert claim(store, clock) == []
    ingest(store, flow, feedback(clock, 102, body="Use 10."), "answer")
    resumed = batch_for(store, claim(store, clock)[0])
    assert len(resumed.feedback) == 2
    assert resumed.open_points == ["Which limit?"]


def test_deletion_is_a_tombstone_and_cannot_be_resurrected(store, review_flow):
    flow, clock = review_flow
    initial = feedback(clock)
    ingest(store, flow, initial)
    clock.now += timedelta(seconds=1)
    ingest(store, flow, feedback(clock, body="Newer content"), "edit")
    # GitHub deletion can carry the object's last update timestamp, not the deletion time.
    ingest(store, flow, initial.model_copy(update={"deleted": True, "body": ""}), "delete")
    ingest(store, flow, initial, "late-created")
    assert claim(store, clock) == []


def test_same_number_in_different_github_namespaces_remains_distinct(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    ingest(store, flow, feedback(clock, kind=ReviewItemKind.REVIEW_BODY), "review")
    batch = batch_for(store, claim(store, clock)[0])
    assert len({f.item_id for f in batch.feedback}) == 2


def test_empty_request_changes_waits_for_inline_then_bypasses_judge(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="", signal="request_changes"))
    assert claim(store, clock) == []
    ingest(store, flow, feedback(clock, 102), "inline")
    clock.now += timedelta(seconds=16)
    jobs = settle(store)
    assert len(jobs) == 1 and jobs[0].kind is JobKind.REVISION
    assert "Add a bound" in jobs[0].payload["instruction"]


def test_cancel_retires_buffer_and_batch_atomically(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = batch_for(store, claim(store, clock)[0])
    ingest(store, flow, feedback(clock, 102), "next")
    ControlService(store).cancel(flow)
    assert store.reviews.get_batch(batch.id).state is BatchState.CANCELLED
    assert claim(store, clock) == []


@pytest.mark.parametrize(
    "event_type,item_key,container,action",
    [
        ("issue_comment", "comment", "issue", "created"),
        ("pull_request_review_comment", "comment", "pull_request", "created"),
        ("pull_request_review", "review", "pull_request", "submitted"),
    ],
)
async def test_bot_mentions_always_enter_triage(
    store, review_flow, event_type, item_key, container, action,
):
    _, clock = review_flow
    body = "@flowtool[bot] add a bound"
    settings = SimpleNamespace(
        allowlist=["alice"],
        bot_login="flowtool[bot]",
        trigger_label="flow",
        settle_seconds=75,
        submit_grace_seconds=15,
    )
    router = EventRouter(store, ControlService(store), settings)
    # An allowlisted author's direct request still needs Judge.
    router.dispatch(
        event_type,
        "comment",
        {
            "action": action,
            "repository": {"full_name": "owner/repo"},
            container: {"number": 17},
            "sender": {"login": "alice", "type": "User"},
            item_key: {
                "id": 101,
                "body": body,
                "state": "commented",
                "user": {"login": "alice"},
                "updated_at": clock.now.isoformat(),
            },
        },
    )
    job = claim(store, clock)[0]
    assert isinstance(job, ReviewBatch)
    batch = batch_for(store, job)
    assert batch.feedback[0].body == body
    assert batch.feedback[0].signal == "discussion"


@pytest.mark.parametrize(
    "state,action,sender,expected",
    [
        ("changes_requested", "submitted", "alice", JobKind.REVISION),
        ("changes_requested", "submitted", "outsider", "discussion"),
        ("changes_requested", "edited", "alice", "discussion"),
        ("approved", "submitted", "alice", None),
        ("changes_requested", "dismissed", "alice", None),
    ],
)
async def test_review_actions_have_explicit_dispatch_semantics(
    store,
    review_flow,
    state,
    action,
    sender,
    expected,
):
    _, clock = review_flow
    settings = SimpleNamespace(
        allowlist=["alice"],
        bot_login="flowtool[bot]",
        trigger_label="flow",
        settle_seconds=75,
        submit_grace_seconds=15,
    )
    router = EventRouter(store, ControlService(store), settings)
    router.dispatch(
        "pull_request_review",
        "review",
        {
            "action": action,
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 17},
            "sender": {"login": sender, "type": "User"},
            "review": {
                "id": 101,
                "body": "Add a bound.",
                "state": state,
                "user": {"login": sender},
                "updated_at": clock.now.isoformat(),
            },
        },
    )
    jobs = claim(store, clock)
    assert [j.trigger if isinstance(j, ReviewBatch) else j.kind for j in jobs] == ([] if expected is None else [expected])


def test_failed_batch_retry_restores_ownership_and_cancel_fences_old_work(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, signal="request_changes"))
    job = claim(store, clock)[0]
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        flow.state = FlowState.FAILED
        tx.update_flow(flow)
        tx.cancel_jobs(flow.id)
    assert store.reviews.get_batch(batch_for(store, job).id).state is BatchState.CANCELLED
    ControlService(store).retry(flow)
    replacement = store.list_jobs(flow.id)[-1]
    assert replacement.id != job.id
    assert batch_for(store, replacement).state is BatchState.READY
    assert batch_for(store, replacement).feedback[0].github_id == 101


async def run_triage(store, flow, batch, verdict, *, before_apply=None, github=None):
    github = github or RecordingGitHub()
    subject = coordinator(store, github)
    async def decide(context):
        if before_apply:
            before_apply()
        return judge_decision(verdict, context)
    judge = SimpleNamespace(triage=AsyncMock(side_effect=decide))
    await subject.triage(batch, judge)
    fresh = store.reviews.get_batch(batch.id)
    if fresh.decision is not None or fresh.job_id is not None:
        await subject.triage(fresh, judge)
    return subject, github, store.reviews.get_batch(batch.id).decision





@pytest.mark.parametrize("confidence,expected", [(0.6, JobKind.REVISION), (0.599, None)])
async def test_revise_confidence_boundary_and_unresolved_points_persist(
    store, review_flow, confidence, expected, recording_resources,
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    job = claim(store, clock)[0]
    _, github, _ = await run_triage(store, flow, job, TriageVerdict(
        verdict=Verdict.REVISE, confidence=confidence, instruction="Add a bound",
        reason="Agreed limit, but naming remains open.", open_points=["Which name?"],
    ))
    batch = store.reviews.get_batch(batch_for(store, job).id)
    assert batch.open_points == ["Which name?"]
    assert batch.decision.verdict.confidence == confidence
    if expected:
        assert store.list_jobs(flow.id)[-1].kind is expected
    else:
        assert batch.state is BatchState.WAITING
        assert github.comments == []
        assert not claim(store, clock)


@pytest.mark.parametrize("receipt", [True, False])
async def test_answer_recovery_waits_for_receipt_and_preserves_a_new_version(
    store, review_flow, receipt, recording_resources, monkeypatch,
):
    comment = PullRequest.comment

    async def lost_response(pr, body):
        if receipt:
            await comment(pr, body)
        raise GitHubError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(PullRequest, "comment", lost_response)

    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    job = claim(store, clock)[0]
    github = RecordingGitHub()
    with pytest.raises(GitHubError):
        await run_triage(store, flow, job, TriageVerdict(
            verdict=Verdict.ANSWER, answer="The bound is ten.", confidence=0.8,
        ), github=github)
    batch = batch_for(store, job)
    assert batch.state is BatchState.READY
    assert len(store.reviews.pending(flow.id)) == 1
    assert batch.decision.verdict.answer == "The bound is ten."
    clock.now += timedelta(seconds=20)
    ingest(store, flow, feedback(clock, body="A new request"), "edit")
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        await coordinator(restarted, github).triage(restarted.reviews.get_batch(job.id),
            SimpleNamespace(triage=AsyncMock(side_effect=AssertionError("saved decision must be reused"))))
        await coordinator(restarted, github).triage(restarted.reviews.get_batch(job.id),
            SimpleNamespace(triage=AsyncMock(side_effect=AssertionError("saved decision must be reused"))))
        assert len(github.comments) == int(receipt)
        assert [(f.github_id, f.version) for f in restarted.reviews.pending(flow.id)] == [(101, 2)]
        assert batch_for(restarted, job).state is (BatchState.COMPLETED if receipt else BatchState.READY)
        assert restarted.list_jobs(flow.id) == []
    finally:
        restarted.close()


@pytest.mark.parametrize("change", ["cancel", "closed", "head"])
async def test_answer_rechecks_lifecycle_and_remote_head_before_sending(
    store, review_flow, change, recording_resources, monkeypatch,
):
    read = PullRequest.refetch
    changed = False

    async def changed_pr(pr):
        observed = await read(pr)
        if changed and change in {"closed", "head"}:
            return observed.model_copy(update={
                "state": "closed" if change == "closed" else "open",
                "head_sha": "changed" if change == "head" else "abc",
            })
        return observed

    monkeypatch.setattr(PullRequest, "refetch", changed_pr)

    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    job = claim(store, clock)[0]
    github = RecordingGitHub()

    def change_state():
        nonlocal changed
        changed = True
        if change == "cancel":
            ControlService(store).cancel(flow)

    await run_triage(store, flow, job, TriageVerdict(verdict=Verdict.ANSWER, answer="Reply"),
                     github=github, before_apply=change_state)
    assert github.comments == []
    assert len(store.reviews.pending(flow.id)) == 1


async def test_ignore_consumes_frozen_input_once_and_leaves_newer_version(store, review_flow, recording_resources):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    job = claim(store, clock)[0]
    await run_triage(
        store,
        flow,
        job,
        TriageVerdict(verdict=Verdict.IGNORE),
        before_apply=lambda: ingest(store, flow, feedback(clock, body="New request"), "edit"),
    )
    assert batch_for(store, job).state is BatchState.COMPLETED
    assert [(f.github_id, f.version) for f in store.reviews.pending(flow.id)] == [(101, 2)]


async def test_revise_transfers_batch_to_one_derived_job(store, review_flow, recording_resources):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    job = claim(store, clock)[0]
    _, github, _ = await run_triage(
        store,
        flow,
        job,
        TriageVerdict(verdict=Verdict.REVISE, confidence=0.9, instruction="Fix the bound"),
    )
    revisions = [j for j in store.list_jobs(flow.id) if j.kind is JobKind.REVISION]
    assert len(revisions) == 1
    assert batch_for(store, revisions[0]).id == batch_for(store, job).id
    assert github.comments == []
    ingest(store, flow, feedback(clock, 102), "next")
    assert claim(store, clock) == []




@pytest.mark.parametrize("outcome", [BatchState.COMPLETED, BatchState.WAITING])
def test_retry_after_successful_replacement_does_not_select_historical_failure(
    store, review_flow, outcome,
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, signal="request_changes"))
    original = claim(store, clock)[0]
    service = ControlService(store)
    service.cancel(flow)
    service.retry(flow)
    replacement = store.list_jobs(flow.id)[-1]
    batch = batch_for(store, replacement)
    store.reviews.complete(batch.id, outcome, [], [])
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET state='done' WHERE id=%s", (replacement.id,))

    # A later idle cancellation must not resurrect the old failure whose owner was replaced.
    service.cancel(flow)
    service.retry(flow)
    assert store.get_job(original.id).state is JobState.FAILED
    assert store.list_jobs(flow.id)[-1].kind is JobKind.INITIAL
    assert store.get_flow(flow.id).state is FlowState.ACTIVE


async def test_answer_uses_frozen_decision_once_and_consumes_after_comment(store, review_flow, recording_resources):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    job = claim(store, clock)[0]
    await run_triage(store, flow, job, TriageVerdict(
        verdict=Verdict.WAIT, open_points=["Why is the limit 10?"],
    ))
    ingest(store, flow, feedback(clock, 102, body="Please explain it."), "reply")
    job = claim(store, clock)[0]
    _, github, _ = await run_triage(store, flow, job, TriageVerdict(
        verdict=Verdict.ANSWER, answer="Ten is the configured retry limit.", confidence=0.9,
    ))
    batch = batch_for(store, job)
    assert "Why is the limit 10?" in batch.context.open_points
    assert batch.decision.verdict.answer == "Ten is the configured retry limit."
    assert len(github.comments) == 1
    assert github.comments[0].startswith("Ten is the configured retry limit.")
    assert "#discussion_r102" in github.comments[0]
    assert batch.state is BatchState.COMPLETED
    assert not store.reviews.pending(flow.id)
    assert all(item.kind.value != "answer" for item in store.list_jobs(flow.id))


@pytest.mark.parametrize("approval_id", [101, 102])
async def test_approval_invalidates_body_without_waking_waiting_feedback(
    store, review_flow, approval_id,
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, kind=ReviewItemKind.REVIEW_BODY))
    ingest(store, flow, feedback(clock, 201), "inline")
    batch = batch_for(store, claim(store, clock)[0])
    store.reviews.complete(batch.id, BatchState.WAITING, [], ["Which limit?"])
    router = EventRouter(store, ControlService(store), SimpleNamespace(
        allowlist=["alice"], bot_login="flowtool[bot]", trigger_label="flow",
        settle_seconds=75, submit_grace_seconds=15,
    ))
    # Both a fresh approval and approval of an existing review must leave discussion asleep.
    router.dispatch("pull_request_review", "approval", {
        "action": "submitted", "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 17}, "sender": {"login": "alice"},
        "review": {"id": approval_id, "state": "approved", "body": "Looks good",
                   "user": {"login": "alice"}, "updated_at": clock.now.isoformat()},
    })
    assert claim(store, clock) == []
    if approval_id == 101:
        assert all(f.kind is not ReviewItemKind.REVIEW_BODY for f in store.reviews.pending(flow.id))


@pytest.mark.parametrize("final_body", ["C", "A"])
def test_same_second_out_of_order_edits_reconcile_after_restart(
    store, review_flow, monkeypatch, final_body,
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="A"))

    def edit(repository, delivery, previous, body):
        return repository.reviews.ingest(
            flow.id, delivery, "pull_request_review_comment",
            {"changes": {"body": {"from": previous}}}, feedback(clock, body=body),
            settle_seconds=75, submit_grace_seconds=15,
        )

    # GitHub timestamps have second precision; C may arrive before its predecessor B.
    edit(store, "C", "B", final_body)
    append_version = ReviewStore._append_version

    def crash_after_replaying_c(self, *args, **kwargs):
        changed = append_version(self, *args, **kwargs)
        if args[2] == "C":
            raise RuntimeError("replay interrupted")
        return changed

    with monkeypatch.context() as patch:
        patch.setattr(ReviewStore, "_append_version", crash_after_replaying_c)
        with pytest.raises(RuntimeError, match="replay interrupted"):
            edit(store, "B", "A", "B")
    assert not store.event_seen("B")
    assert store.reviews.pending(flow.id)[0].body == "A"
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        edit(restarted, "B", "A", "B")
        assert restarted.reviews.pending(flow.id)[0].body == final_body
        assert not edit(restarted, "C", "B", final_body)
        batch = batch_for(restarted, claim(restarted, clock)[0])
        assert batch.feedback[0].body == final_body
        assert batch.feedback[0].version == 3
    finally:
        restarted.close()
