"""Durable feedback must survive crashes without consuming a newer discussion version."""

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.controler.events.router import EventRouter
from app.controler.workflow.service import ControlService
from app.domain.context_models import ReviewContext
from app.domain.flow_policy import FlowEvent
from app.domain.judge_models import JudgeDecision
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
from app.github import PullRequest
from app.persistence.jobs import JobStore
from app.persistence.reviews import ReviewStore
from app.persistence.store import Store
from tests.flow_fixtures import apply_flow_event
from tests.github import RecordingGitHub
from tests.review_memory import feedback_threads, judge_decision, review_context
from tests.test_coordinator import coordinator


def open_flow(store, issue_number=7, pr_number=17):
    flow = store.flows.create_flow("owner/repo", issue_number, 42)
    flow = apply_flow_event(store, flow, FlowEvent.DELIVERY_APPLIED)
    delivery = store.deliveries.get_or_create_delivery(flow.required_attempt_id, f"flow/{flow.id}")
    delivery.pr_number, delivery.state = pr_number, DeliveryState.OPEN
    store.deliveries.update_delivery(delivery)
    return flow


@pytest.fixture
def review_flow(store, monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 9, 7, tzinfo=UTC))
    monkeypatch.setattr(store._db, "_clock", lambda: clock.now)
    return open_flow(store), clock


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
    return [store.jobs.get_job(b.job_id) if b.job_id is not None else b for b in store.reviews.claim_due()]


def claim(store, clock):
    clock.now += timedelta(seconds=76)
    return settle(store)


@pytest.mark.parametrize("legacy_column", [False, True])
def test_legacy_frozen_context_keeps_digest_and_completed_decision(store, review_flow, legacy_column):
    # Old snapshots lack task_content. Adding a serialized null would invalidate saved decisions.
    if legacy_column:
        with store.connection() as connection:
            connection.execute(
                "ALTER TABLE review_batches ADD COLUMN answer_dispatched BOOLEAN NOT NULL DEFAULT FALSE"
            )
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = batch_for(store, claim(store, clock)[0])
    legacy = review_context(batch.id).model_dump_json(exclude={"task_content"})
    digest = hashlib.sha256(legacy.encode()).hexdigest()
    snapshot = ReviewContext.model_validate_json(legacy)
    assert snapshot.task_content is None
    assert snapshot.model_dump_json() == legacy and snapshot.digest == digest
    decision = JudgeDecision(
        context_digest=digest, session_id="legacy", model="fake",
        verdict=TriageVerdict(verdict=Verdict.ANSWER, confidence=1, reason="Known", answer="Ten"),
    )
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        assert tx.reviews.freeze_context(batch, snapshot) == snapshot
        tx.reviews.record_decision(batch, decision)
        tx.reviews.complete(batch.id, BatchState.COMPLETED, list(batch.feedback), [])
    with store.connection() as connection:
        stored = connection.execute(
            "SELECT context FROM review_batches WHERE id=%s", (batch.id,),
        ).fetchone()["context"]
        assert "task_content" not in stored
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        fresh = restarted.reviews.get_batch(batch.id)
        assert fresh.context.digest == digest and fresh.decision == decision
        assert fresh.state is BatchState.COMPLETED
    finally:
        restarted.close()


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
    assert not store.webhooks.event_seen("delivery-1")
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
    original = JobStore.enqueue_job

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("claim interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(JobStore, "enqueue_job", interrupted)
        with pytest.raises(RuntimeError, match="claim interrupted"):
            claim(store, clock)
    assert store.jobs.list_jobs(flow.id) == []
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


def test_unchanged_comment_from_another_flow_is_not_new_feedback(store, review_flow):
    # A Flow resumed on the same Issue polls comments already handled by its predecessor.
    earlier, clock = review_flow
    comment = feedback(clock, 202, kind=ReviewItemKind.CONVERSATION, body="Keep the limit at 10.")
    ingest(store, earlier, comment)
    later = open_flow(store, issue_number=8, pr_number=18)

    def poll(update):
        with store.transaction() as tx:
            tx.lock_workflow(later.id)
            tx.reviews.observe_feedback(later.id, later.current_attempt_id, [update],
                                        tx.reviews.observation_boundary(), 75)

    poll(comment)
    assert store.reviews.pending(later.id) == []
    clock.now += timedelta(seconds=1)
    poll(comment.model_copy(update={"body": "Use 20 instead.", "updated_at": clock.now}))
    assert [(f.item_id, f.version, f.body) for f in store.reviews.pending(later.id)] == [
        (202 * 4 + 3, 2, "Use 20 instead."),
    ]
    assert store.reviews.pending(earlier.id) == []


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
    ControlService(
        database=store.database,
    ).cancel(flow)
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
        trigger_label="flow",
        settle_seconds=75,
        submit_grace_seconds=15,
    )
    router = EventRouter(
        ControlService(
            database=store.database,
        ),
        settings,
        bot_login="flowtool[bot]",
        database=store.database,
    )
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
        trigger_label="flow",
        settle_seconds=75,
        submit_grace_seconds=15,
    )
    router = EventRouter(
        ControlService(
            database=store.database,
        ),
        settings,
        bot_login="flowtool[bot]",
        database=store.database,
    )
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


def test_blocked_batch_retry_preserves_frozen_feedback_without_dispatching(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, signal="request_changes"))
    job = claim(store, clock)[0]
    with store.transaction() as tx:
        flow = apply_flow_event(tx, flow, FlowEvent.RESULT_FAILED)
        assert tx.reviews.get_batch(batch_for(tx, job).id).state is BatchState.READY
        tx.assignments.retire_queued_work(flow.id)
    assert store.reviews.get_batch(batch_for(store, job).id).state is BatchState.CANCELLED
    assert settle(store) == []
    ControlService(
        database=store.database,
    ).retry(flow)
    replacement = store.jobs.list_jobs(flow.id)[-1]
    assert replacement.id != job.id
    assert batch_for(store, replacement).state is BatchState.READY
    assert batch_for(store, replacement).feedback[0].github_id == 101


async def run_triage(store, flow, batch, verdict, *, before_apply=None, github=None):
    github = github or RecordingGitHub()
    github.threads = feedback_threads(batch.feedback)
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
        assert store.jobs.list_jobs(flow.id)[-1].kind is expected
    else:
        assert batch.state is BatchState.WAITING
        assert github.comments == []
        assert not claim(store, clock)


@pytest.mark.parametrize("change", ["cancel", "closed", "head"])
async def test_answer_rechecks_lifecycle_and_remote_head_before_queueing(
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
            ControlService(
                database=store.database,
            ).cancel(flow)

    await run_triage(store, flow, job, TriageVerdict(verdict=Verdict.ANSWER, instruction="Explain the bound"),
                     github=github, before_apply=change_state)
    assert github.comments == []
    assert store.jobs.list_jobs(flow.id) == []
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


@pytest.mark.parametrize("outcome", [BatchState.COMPLETED, BatchState.WAITING])
def test_retry_after_successful_replacement_does_not_select_historical_failure(
    store, review_flow, outcome,
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, signal="request_changes"))
    original = claim(store, clock)[0]
    service = ControlService(
        database=store.database,
    )
    flow = apply_flow_event(store, flow, FlowEvent.RESULT_FAILED)
    store.assignments.retire_queued_work(flow.id)
    service.retry(flow)
    replacement = store.jobs.list_jobs(flow.id)[-1]
    batch = batch_for(store, replacement)
    store.reviews.complete(batch.id, outcome, [], [])
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET state='done' WHERE id=%s", (replacement.id,))

    # A new round must not resurrect the old failure whose owner was replaced.
    service.cancel(flow)
    with pytest.raises(ValueError, match="archived"):
        service.retry(flow)
    resumed = service.resume(flow, expected_attempt_id=flow.required_attempt_id)
    assert store.jobs.get_job(original.id).state is JobState.FAILED
    assert store.jobs.list_jobs(flow.id)[-1].kind is JobKind.INITIAL
    assert resumed.state is FlowState.QUEUED
    assert resumed.current_attempt_id != flow.current_attempt_id


async def test_answer_carries_waiting_questions_into_worker_task(store, review_flow, recording_resources):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    job = claim(store, clock)[0]
    await run_triage(store, flow, job, TriageVerdict(
        verdict=Verdict.WAIT, open_points=["Why is the limit 10?"],
    ))
    ingest(store, flow, feedback(clock, 102, body="Please explain it."), "reply")
    job = claim(store, clock)[0]
    _, github, _ = await run_triage(store, flow, job, TriageVerdict(
        verdict=Verdict.ANSWER, instruction="Explain the configured retry limit.", confidence=0.9,
    ))
    batch = batch_for(store, job)
    assert "Why is the limit 10?" in batch.context.open_points
    assert batch.decision.verdict.instruction == "Explain the configured retry limit."
    task, = store.jobs.list_jobs(flow.id)
    assert task.kind is JobKind.REVISION and task.payload["purpose"] == "answer"
    assert task.payload["instruction"] == "Explain the configured retry limit."
    assert batch.job_id == task.id and batch.state is BatchState.READY
    assert store.reviews.pending(flow.id)
    assert github.comments == []


@pytest.mark.parametrize("approval_id", [101, 102])
async def test_approval_invalidates_body_without_waking_waiting_feedback(
    store, review_flow, approval_id,
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, kind=ReviewItemKind.REVIEW_BODY))
    ingest(store, flow, feedback(clock, 201), "inline")
    batch = batch_for(store, claim(store, clock)[0])
    store.reviews.complete(batch.id, BatchState.WAITING, [], ["Which limit?"])
    router = EventRouter(
        ControlService(
            database=store.database,
        ),
        SimpleNamespace(
            allowlist=["alice"],
            trigger_label="flow",
            settle_seconds=75,
            submit_grace_seconds=15,
        ),
        bot_login="flowtool[bot]",
        database=store.database,
    )
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


LEGACY_REVIEW_SCHEMA = """
DROP TABLE review_versions;
ALTER TABLE review_buffers ADD COLUMN generation BIGINT NOT NULL DEFAULT 1,
  ADD COLUMN force_triage BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE review_batches ADD COLUMN generation BIGINT NOT NULL DEFAULT 1,
  ADD UNIQUE(flow_id,generation);
CREATE TABLE review_items (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  flow_id BIGINT NOT NULL REFERENCES flows(id), kind TEXT NOT NULL, github_id BIGINT NOT NULL,
  version INTEGER NOT NULL, observed_at TIMESTAMPTZ NOT NULL, UNIQUE(flow_id,kind,github_id)
);
CREATE TABLE review_versions (
  item_id BIGINT NOT NULL REFERENCES review_items(id), version INTEGER NOT NULL,
  attempt_id BIGINT NOT NULL REFERENCES attempts(id), source_delivery TEXT, content JSONB NOT NULL,
  relayed_at TIMESTAMPTZ, consumed_at TIMESTAMPTZ, addressed_sha TEXT,
  addressed_job_id BIGINT REFERENCES jobs(id), signal_retired BOOLEAN NOT NULL DEFAULT FALSE,
  observation_seq BIGINT GENERATED ALWAYS AS IDENTITY, PRIMARY KEY(item_id,version)
);
CREATE TABLE review_pending_edits (
  source_delivery TEXT PRIMARY KEY, item_id BIGINT NOT NULL REFERENCES review_items(id),
  attempt_id BIGINT NOT NULL REFERENCES attempts(id), previous_body TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL, content JSONB NOT NULL
);
CREATE TABLE review_thread_observations (
  flow_id BIGINT NOT NULL REFERENCES flows(id), thread_id TEXT NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL, snapshot JSONB NOT NULL, PRIMARY KEY(flow_id,thread_id)
);
"""


def legacy_reviews(store, rows):
    """Recreate the pre-merge review tables (frozen from 72a9f6b) holding (flow, versions) rows."""
    with store.connection() as connection:
        connection.execute(LEGACY_REVIEW_SCHEMA)
        for flow, versions in rows:
            current = versions[-1]
            item = connection.execute(
                "INSERT INTO review_items(flow_id,kind,github_id,version,observed_at)"
                " VALUES(%s,%s,%s,%s,%s) RETURNING id",
                (flow.id, current.kind, current.github_id, len(versions), current.updated_at),
            ).fetchone()["id"]
            for version, update in enumerate(versions, 1):
                connection.execute(
                    "INSERT INTO review_versions(item_id,version,attempt_id,source_delivery,content)"
                    " VALUES(%s,%s,%s,%s,%s)",
                    (item, version, flow.current_attempt_id, f"legacy-{item}-{version}",
                     Jsonb(update.model_dump(mode="json") | {"inert": False})),
                )
        first_flow, first_versions = rows[0]
        connection.execute(
            "INSERT INTO review_pending_edits VALUES('queued',1,%s,'B',%s,'{}')",
            (first_flow.current_attempt_id, first_versions[0].updated_at),
        )
        connection.execute(
            "INSERT INTO review_thread_observations VALUES(%s,'T1',now(),'{}')", (first_flow.id,),
        )


def test_legacy_review_tables_merge_once_and_keep_frozen_identities(store, review_flow):
    flow, clock = review_flow
    legacy_reviews(store, [
        (flow, [feedback(clock, body="A"), feedback(clock, body="B")]),
        (flow, [feedback(clock, 202, kind=ReviewItemKind.CONVERSATION, body="C")]),
    ])
    store.init_db()
    store.init_db()
    with store.connection() as connection:
        remaining = [
            connection.execute("SELECT to_regclass(%s) AS name", (table,)).fetchone()["name"]
            for table in ("review_items", "review_pending_edits", "review_thread_observations")
        ]
        rows = connection.execute(
            "SELECT item_id,version,kind,github_id FROM review_versions ORDER BY 1,2"
        ).fetchall()
        current = connection.execute(
            "SELECT DISTINCT ON (kind,github_id) item_id,version FROM review_versions"
            " ORDER BY kind,github_id,version DESC"
        ).fetchall()
        dropped = connection.execute(
            "SELECT table_name,column_name FROM information_schema.columns"
            " WHERE table_schema=current_schema() AND table_name LIKE 'review%%'"
            " AND column_name IN ('generation','force_triage','signal_retired')"
        ).fetchall()
    assert remaining == [None, None, None]
    assert dropped == []
    assert [tuple(row.values()) for row in rows] == [
        (1, 1, "inline", 101), (1, 2, "inline", 101), (2, 1, "conversation", 202),
    ]
    assert sorted(tuple(row.values()) for row in current) == [(1, 2), (2, 1)]
    # Upgraded objects keep their frozen item IDs; only new objects use the encoded identity.
    clock.now += timedelta(seconds=1)
    assert ingest(store, flow, feedback(clock, body="D"), "after-upgrade")
    assert ingest(store, flow, feedback(clock, 303), "new-object")
    assert [(f.item_id, f.version) for f in store.reviews.pending(flow.id)] == [
        (1, 3), (2, 1), (303 * 4 + 2, 1),
    ]


def test_legacy_object_shared_by_flows_stops_the_upgrade(store, review_flow):
    flow, clock = review_flow
    other = open_flow(store, issue_number=8, pr_number=18)
    legacy_reviews(store, [(flow, [feedback(clock)]), (other, [feedback(clock)])])
    with pytest.raises(psycopg.errors.RaiseException, match="several Flows"):
        store.init_db()
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM review_items").fetchone()["n"] == 2
