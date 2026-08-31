from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg.errors import UniqueViolation

from app.models import FlowState, JobKind, JobState, ReviewItem, ReviewItemKind
from app.store import Store


def test_only_one_active_flow_exists_per_issue(store: Store) -> None:
    flow = store.create_flow("owner/repo", 7, installation_id=11)

    with pytest.raises(UniqueViolation):
        store.create_flow("owner/repo", 7, installation_id=11)

    flow.state = FlowState.CANCELLED
    store.update_flow(flow)
    replacement = store.create_flow("owner/repo", 7, installation_id=11)

    assert replacement.id != flow.id
    assert store.get_active_flow("owner/repo", 7) == replacement


def test_attempt_and_job_lifecycle_round_trip(store: Store) -> None:
    flow = store.create_flow("owner/repo", 8, installation_id=12)
    attempt = store.create_attempt(flow.id)
    attempt.branch = "flow/issue-8"
    flow.current_attempt_id = attempt.id
    store.update_attempt(attempt)
    store.update_flow(flow)

    job = store.enqueue_job(flow.id, JobKind.INITIAL, payload={"issue_number": 8})
    claimed = store.assign_job("worker-1", "assignment-1")

    assert store.get_current_attempt(flow) == attempt
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.state is JobState.RUNNING
    assert claimed.attempt == 1
    assert claimed.payload == {"issue_number": 8}
    assert claimed.assigned_worker_id == "worker-1"
    assert claimed.assignment_token == "assignment-1"

    assert store.start_assignment(claimed.id, "worker-1", "wrong") is False
    assert store.start_assignment(claimed.id, "worker-1", "assignment-1") is True
    assert store.heartbeat_assignment(claimed.id, "worker-1", "assignment-1") is True
    assert store.finish_assignment(
        claimed.id, "worker-1", "assignment-1", JobState.DONE
    ) is True
    finished = store.get_job(claimed.id)
    assert finished is not None
    assert finished.state is JobState.DONE


def test_only_unstarted_remote_assignments_can_be_requeued(store: Store) -> None:
    flow = store.create_flow("owner/repo", 9, installation_id=None)
    first = store.enqueue_job(flow.id, JobKind.INITIAL)
    assigned = store.assign_job("worker-1", "assignment-1")
    assert assigned is not None
    assert store.requeue_assignment(first.id, "worker-1", "assignment-1") is True
    requeued = store.get_job(first.id)
    assert requeued is not None
    assert requeued.state is JobState.QUEUED

    assigned = store.assign_job("worker-1", "assignment-2")
    assert assigned is not None
    assert store.start_assignment(first.id, "worker-1", "assignment-2") is True
    # Once the remote executor starts, replay may duplicate external effects; fail closed instead.
    assert store.requeue_assignment(first.id, "worker-1", "assignment-2") is False
    assert store.fail_assignment(
        first.id, "worker-1", "assignment-2", "worker disconnected"
    ) is True
    failed = store.get_job(first.id)
    assert failed is not None
    assert failed.state is JobState.FAILED
    assert failed.last_error == "worker disconnected"
    assert store.latest_failed_job(flow.id) == failed


def test_startup_requeues_unstarted_and_fails_started_assignments(store: Store) -> None:
    unstarted_flow = store.create_flow("owner/unstarted", 1, installation_id=None)
    unstarted_job = store.enqueue_job(unstarted_flow.id, JobKind.INITIAL)
    assert store.assign_job("worker-1", "unstarted-token") is not None

    started_flow = store.create_flow("owner/started", 2, installation_id=None)
    started_job = store.enqueue_job(started_flow.id, JobKind.INITIAL)
    assert store.assign_job("worker-1", "started-token") is not None
    assert store.start_assignment(
        started_job.id, "worker-1", "started-token"
    ) is True

    assert store.recover_abandoned_assignments() == (1, 1)

    recovered_unstarted = store.get_job(unstarted_job.id)
    recovered_started = store.get_job(started_job.id)
    recovered_flow = store.get_flow(started_flow.id)
    assert recovered_unstarted is not None
    assert recovered_unstarted.state is JobState.QUEUED
    assert recovered_started is not None
    assert recovered_started.state is JobState.FAILED
    assert recovered_flow is not None
    assert recovered_flow.state is FlowState.FAILED


def test_review_item_upsert_preserves_one_feedback_record(store: Store) -> None:
    flow = store.create_flow("owner/repo", 10, installation_id=None)
    attempt = store.create_attempt(flow.id)
    item = ReviewItem(
        id=None,
        attempt_id=attempt.id,
        kind=ReviewItemKind.INLINE,
        github_id=101,
        author="reviewer",
        body="Please add a bound.",
    )
    store.upsert_review_item(item)
    item.body = "Please add a strict bound."
    store.upsert_review_item(item)
    store.mark_resolved(attempt.id, item.github_id)

    pending = store.unrelayed_items(attempt.id)
    assert len(pending) == 1
    assert pending[0].body == "Please add a strict bound."
    assert pending[0].resolved is True

    assert pending[0].id is not None
    store.mark_relayed([pending[0].id])
    assert store.unrelayed_items(attempt.id) == []


def test_independent_stores_share_persisted_state(
    store: Store, postgresql_url: str
) -> None:
    flow = store.create_flow("owner/shared", 11, installation_id=22)
    other = Store(postgresql_url)

    try:
        assert other.get_flow(flow.id) == flow
    finally:
        other.close()


def test_concurrent_stores_do_not_claim_the_same_job(
    store: Store, postgresql_url: str
) -> None:
    flow = store.create_flow("owner/queue", 12, installation_id=None)
    queued = store.enqueue_job(flow.id, JobKind.INITIAL)
    other = Store(postgresql_url)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = list(
                executor.map(
                    lambda entry: entry[0].assign_job(entry[1], entry[2]),
                    (
                        (store, "worker-1", "assignment-1"),
                        (other, "worker-2", "assignment-2"),
                    ),
                )
            )
    finally:
        other.close()

    claimed_ids = [job.id for job in claimed if job is not None]
    assert claimed_ids == [queued.id]


def test_concurrent_stores_reserve_a_delivery_once(
    store: Store, postgresql_url: str
) -> None:
    other = Store(postgresql_url)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            recorded = list(
                executor.map(
                    lambda current: current.record_event(
                        "shared-delivery", "issues", None, {"action": "labeled"}
                    ),
                    (store, other),
                )
            )
    finally:
        other.close()

    assert sorted(recorded) == [False, True]
    assert store.event_seen("shared-delivery") is True
