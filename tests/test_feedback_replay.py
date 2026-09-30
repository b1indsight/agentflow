"""Late webhook edits must not revive content already seen in the same timestamp tick."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app.domain.github_models import ReviewThreads
from app.persistence.review_store import ReviewStore
from app.persistence.store import Store
from tests.test_review_batches import feedback, ingest
from tests.test_review_batches import (
    review_flow as review_flow,  # noqa: PLC0414 - fixture re-export
)
from tests.test_review_resolution import human, snapshot


def edit(store, flow, clock, delivery, previous, body):
    return store.reviews.ingest(
        flow.id, delivery, "pull_request_review_comment",
        {"changes": {"body": {"from": previous}}}, feedback(clock, body=body),
        settle_seconds=75, submit_grace_seconds=15,
    )


def observe(store, flow, clock, body, *, baseline=None):
    frozen = store.reviews.pending(flow.id) if baseline is None else baseline
    remote = ReviewThreads(repo=flow.repo, pr_number=17, fetched_at=clock.now,
        threads=[snapshot(human(101, body=body, updated_at=clock.now))])
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.observe_threads(flow.id, remote, frozen)


@pytest.mark.parametrize("late_after_revert", [False, True])
def test_observed_edit_does_not_replay_after_same_second_revert(
    store, review_flow, late_after_revert,
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="A"))
    observe(store, flow, clock, "B")
    # GitHub can deliver A→B after we observed B, either before or after B→A.
    if not late_after_revert:
        assert not edit(store, flow, clock, "late-b", "A", "B")
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        observe(restarted, flow, clock, "A")
        if late_after_revert:
            assert not edit(restarted, flow, clock, "late-b", "A", "B")
        assert [(f.version, f.body) for f in restarted.reviews.pending(flow.id)] == [(3, "A")]
        batch = restarted.reviews.for_job(restarted.reviews.claim_due()[0])
        assert [(f.version, f.body) for f in batch.feedback] == [(3, "A")]
        assert not edit(restarted, flow, clock, "late-b", "A", "B")
    finally:
        restarted.close()


def test_repeated_transition_in_a_later_second_is_new_feedback(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="A"))
    for index, (previous, body) in enumerate([("A", "B"), ("B", "A"), ("A", "B")]):
        clock.now += timedelta(seconds=1)
        assert edit(store, flow, clock, str(index), previous, body)
    assert [(f.version, f.body) for f in store.reviews.pending(flow.id)] == [(4, "B")]


def test_ambiguous_repeat_waits_for_a_fresh_observation(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="A"))
    edit(store, flow, clock, "b", "A", "B")
    edit(store, flow, clock, "a", "B", "A")
    # The timestamp and predecessor cannot distinguish a third edit from a redelivery.
    assert not edit(store, flow, clock, "maybe-new-b", "A", "B")
    assert store.reviews.pending(flow.id)[0].body == "A"
    observe(store, flow, clock, "B")
    assert [(f.version, f.body) for f in store.reviews.pending(flow.id)] == [(4, "B")]


def test_competing_edit_successors_are_not_ordered_by_delivery_id(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="Original"))
    edit(store, flow, clock, "first", "A", "B")
    edit(store, flow, clock, "second", "A", "C")
    # Two edits have the same missing predecessor; neither delivery ID proves the ordering.
    observe(store, flow, clock, "A")
    assert store.reviews.pending(flow.id)[0].body == "A"
    observe(store, flow, clock, "C")
    assert store.reviews.pending(flow.id)[0].body == "C"


def test_observation_covering_skipped_predecessor_cannot_replay(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="X"))
    # The remote X→A→B edits occurred before the poll; A was never stored locally.
    observe(store, flow, clock, "B")
    assert not edit(store, flow, clock, "late-a-b", "A", "B")
    observe(store, flow, clock, "A")
    assert store.reviews.pending(flow.id)[0].body == "A"


def test_incoming_edit_cannot_bypass_pending_competing_successors(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="Original"))
    edit(store, flow, clock, "queued-b", "A", "B")
    edit(store, flow, clock, "queued-c", "A", "C")
    edit(store, flow, clock, "arriving-a", "Original", "A")
    assert not edit(store, flow, clock, "late-d", "A", "D")
    assert store.reviews.pending(flow.id)[0].body == "A"


def test_late_edit_reservation_rolls_back_with_the_observation(store, review_flow, monkeypatch):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="A"))
    observe(store, flow, clock, "B")
    original = ReviewStore._append_version

    def interrupted(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("after version decision")

    with monkeypatch.context() as patch:
        patch.setattr(ReviewStore, "_append_version", interrupted)
        with pytest.raises(RuntimeError, match="version decision"):
            edit(store, flow, clock, "late-b", "A", "B")
    assert not store.event_seen("late-b")
    assert not edit(store, flow, clock, "late-b", "A", "B")
    observe(store, flow, clock, "A")
    assert store.reviews.pending(flow.id)[0].body == "A"


def test_query_frozen_before_a_webhook_does_not_revert_it(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="A"))
    frozen = store.reviews.pending(flow.id)
    edit(store, flow, clock, "b", "A", "B")
    edit(store, flow, clock, "a", "B", "A")
    observe(store, flow, clock, "B", baseline=frozen)
    assert [(f.version, f.body) for f in store.reviews.pending(flow.id)] == [(3, "A")]


def test_first_remote_observation_does_not_require_a_missing_predecessor(store, review_flow):
    flow, clock = review_flow
    # The original A webhook was lost; our first local version is already the observed B.
    observe(store, flow, clock, "B")
    assert not edit(store, flow, clock, "late-b", "A", "B")
    observe(store, flow, clock, "A")
    assert [(f.version, f.body) for f in store.reviews.pending(flow.id)] == [(2, "A")]


def test_observation_and_late_webhook_serialize_across_connections(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="A"))
    observe(store, flow, clock, "B")
    frozen = store.reviews.pending(flow.id)
    left = Store(store.database_url, clock=lambda: clock.now)
    right = Store(store.database_url, clock=lambda: clock.now)
    try:
        # Both serial orders must be safe when poll and webhook handlers use separate connections.
        with ThreadPoolExecutor(max_workers=2) as pool:
            observed = pool.submit(observe, left, flow, clock, "A", baseline=frozen)
            delivered = pool.submit(edit, right, flow, clock, "late-b", "A", "B")
            observed.result()
            assert not delivered.result()
        assert [(f.version, f.body) for f in store.reviews.pending(flow.id)] == [(3, "A")]
    finally:
        left.close()
        right.close()


def test_deleted_feedback_cannot_be_revived_by_pending_edits(store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="A"))
    observe(store, flow, clock, "B")
    edit(store, flow, clock, "late-b", "A", "B")
    ingest(store, flow, feedback(clock, deleted=True, body=""), "deleted")
    observe(store, flow, clock, "A")
    assert store.reviews.pending(flow.id) == []
