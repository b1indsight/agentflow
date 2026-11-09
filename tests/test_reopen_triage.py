"""A reopened historical PR cannot authorize work from its archived review backlog."""

from datetime import timedelta

import pytest

from app.domain.context_models import MAX_DISCUSSIONS
from app.domain.models import ArchiveReason, FlowState, JobKind
from app.domain.review_models import BatchState, FeedbackRef
from app.persistence.store import Store
from tests.test_pr_lifecycle import GitHub, check
from tests.test_pr_lifecycle import lifecycle_flow as lifecycle_flow  # noqa: PLC0414
from tests.test_pr_lifecycle import lifecycle_resources as lifecycle_resources  # noqa: PLC0414
from tests.test_review_batches import batch_for, feedback, ingest, settle
from tests.test_review_batches import review_flow as review_flow  # noqa: PLC0414


@pytest.mark.parametrize("outcome", [BatchState.COMPLETED, BatchState.WAITING])
async def test_reopen_retained_signals_stay_retired_across_batches_and_restart(
    store, lifecycle_flow, outcome, lifecycle_resources,
):
    flow, clock = lifecycle_flow
    # A backlog larger than one bounded batch must stay inert after close/reopen,
    # regardless of whether the first discussion completed or awaited clarification.
    for index in range(MAX_DISCUSSIONS + 1):
        ingest(store, flow, feedback(clock, github_id=1000 + index), f"old-{index}")
    clock.now += timedelta(seconds=76)
    first = settle(store)[0]
    assert first.job_id is None
    batch = batch_for(store, first)
    assert len(batch.feedback) == MAX_DISCUSSIONS
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.relayed(batch_for(tx, first))
        consumed = ([FeedbackRef(item_id=f.item_id, version=f.version) for f in batch.feedback]
                    if outcome is BatchState.COMPLETED else [])
        tx.reviews.complete(batch.id, outcome, consumed, [])
    await check(store, flow, GitHub(flow))
    await check(store, flow, GitHub(flow, "open"))
    archived = store.flows.get_round(flow.id, 1)
    assert archived is not None and archived.archive_reason is ArchiveReason.ABANDONED
    assert settle(store) == []

    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        assert settle(restarted) == []
        assert restarted.jobs.list_jobs(flow.id) == []
        assert restarted.flows.get_round(flow.id, 1) == archived
    finally:
        restarted.close()


@pytest.mark.parametrize("new_kind", ["new_review", "edited_version"])
async def test_reopen_rejects_duplicate_and_new_authorization_for_archived_round(
    store,
    lifecycle_flow,
    new_kind,
    lifecycle_resources,
):
    flow, clock = lifecycle_flow
    old = feedback(clock, signal="request_changes", submitted=True)
    ingest(store, flow, old)
    await check(store, flow, GitHub(flow))
    await check(store, flow, GitHub(flow, "open"))
    # A duplicate webhook or a poll of unchanged content must not revive old authority.
    assert not ingest(store, flow, old, "duplicate")
    with store.connection() as connection:
        row = connection.execute("SELECT content,consumed_at,relayed_at FROM review_versions").fetchone()
    assert row["content"]["body"] == old.body
    assert row["consumed_at"] is None and row["relayed_at"] is None
    assert settle(store) == []

    clock.now += timedelta(seconds=1)
    new = feedback(clock, github_id=102 if new_kind == "new_review" else 101,
                   body="A new requested change", signal="request_changes", submitted=True)
    assert not ingest(store, flow, new, "new-signal")
    clock.now += timedelta(seconds=76)
    assert settle(store) == []
    # Repeated lifecycle observations still grant no new round or coding authority.
    await check(store, flow, GitHub(flow))
    await check(store, flow, GitHub(flow, "open"))
    assert settle(store) == []
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.flows.get_flow(flow.id).current_round_no == 1
    assert store.jobs.list_jobs(flow.id) == []


async def test_empty_reopen_batch_does_not_restore_retired_review_authorization(
    store, lifecycle_flow, lifecycle_resources,
):
    flow, clock = lifecycle_flow
    ingest(store, flow, feedback(clock, body="", signal="request_changes", submitted=True))
    await check(store, flow, GitHub(flow))
    await check(store, flow, GitHub(flow, "open"))
    assert settle(store) == []
    clock.now += timedelta(seconds=1)
    assert not ingest(store, flow, feedback(clock, github_id=102), "new-discussion")
    clock.now += timedelta(seconds=76)
    assert settle(store) == []
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED


def test_reopen_authorization_retirement_rolls_back_with_the_workflow(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, signal="request_changes", submitted=True))
    # Failure to commit reopening must not silently revoke still-valid review authority.
    with pytest.raises(RuntimeError, match="reopen interrupted"), store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.reopened(flow.id, 0)
        raise RuntimeError("reopen interrupted")
    assert store.reviews._has_unused_signal(flow.id)
    clock.now += timedelta(seconds=76)
    assert settle(store)[0].kind is JobKind.REVISION
