"""Late webhook edits must not revive content already seen in the same timestamp tick."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app.domain.models import ReviewItemKind
from app.github import ReviewThreads
from app.persistence.reviews import ReviewStore
from app.persistence.store import Store
from tests.test_review_batches import batch_for, claim, feedback, ingest
from tests.test_review_batches import (
    review_flow as review_flow,  # noqa: PLC0414 - fixture re-export
)
from tests.test_review_resolution import human, snapshot


def edit(store, flow, clock, delivery, previous, body, kind=ReviewItemKind.INLINE):
    return store.reviews.ingest(
        flow.id, delivery, "pull_request_review_comment",
        {"changes": {"body": {"from": previous}}}, feedback(clock, body=body, kind=kind),
        settle_seconds=75, submit_grace_seconds=15,
    )


def observe(store, flow, clock, body, *, baseline=None):
    frozen = store.reviews.pending(flow.id) if baseline is None else baseline
    remote = ReviewThreads(repo=flow.repo, pr_number=17, fetched_at=clock.now,
        threads=[snapshot(human(101, body=body, updated_at=clock.now))])
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.observe_threads(flow.id, remote, frozen)


def poll(store, flow, clock, body):
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.reviews.observe_feedback(
            flow.id, flow.current_attempt_id,
            [feedback(clock, body=body, kind=ReviewItemKind.CONVERSATION)],
            tx.reviews.observation_boundary(), 75,
        )


# Steps: ("ingest", body), ("observe", body), ("late", previous, body) must be rejected,
# ("edit", previous, body) must be accepted, ("freeze",) captures pending for later observations.
@pytest.mark.parametrize(("steps", "expected"), [
    pytest.param([("ingest", "A"), ("observe", "B"), ("late", "A", "B"), ("observe", "A")],
                 [(3, "A")], id="late-before-same-second-revert"),
    pytest.param([("ingest", "A"), ("observe", "B"), ("observe", "A"), ("late", "A", "B")],
                 [(3, "A")], id="late-after-same-second-revert"),
    # The remote X→A→B edits occurred before the poll; A was never stored locally.
    pytest.param([("ingest", "X"), ("observe", "B"), ("late", "A", "B"), ("observe", "A")],
                 [(3, "A")], id="skipped-predecessor"),
    # The original A webhook was lost; the first local version is already the observed B.
    pytest.param([("observe", "B"), ("late", "A", "B"), ("observe", "A")],
                 [(2, "A")], id="missing-first-webhook"),
    pytest.param([("ingest", "A"), ("freeze",), ("edit", "A", "B"), ("edit", "B", "A"), ("observe", "B")],
                 [(3, "A")], id="query-frozen-before-webhooks"),
])
def test_late_same_second_webhook_cannot_replay_observed_history(store, review_flow, steps, expected):
    flow, clock = review_flow
    frozen = None
    for index, (action, *bodies) in enumerate(steps):
        if action == "ingest":
            ingest(store, flow, feedback(clock, body=bodies[0]))
        elif action == "observe":
            observe(store, flow, clock, bodies[0], baseline=frozen)
        elif action == "freeze":
            frozen = store.reviews.pending(flow.id)
        else:
            assert edit(store, flow, clock, f"{action}-{index}", *bodies) is (action == "edit")
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        assert [(f.version, f.body) for f in restarted.reviews.pending(flow.id)] == expected
        for index, (action, *bodies) in enumerate(steps):
            if action == "late":
                assert not edit(restarted, flow, clock, f"{action}-{index}-again", *bodies)
        batch = batch_for(restarted, claim(restarted, clock)[0])
        assert [(f.version, f.body) for f in batch.feedback] == expected
    finally:
        restarted.close()


@pytest.mark.parametrize("seconds", [0, 1])
def test_repeated_transition_is_new_only_in_a_later_second(store, review_flow, seconds):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="A"))
    for index, (previous, body) in enumerate([("A", "B"), ("B", "A")]):
        clock.now += timedelta(seconds=seconds)
        assert edit(store, flow, clock, str(index), previous, body)
    clock.now += timedelta(seconds=seconds)
    # In one tick, the timestamp and predecessor cannot distinguish a third edit from a redelivery.
    assert edit(store, flow, clock, "repeat", "A", "B") is bool(seconds)
    if not seconds:
        assert store.reviews.pending(flow.id)[0].body == "A"
        observe(store, flow, clock, "B")
    assert [(f.version, f.body) for f in store.reviews.pending(flow.id)] == [(4, "B")]


@pytest.mark.parametrize("source", ["threads", "polling"])
def test_unordered_same_second_edit_waits_for_the_next_observation(store, review_flow, source):
    flow, clock = review_flow
    kind = ReviewItemKind.INLINE if source == "threads" else ReviewItemKind.CONVERSATION
    ingest(store, flow, feedback(clock, body="A", kind=kind))
    # GitHub timestamps have second precision; the B→C webhook can arrive before A→B.
    assert not edit(store, flow, clock, "c", "B", "C", kind)
    assert edit(store, flow, clock, "b", "A", "B", kind)
    assert [(f.version, f.body) for f in store.reviews.pending(flow.id)] == [(2, "B")]
    if source == "threads":
        observe(store, flow, clock, "C")
    else:
        poll(store, flow, clock, "C")
    assert not edit(store, flow, clock, "c-again", "B", "C", kind)
    queued = claim(store, clock)
    assert len(queued) == 1
    assert [(f.version, f.body) for f in batch_for(store, queued[0]).feedback] == [(3, "C")]


def test_late_edit_rolls_back_with_its_delivery_reservation(store, review_flow, monkeypatch):
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
    assert not store.webhooks.event_seen("late-b")
    assert not edit(store, flow, clock, "late-b", "A", "B")
    observe(store, flow, clock, "A")
    assert store.reviews.pending(flow.id)[0].body == "A"


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
