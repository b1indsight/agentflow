"""Durable feedback must survive crashes without consuming a newer discussion version."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.control.router import EventRouter
from app.control.service import ControlService
from app.models import DeliveryState, JobKind, JobState, ReviewItemKind, TriageVerdict, Verdict
from app.protocol import Succeeded, TriageResult, terminal_digest
from app.review_models import BatchState, FeedbackUpdate
from app.review_store import ReviewStore
from app.store import Store
from tests.test_coordinator import RecordingGitHub, coordinator


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


def claim(store, clock):
    clock.now += timedelta(seconds=76)
    return store.reviews.claim_due()


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


def test_settle_survives_new_store_and_concurrent_claims(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    clock.now += timedelta(seconds=76)
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda db: db.reviews.claim_due(), [store, restarted]))
        jobs = [job for group in claimed for job in group]
        assert len(jobs) == 1
        assert jobs[0].kind is JobKind.TRIAGE
        batch = restarted.reviews.for_job(jobs[0])
        assert batch.feedback[0].github_id == 101
    finally:
        restarted.close()


def test_batch_completion_cannot_consume_new_comments_or_edits(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    job = claim(store, clock)[0]
    batch = store.reviews.for_job(job)
    ingest(store, flow, feedback(clock, body="Add an upper and lower bound."), "edit")
    ingest(store, flow, feedback(clock, 102), "second")
    assert claim(store, clock) == []  # A running batch owns the Flow until result completion.
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.complete(batch.id, BatchState.COMPLETED, batch.feedback, [])
    next_job = store.reviews.claim_due()[0]
    next_batch = store.reviews.for_job(next_job)
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
    assert len(store.reviews.claim_due()) == 1


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
    ingest(store, flow, feedback(clock))
    original = Store.enqueue_job

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("claim interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "enqueue_job", interrupted)
        with pytest.raises(RuntimeError, match="claim interrupted"):
            claim(store, clock)
    assert store.list_jobs(flow.id) == []
    assert len(store.reviews.claim_due()) == 1


def test_legacy_import_preserves_pending_feedback_without_trusting_resolution(store, review_flow):
    from app.models import ReviewItem

    flow, _ = review_flow
    store.upsert_review_item(
        ReviewItem(
            id=None,
            attempt_id=flow.current_attempt_id,
            kind=ReviewItemKind.INLINE,
            github_id=555,
            author="alice",
            body="Unverified old response",
            resolved=True,
        )
    )
    with store.connection() as connection:
        connection.execute("DELETE FROM review_migrations")
    store.reviews.migrate_legacy()
    store.reviews.migrate_legacy()
    pending = store.reviews.pending(flow.id)
    assert len(pending) == 1 and pending[0].github_id == 555
    assert len(store.reviews.claim_due()) == 1


def test_wait_needs_new_generation_and_carries_questions(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = store.reviews.for_job(claim(store, clock)[0])
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.complete(batch.id, BatchState.WAITING, [], ["Which limit?"])
    assert claim(store, clock) == []
    ingest(store, flow, feedback(clock, 102, body="Use 10."), "answer")
    resumed = store.reviews.for_job(claim(store, clock)[0])
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
    batch = store.reviews.for_job(claim(store, clock)[0])
    assert len({f.item_id for f in batch.feedback}) == 2


def test_empty_request_changes_waits_for_inline_then_bypasses_judge(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="", signal="request_changes"))
    assert claim(store, clock) == []
    ingest(store, flow, feedback(clock, 102), "inline")
    clock.now += timedelta(seconds=16)
    jobs = store.reviews.claim_due()
    assert len(jobs) == 1 and jobs[0].kind is JobKind.REVISION
    assert "Add a bound" in jobs[0].payload["instruction"]


def test_cancel_retires_buffer_and_batch_atomically(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = store.reviews.for_job(claim(store, clock)[0])
    ingest(store, flow, feedback(clock, 102), "next")
    ControlService(store).cancel(flow)
    assert store.reviews.get_batch(batch.id).state is BatchState.CANCELLED
    assert claim(store, clock) == []


@pytest.mark.parametrize(
    "actor,body,kind",
    [
        ("alice", "@flowtool[bot] add a bound", JobKind.REVISION),
        ("outsider", "@flowtool[bot] add a bound", JobKind.TRIAGE),
        ("alice", "> @flowtool[bot] add a bound", JobKind.TRIAGE),
        ("alice", "```\n@flowtool[bot] add a bound\n```", JobKind.TRIAGE),
    ],
)
async def test_strong_commands_require_author_and_unquoted_instruction(
    store,
    review_flow,
    actor,
    body,
    kind,
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
    await router.handle(
        "issue_comment",
        "comment",
        {
            "action": "created",
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 17},
            "sender": {"login": actor, "type": "User"},
            "comment": {
                "id": 101,
                "body": body,
                "user": {"login": actor},
                "updated_at": clock.now.isoformat(),
            },
        },
    )
    assert claim(store, clock)[0].kind is kind


@pytest.mark.parametrize(
    "state,action,sender,expected",
    [
        ("changes_requested", "submitted", "alice", JobKind.REVISION),
        ("changes_requested", "submitted", "outsider", JobKind.TRIAGE),
        ("changes_requested", "edited", "alice", JobKind.TRIAGE),
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
    await router.handle(
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
    assert [j.kind for j in jobs] == ([] if expected is None else [expected])


def test_upgrade_refuses_unbatched_review_work(store, review_flow):
    flow, _ = review_flow
    job = store.enqueue_job(flow.id, JobKind.TRIAGE)
    with pytest.raises(RuntimeError, match=str(job.id)):
        store.reviews.check_upgrade()


def test_failed_batch_retry_restores_ownership_and_cancel_fences_old_work(store, review_flow):
    from app.models import FlowState

    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    job = claim(store, clock)[0]
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        flow.state = FlowState.FAILED
        tx.update_flow(flow)
        tx.cancel_jobs(flow.id)
    assert store.reviews.get_batch(job.payload["batch_id"]).state is BatchState.CANCELLED
    ControlService(store).retry(flow)
    replacement = store.list_jobs(flow.id)[-1]
    assert replacement.id != job.id
    assert store.reviews.for_job(replacement).state is BatchState.READY
    assert store.reviews.for_job(replacement).feedback[0].github_id == 101


async def run_triage(store, flow, job, verdict, *, before_apply=None):
    github = RecordingGitHub()
    subject = coordinator(store, github)
    store.worker_connected("worker-a", 5, "epoch-a")
    assigned = store.assign_job("worker-a", "token", "epoch-a")
    assert assigned.id == job.id
    task = await subject.prepare(assigned)
    assert store.start_assignment(job.id, flow.current_attempt_id, "worker-a", "token", "epoch-a")
    subject.mark_started(assigned, task, "worker-a")
    result = TriageResult(batch_id=task.batch_id, feedback_refs=task.feedback, verdict=verdict)
    message = Succeeded(
        job_id=job.id, assignment_token="token", connection_epoch="epoch-a", result=result
    )
    assert (
        store.record_assignment_result(
            job.id,
            "worker-a",
            "token",
            "epoch-a",
            message.model_dump(mode="json"),
            terminal_digest(message),
        )
        == "pending"
    )
    if before_apply:
        before_apply()
    await subject.apply_result(assigned, result)
    await subject.apply_result(assigned, result)
    return subject, github, result


async def test_ignore_consumes_frozen_input_once_and_leaves_newer_version(store, review_flow):
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
    assert store.get_job(job.id).state is JobState.DONE
    assert [(f.github_id, f.version) for f in store.reviews.pending(flow.id)] == [(101, 2)]


async def test_revise_transfers_batch_to_one_derived_job(store, review_flow):
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
    assert store.reviews.for_job(revisions[0]).id == job.payload["batch_id"]
    assert len(github.comments) == 1
    ingest(store, flow, feedback(clock, 102), "next")
    assert claim(store, clock) == []
