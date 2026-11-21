"""Round ownership survives delayed observations and resumed Flows on PostgreSQL."""

from datetime import timedelta

import pytest

from app.controler.workflow.service import ControlService
from app.domain.inference_models import InferenceMessage, InferenceSummary, InferenceTask
from app.domain.models import ArchiveReason, DeliveryState, Flow, FlowState
from app.github.console import Observation
from app.persistence.activity import ActivityStore
from app.persistence.console_sync import ConsoleSyncStore
from app.persistence.inference_notices import InferenceNoticeStore
from app.persistence.store import Store
from tests.test_review_batches import claim, feedback, ingest
from tests.test_review_batches import review_flow as review_flow  # noqa: PLC0414


def attach_pr(store: Store, flow: Flow, number: int) -> None:
    delivery = store.deliveries.get_attempt_delivery(flow.required_attempt_id)
    if delivery is None:
        delivery = store.deliveries.get_or_create_delivery(flow.required_attempt_id, flow.initial_branch)
    delivery.pr_number, delivery.state, delivery.head_sha = number, DeliveryState.OPEN, "a" * 40
    store.deliveries.update_delivery(delivery)


def archive(store: Store, flow: Flow) -> Flow:
    ControlService(database=store.database).finish(
        flow, reason=ArchiveReason.ABANDONED, summary="Stopped this round",
        expected_attempt_id=flow.required_attempt_id,
    )
    archived = store.flows.get_flow(flow.id)
    assert archived is not None and archived.state is FlowState.ARCHIVED
    return archived


def test_foreign_worker_attempt_cannot_publish_or_advance_checkpoint(store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    foreign = store.flows.create_flow("owner/repo", 8, 42)
    attempt = store.flows.get_current_attempt(foreign)
    assert attempt is not None
    with store.connection() as connection:
        connection.execute("UPDATE console_sync SET pending=(source='worker' AND flow_id=%s)", (flow.id,))
    sync = ConsoleSyncStore(store.database)
    pending = sync.claim()
    assert pending is not None
    item = Observation(
        key="worker:foreign", source="worker", kind="message", author="assistant",
        occurred_at=flow.created_at, updated_at=flow.created_at,
        attempt_id=attempt.id,
    )
    activity = ActivityStore(store.database)
    before = activity.page(flow.id)
    with pytest.raises(ValueError, match="Attempt does not belong"):
        sync.finish(pending, [item], cursor="eof", more=False, limitations=[])
    assert activity.page(flow.id) == before
    with store.connection() as connection:
        checkpoint = connection.execute(
            "SELECT cursor,claim_token FROM console_sync WHERE flow_id=%s AND source='worker'",
            (flow.id,),
        ).fetchone()
    assert checkpoint == {"cursor": None, "claim_token": pending.claim_token}


def test_stale_pr_claim_cannot_publish_or_create_children_after_resume(store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    attach_pr(store, flow, 17)
    with store.connection() as connection:
        connection.execute("UPDATE console_sync SET pending=FALSE")
        connection.execute("INSERT INTO console_sync(flow_id,source) VALUES(%s,'pr.comments')", (flow.id,))
    sync = ConsoleSyncStore(store.database)
    pending = sync.claim()
    assert pending is not None and pending.attempt_id == flow.current_attempt_id and pending.pr_number == 17
    resumed = ControlService(database=store.database).resume(archive(store, flow), expected_attempt_id=flow.required_attempt_id)
    attach_pr(store, resumed, 18)
    activity = ActivityStore(store.database)
    before = activity.page(flow.id)
    item = Observation(
        key="github:owner/repo:comment:old", source="pr.comments", kind="comment", author="reviewer",
        occurred_at=flow.created_at, updated_at=flow.created_at,
    )
    assert not sync.finish(
        pending, [item], cursor="old-next", more=True, limitations=[], children=["comment.reactions:9"],
    )
    assert activity.page(flow.id) == before
    # The Attempt/PR fence also refuses a source row restored from an older checkpoint.
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO console_sync(flow_id,source,claim_token) VALUES(%s,%s,%s)",
            (flow.id, pending.source, pending.claim_token),
        )
    before = activity.page(flow.id)
    assert not sync.finish(
        pending, [item], cursor="old-next", more=True, limitations=[], children=["comment.reactions:9"],
    )
    assert activity.page(flow.id) == before
    sync.fail(pending, "rate_limited", delay=60)
    with store.connection() as connection:
        assert connection.execute("SELECT 1 FROM console_installation_backoff").fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM console_sync WHERE flow_id=%s AND source='comment.reactions:9'", (flow.id,),
        ).fetchone() is None


@pytest.mark.parametrize("new_flow", [False, True], ids=["resumed-round", "new-issue-owner"])
def test_old_pr_webhooks_find_original_owner_but_issue_content_stays_shared(store, new_flow):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    attach_pr(store, flow, 17)
    archived = archive(store, flow)
    current = (
        store.flows.create_flow(flow.repo, flow.issue_number, 42) if new_flow
        else ControlService(database=store.database).resume(archived, expected_attempt_id=flow.required_attempt_id)
    )
    attach_pr(store, current, 18)
    # A PR number is not the Issue number of a different Flow in the same repository.
    unrelated = store.flows.create_flow(flow.repo, 17, 42)
    sync = ConsoleSyncStore(store.database)
    old_pr = {
        "repository": {"full_name": "OWNER/REPO"}, "action": "created",
        "issue": {"number": 17, "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/17"}},
        "comment": {"id": 101, "body": "old PR", "created_at": current.updated_at.isoformat()},
    }
    with store.connection() as connection:
        connection.execute("UPDATE console_sync SET pending=FALSE WHERE flow_id=%s", (current.id,))
    sync.observe_webhook("issue_comment", old_pr)
    with store.connection() as connection:
        assert connection.execute("SELECT 1 FROM console_sync WHERE flow_id=%s AND pending",
                                  (current.id,)).fetchone() is None
    sync.observe_webhook("issue_comment", {
        **old_pr, "issue": {"number": 18, "pull_request": old_pr["issue"]["pull_request"]},
        "comment": {**old_pr["comment"], "id": 102, "body": "new PR"},
    })
    sync.observe_webhook("issue_comment", {
        **old_pr, "issue": {"number": 7},
        "comment": {**old_pr["comment"], "id": 103, "body": "shared Issue"},
    })
    # A later edit keeps the same external identity and original round.
    sync.observe_webhook("issue_comment", {
        **old_pr, "action": "edited", "comment": {
            **old_pr["comment"], "body": "old PR edited",
            "updated_at": (current.updated_at + timedelta(seconds=1)).isoformat(),
        },
    })
    activity = ActivityStore(store.database)
    old = {item.key: item for item in activity.page(flow.id).items}
    fresh = {item.key: item for item in activity.page(current.id).items}
    assert old["github:owner/repo:comment:101"].attempt_id == flow.current_attempt_id
    assert fresh["github:owner/repo:comment:102"].attempt_id == current.current_attempt_id
    assert old["github:owner/repo:comment:103"].attempt_id is None
    assert fresh["github:owner/repo:comment:103"].attempt_id is None
    assert all(item.attempt_id == flow.current_attempt_id for item in activity.versions(flow.id, "github:owner/repo:comment:101").items)
    assert activity.page(unrelated.id).items == []
    if new_flow:
        assert "github:owner/repo:comment:101" not in fresh
        assert "github:owner/repo:comment:102" not in old
    assert store.flows.get_flow(current.id) == current


@pytest.mark.parametrize("source,kind,expected", [
    ("pr.comments", "comment", True),
    ("pr.review_threads", "thread", True),
    ("review.reactions:review-id", "reaction", True),
    ("issue.comments", "comment", None),
    ("comment.reactions:12", "reaction", None),
    ("pr.commits", "commit", None),
    ("commit.check_runs:" + "a" * 40, "check", None),
])
def test_remote_claims_only_attribute_pr_owned_objects(store, source, kind, expected):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    resumed = ControlService(database=store.database).resume(archive(store, flow), expected_attempt_id=flow.required_attempt_id)
    attach_pr(store, resumed, 18)
    with store.connection() as connection:
        connection.execute("UPDATE console_sync SET pending=FALSE")
        connection.execute("INSERT INTO console_sync(flow_id,source) VALUES(%s,%s)", (flow.id, source))
    sync = ConsoleSyncStore(store.database)
    pending = sync.claim()
    assert pending is not None and pending.attempt_id == resumed.current_attempt_id
    item = Observation(
        key="github:owner/repo:remote:12", source=source, kind=kind, author="reviewer",
        occurred_at=flow.created_at, updated_at=flow.created_at,
    )
    assert sync.finish(pending, [item], cursor=None, more=False, limitations=[])
    entry, = ActivityStore(store.database).page(flow.id).items
    assert entry.attempt_id == (resumed.current_attempt_id if expected else None)


def test_delayed_inference_notice_uses_frozen_job_and_batch_identity(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch, = claim(store, clock)
    task = InferenceTask(messages=[InferenceMessage(role="user", content="Review feedback")])
    job = store.inference.enqueue_inference(task, flow_id=flow.id, batch_id=batch.id)
    resumed = ControlService(database=store.database).resume(archive(store, flow), expected_attempt_id=flow.required_attempt_id)
    assert resumed.current_attempt_id != flow.current_attempt_id
    summary = InferenceSummary(
        logical_call_id=task.request_id, attempts=3, stop_reason="max_attempts",
        last_failure_code="execution_timeout", finished_at=clock.now, elapsed_ms=3000,
    )
    notices = InferenceNoticeStore(store.database)
    assert notices.append(flow.id, batch.id, summary)
    with store.connection() as connection:
        entry = connection.execute(
            "SELECT attempt_id,source FROM console_activity WHERE flow_id=%s AND key=%s",
            (flow.id, f"inference-exhausted:{batch.id}"),
        ).fetchone()
    assert entry == {"attempt_id": job.attempt_id, "source": "flow"}
    assert notices.page(flow.id).items[0].historical
    assert not notices.append(flow.id, batch.id, summary)
    assert ActivityStore(store.database).page(flow.id).items == []
    with pytest.raises(ValueError, match="does not belong"):
        notices.append(flow.id, batch.id, summary.model_copy(update={"logical_call_id": "f" * 32}))
