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
    store.worker_connected("worker-1", protocol_version=1)

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

    assert store.start_assignment(claimed.id, attempt.id, "worker-1", "wrong") is False
    assert store.start_assignment(
        claimed.id, attempt.id, "worker-1", "assignment-1"
    ) is True
    assert store.get_attempt(attempt.id).worker_id == "worker-1"  # type: ignore[union-attr]
    assert store.heartbeat_assignment(claimed.id, "worker-1", "assignment-1") is True
    assert store.finish_assignment(
        claimed.id, "worker-1", "assignment-1", JobState.DONE
    ) is True
    finished = store.get_job(claimed.id)
    assert finished is not None
    assert finished.state is JobState.DONE


def test_only_unstarted_remote_assignments_can_be_requeued(store: Store) -> None:
    flow = store.create_flow("owner/repo", 9, installation_id=None)
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    store.worker_connected("worker-1", protocol_version=1)
    first = store.enqueue_job(flow.id, JobKind.INITIAL)
    assigned = store.assign_job("worker-1", "assignment-1")
    assert assigned is not None
    assert store.requeue_assignment(first.id, "worker-1", "assignment-1") is True
    requeued = store.get_job(first.id)
    assert requeued is not None
    assert requeued.state is JobState.QUEUED

    assigned = store.assign_job("worker-1", "assignment-2")
    assert assigned is not None
    assert store.start_assignment(
        first.id, attempt.id, "worker-1", "assignment-2"
    ) is True
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
    store.worker_connected("worker-1", protocol_version=1)
    store.worker_connected("worker-2", protocol_version=1)
    unstarted_flow = store.create_flow("owner/unstarted", 1, installation_id=None)
    unstarted_job = store.enqueue_job(unstarted_flow.id, JobKind.INITIAL)
    assert store.assign_job("worker-1", "unstarted-token") is not None

    started_flow = store.create_flow("owner/started", 2, installation_id=None)
    started_attempt = store.create_attempt(started_flow.id)
    started_flow.current_attempt_id = started_attempt.id
    store.update_flow(started_flow)
    started_job = store.enqueue_job(started_flow.id, JobKind.INITIAL)
    assert store.assign_job("worker-2", "started-token") is not None
    assert store.start_assignment(
        started_job.id, started_attempt.id, "worker-2", "started-token"
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
    store.worker_connected("worker-1", protocol_version=1)
    store.worker_connected("worker-2", protocol_version=1)

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


def test_workers_are_single_slot_and_attempt_affinity_is_enforced(store: Store) -> None:
    store.worker_connected("worker-a", protocol_version=1)
    store.worker_connected("worker-b", protocol_version=1)
    first_flow = store.create_flow("owner/first", 21, installation_id=None)
    second_flow = store.create_flow("owner/second", 22, installation_id=None)
    first_attempt = store.create_attempt(first_flow.id)
    first_flow.current_attempt_id = first_attempt.id
    store.update_flow(first_flow)
    first = store.enqueue_job(first_flow.id, JobKind.INITIAL)
    second = store.enqueue_job(second_flow.id, JobKind.INITIAL)

    first_claim = store.assign_job("worker-a", "token-a")
    assert first_claim is not None and first_claim.id == first.id
    assert store.assign_job("worker-a", "another-token") is None
    second_claim = store.assign_job("worker-b", "token-b")
    assert second_claim is not None and second_claim.id == second.id
    assert store.start_assignment(
        first.id, first_attempt.id, "worker-a", "token-a"
    ) is True
    assert store.finish_assignment(first.id, "worker-a", "token-a", JobState.DONE)

    revision = store.enqueue_job(first_flow.id, JobKind.REVISION)
    assert store.finish_assignment(second.id, "worker-b", "token-b", JobState.DONE)
    assert store.assign_job("worker-b", "wrong-worker") is None
    affinity_claim = store.assign_job("worker-a", "right-worker")
    assert affinity_claim is not None and affinity_claim.id == revision.id


def test_worker_presence_round_trip(store: Store) -> None:
    online = store.worker_connected("worker-a", protocol_version=1)

    assert online.status == "online"
    assert online.current_job_id is None
    store.worker_disconnected("worker-a", "connection closed")

    worker = store.list_workers()[0]
    assert worker.worker_id == "worker-a"
    assert worker.status == "offline"
    assert worker.last_error == "connection closed"


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
