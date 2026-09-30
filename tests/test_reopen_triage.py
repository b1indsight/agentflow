"""Retained review authorization must not escape reopen triage through later batches."""

from datetime import timedelta

import pytest

from app.domain.context_models import MAX_DISCUSSIONS
from app.domain.models import JobKind
from app.domain.review_models import BatchState, FeedbackRef
from app.persistence.store import Store
from tests.test_pr_lifecycle import GitHub, check
from tests.test_pr_lifecycle import lifecycle_flow as lifecycle_flow  # noqa: PLC0414
from tests.test_review_batches import feedback, ingest
from tests.test_review_batches import review_flow as review_flow  # noqa: PLC0414


@pytest.mark.parametrize("outcome", [BatchState.COMPLETED, BatchState.WAITING])
async def test_reopen_retained_signals_stay_retired_across_batches_and_restart(
    store, lifecycle_flow, outcome,
):
    flow, clock = lifecycle_flow
    await check(store, flow, GitHub(flow))
    # The strong signal falls outside the first bounded batch after a real close/reopen.
    for index in range(MAX_DISCUSSIONS + 1):
        ingest(store, flow, feedback(clock, github_id=1000 + index,
                                    signal="request_changes", submitted=True), f"old-{index}")
    await check(store, flow, GitHub(flow, "open"))
    first = store.reviews.claim_due()[0]
    assert first.kind is JobKind.TRIAGE
    batch = store.reviews.for_job(first)
    assert len(batch.feedback) == MAX_DISCUSSIONS
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.started(first)
        consumed = ([FeedbackRef(item_id=f.item_id, version=f.version) for f in batch.feedback]
                    if outcome is BatchState.COMPLETED else [])
        tx.reviews.complete(batch.id, outcome, consumed, [])

    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        second = restarted.reviews.claim_due()[0]
        assert second.kind is JobKind.TRIAGE
        assert any(f.github_id == 1000 + MAX_DISCUSSIONS
                   for f in restarted.reviews.for_job(second).feedback)
    finally:
        restarted.close()


@pytest.mark.parametrize("new_kind", ["new_review", "edited_version"])
async def test_reopen_retires_duplicates_but_preserves_new_authorization(
    store, lifecycle_flow, new_kind,
):
    flow, clock = lifecycle_flow
    old = feedback(clock, signal="request_changes", submitted=True)
    ingest(store, flow, old)
    await check(store, flow, GitHub(flow))
    await check(store, flow, GitHub(flow, "open"))
    # A duplicate webhook or a poll of unchanged content must not revive old authority.
    assert not ingest(store, flow, old, "duplicate")
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.observe_feedback(flow.id, flow.current_attempt_id, [old],
                                    tx.reviews.observation_boundary(), 0)
    assert not store.reviews._has_unused_signal(flow.id)
    with store.connection() as connection:
        row = connection.execute("SELECT content,consumed_at,relayed_at FROM review_versions").fetchone()
    assert row["content"]["body"] == old.body
    assert row["consumed_at"] is None and row["relayed_at"] is None
    first = store.reviews.claim_due()[0]
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.started(first)
        tx.reviews.complete(tx.reviews.for_job(first).id, BatchState.COMPLETED, [], [])

    clock.now += timedelta(seconds=1)
    new = feedback(clock, github_id=102 if new_kind == "new_review" else 101,
                   body="A new requested change", signal="request_changes", submitted=True)
    assert ingest(store, flow, new, "new-signal")
    clock.now += timedelta(seconds=76)
    assert store.reviews.claim_due()[0].kind is JobKind.REVISION
    # A second close/reopen retires the newly retained authority as well.
    await check(store, flow, GitHub(flow))
    await check(store, flow, GitHub(flow, "open"))
    assert not store.reviews._has_unused_signal(flow.id)
    assert store.reviews.claim_due()[0].kind is JobKind.TRIAGE


async def test_empty_reopen_batch_does_not_restore_retired_review_authorization(store, lifecycle_flow):
    flow, clock = lifecycle_flow
    ingest(store, flow, feedback(clock, body="", signal="request_changes", submitted=True))
    await check(store, flow, GitHub(flow))
    await check(store, flow, GitHub(flow, "open"))
    assert store.reviews.claim_due() == []
    clock.now += timedelta(seconds=1)
    ingest(store, flow, feedback(clock, github_id=102), "new-discussion")
    clock.now += timedelta(seconds=76)
    assert store.reviews.claim_due()[0].kind is JobKind.TRIAGE


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
    assert store.reviews.claim_due()[0].kind is JobKind.REVISION
