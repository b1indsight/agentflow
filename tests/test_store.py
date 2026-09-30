from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.errors import UniqueViolation

from app.control.service import ControlService
from app.delivery_models import OpenedDelivery
from app.models import DeliveryState, FlowState, JobKind, JobState
from app.store import Store


@pytest.mark.parametrize("state,received,applied,blocked", [
    (JobState.QUEUED, False, False, False),
    (JobState.RUNNING, False, False, True),
    (JobState.DONE, False, False, False),
    (JobState.FAILED, True, False, True),
    (JobState.DONE, True, True, False),
])
def test_unsettled_jobs_distinguishes_executors_and_pending_results(
    store, state, received, applied, blocked,
):
    flow = store.create_flow("owner/repo", 17, installation_id=42)
    archive = store.enqueue_job(flow.id, JobKind.ARCHIVE)
    prior = store.enqueue_job(flow.id, JobKind.REVISION)
    other = store.create_flow("owner/repo", 18, installation_id=42)
    unrelated = store.enqueue_job(other.id, JobKind.INITIAL)
    now = store.now()
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET state=%s WHERE id=ANY(%s)",
                           (JobState.RUNNING, [archive.id, unrelated.id]))
        connection.execute(
            "UPDATE jobs SET state=%s,result_received_at=%s,result_applied_at=%s WHERE id=%s",
            (state, now if received else None, now if applied else None, prior.id),
        )
    # Cleanup must wait for every earlier executor/result, but not itself or another Flow.
    assert store.has_unsettled_jobs(flow.id, excluding_job_id=archive.id) is blocked


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
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    delivery = store.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    store.worker_connected("worker-1", protocol_version=1)

    job = store.enqueue_job(flow.id, JobKind.INITIAL, payload={"issue_number": 8})
    claimed = store.assign_job("worker-1", "assignment-1")

    assert store.get_current_attempt(flow) == attempt
    assert delivery.branch == f"flow/{flow.id}"
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
    assert store.get_flow_execution(flow.id).worker_id == "worker-1"  # type: ignore[union-attr]
    assert store.heartbeat_assignment(claimed.id, "worker-1", "assignment-1") is True
    assert store.finish_assignment(
        claimed.id, "worker-1", "assignment-1", JobState.DONE
    ) is True
    finished = store.get_job(claimed.id)
    assert finished is not None
    assert finished.state is JobState.DONE


def test_flow_binding_delivery_and_tool_ledger_round_trip(store: Store) -> None:
    store.worker_connected("worker-a", protocol_version=4, connection_epoch="epoch-a")
    flow = store.create_flow("owner/repo", 18, installation_id=42)
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    job = store.enqueue_job(flow.id, JobKind.INITIAL)

    claimed = store.assign_job("worker-a", "token-a", "epoch-a")
    assert claimed is not None and claimed.id == job.id
    execution = store.get_flow_execution(flow.id)
    assert execution is not None
    assert execution.worker_id == "worker-a"
    assert execution.repo_identity == "worker-a:owner/repo"

    delivery = store.get_or_create_delivery(flow.id, "flow/1")
    delivery.base_branch = "trunk"
    store.update_delivery(delivery)
    assert store.get_delivery(flow.id).state is DeliveryState.NONE  # type: ignore[union-attr]

    call, created = store.reserve_tool_call(
        call_id="call-a",
        flow_id=flow.id,
        attempt_id=attempt.id,
        job_id=job.id,
        name="flow.open_pr",
        arguments={"title": "Fix", "body": "Done"},
    )
    assert created is True
    assert call.response is None
    store.complete_tool_call("call-a", OpenedDelivery(pr_number=7, url="https://github.com/owner/repo/pull/17", head_sha="abc", delivery_version=1))
    replay, created = store.reserve_tool_call(
        call_id="call-a",
        flow_id=flow.id,
        attempt_id=attempt.id,
        job_id=job.id,
        name="flow.open_pr",
        arguments={"title": "Fix", "body": "Done"},
    )
    assert created is False
    assert replay.response == OpenedDelivery(pr_number=7, url="https://github.com/owner/repo/pull/17", head_sha="abc", delivery_version=1)


def test_pr_numbers_are_scoped_by_repository(store: Store) -> None:
    first = store.create_flow("owner/first", 1, installation_id=42)
    second = store.create_flow("owner/second", 1, installation_id=42)
    first_delivery = store.get_or_create_delivery(first.id, f"flow/{first.id}")
    second_delivery = store.get_or_create_delivery(second.id, f"flow/{second.id}")
    first_delivery.pr_number = 7
    second_delivery.pr_number = 7

    store.update_delivery(first_delivery)
    store.update_delivery(second_delivery)

    assert store.get_flow_by_pr("owner/first", 7) == first
    assert store.get_flow_by_pr("owner/second", 7) == second


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
    current = store.get_job(first.id)
    assert current is not None and current.state is JobState.RUNNING


def test_result_inbox_deduplicates_and_rejects_conflicting_payloads(store: Store) -> None:
    flow = store.create_flow("owner/results", 33, installation_id=None)
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    store.worker_connected("worker-1", protocol_version=1, connection_epoch="epoch-1")
    job = store.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.assign_job("worker-1", "token-1", "epoch-1") is not None
    assert store.start_assignment(
        job.id, attempt.id, "worker-1", "token-1", "epoch-1"
    )
    payload = {"type": "succeeded", "result": {"kind": "initial", "exit_code": 0}}

    assert store.record_assignment_result(
        job.id, "worker-1", "token-1", "epoch-1", payload, "digest-1"
    ) == "pending"
    assert store.record_assignment_result(
        job.id, "worker-1", "token-1", "epoch-1", payload, "digest-1"
    ) == "pending"
    assert store.record_assignment_result(
        job.id, "worker-1", "token-1", "epoch-1", {"different": True}, "digest-2"
    ) == "conflict"
    assert [pending.id for pending in store.list_unapplied_results()] == [job.id]
    assert store.complete_persisted_result(job.id, JobState.DONE)
    assert store.record_assignment_result(
        job.id, "worker-1", "token-1", "epoch-1", payload, "digest-1"
    ) == "applied"


def test_startup_preserves_assignments_for_lease_recovery(store: Store) -> None:
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

    assert store.recover_abandoned_assignments() == (0, 0)

    recovered_unstarted = store.get_job(unstarted_job.id)
    recovered_started = store.get_job(started_job.id)
    recovered_flow = store.get_flow(started_flow.id)
    assert recovered_unstarted is not None
    assert recovered_unstarted.state is JobState.RUNNING
    assert recovered_started is not None
    assert recovered_started.state is JobState.RUNNING
    assert recovered_started.recovery_deadline is not None
    assert recovered_flow is not None
    assert recovered_flow.state is FlowState.ACTIVE


def test_epoch_fencing_and_started_recovery_grace(postgresql_url: str) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)

    def clock() -> datetime:
        return now

    store = Store(
        postgresql_url,
        clock=clock,
        lease_duration=timedelta(seconds=30),
        recovery_grace=timedelta(seconds=90),
    )
    store.init_db()
    try:
        flow = store.create_flow("owner/recovery", 31, installation_id=None)
        attempt = store.create_attempt(flow.id)
        flow.current_attempt_id = attempt.id
        store.update_flow(flow)
        store.worker_connected("worker-1", protocol_version=1, connection_epoch="epoch-1")
        queued = store.enqueue_job(flow.id, JobKind.INITIAL)
        assigned = store.assign_job("worker-1", "token-1", "epoch-1")
        assert assigned is not None and assigned.id == queued.id
        assert assigned.lease_expires_at == now + timedelta(seconds=30)
        assert store.start_assignment(
            queued.id, attempt.id, "worker-1", "token-1", "epoch-1"
        )

        now += timedelta(seconds=10)
        store.worker_disconnected(
            "worker-1", "network lost", connection_epoch="epoch-1"
        )
        recovering = store.get_job(queued.id)
        assert recovering is not None
        assert recovering.recovery_deadline == now + timedelta(seconds=90)

        store.worker_connected("worker-1", protocol_version=1, connection_epoch="epoch-2")
        assert not store.heartbeat_assignment(
            queued.id, "worker-1", "token-1", "epoch-1"
        )
        assert store.resume_assignment(
            queued.id, "worker-1", "token-1", "epoch-2"
        )
        assert store.heartbeat_assignment(
            queued.id, "worker-1", "token-1", "epoch-2"
        )

        now += timedelta(seconds=91)
        store.worker_disconnected(
            "worker-1", "network lost again", connection_epoch="epoch-2"
        )
        now += timedelta(seconds=91)
        assert store.sweep_expired_assignments() == (0, 1)
        assert store.get_job(queued.id).state is JobState.FAILED  # type: ignore[union-attr]
        assert store.get_flow(flow.id).state is FlowState.FAILED  # type: ignore[union-attr]
    finally:
        store.close()


def test_idle_flow_binding_fails_after_same_worker_recovery_grace(
    postgresql_url: str,
) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    store = Store(
        postgresql_url,
        clock=lambda: now,
        recovery_grace=timedelta(seconds=30),
    )
    store.init_db()
    try:
        store.worker_connected("worker-a", protocol_version=4, connection_epoch="epoch-a")
        flow = store.create_flow("owner/idle", 51, installation_id=42)
        job = store.enqueue_job(flow.id, JobKind.INITIAL)
        assert store.assign_job("worker-a", "token-a", "epoch-a") is not None
        assert store.finish_assignment(
            job.id, "worker-a", "token-a", JobState.DONE, "epoch-a"
        )

        store.worker_disconnected("worker-a", "lost", "epoch-a")
        execution = store.get_flow_execution(flow.id)
        assert execution is not None
        assert execution.recovery_deadline == now + timedelta(seconds=30)

        now += timedelta(seconds=31)
        assert store.sweep_expired_assignments() == (0, 1)
        assert store.get_flow(flow.id).state is FlowState.FAILED  # type: ignore[union-attr]
        assert store.get_flow_execution(flow.id).released_at is not None  # type: ignore[union-attr]
    finally:
        store.close()


def test_unstarted_assignment_is_requeued_only_after_lease_expiry(
    postgresql_url: str,
) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)

    def clock() -> datetime:
        return now

    store = Store(
        postgresql_url,
        clock=clock,
        lease_duration=timedelta(seconds=30),
    )
    store.init_db()
    try:
        flow = store.create_flow("owner/unstarted-lease", 32, installation_id=None)
        job = store.enqueue_job(flow.id, JobKind.INITIAL)
        store.worker_connected("worker-1", protocol_version=1, connection_epoch="epoch-1")
        assert store.assign_job("worker-1", "token-1", "epoch-1") is not None
        store.worker_disconnected(
            "worker-1", "network lost", connection_epoch="epoch-1"
        )
        assert store.sweep_expired_assignments() == (0, 0)

        now += timedelta(seconds=31)
        assert store.sweep_expired_assignments() == (1, 0)
        assert store.get_job(job.id).state is JobState.QUEUED  # type: ignore[union-attr]
    finally:
        store.close()


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


def test_failed_flow_retry_reactivates_its_released_execution(store: Store) -> None:
    # A failed Flow keeps its execution history row; retry must reactivate that row instead of
    # inserting the same flow_id again and making the queued retry permanently unassignable.
    store.worker_connected("worker-a", protocol_version=1)
    flow = store.create_flow("owner/retry", 23, installation_id=None)
    failed = store.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.assign_job("worker-a", "token-a") is not None
    assert store.finish_assignment(failed.id, "worker-a", "token-a", JobState.FAILED)
    flow.state = FlowState.FAILED
    store.update_flow(flow)
    assert store.release_flow_execution(flow.id)

    ControlService(store).retry(flow)

    retried = store.assign_job("worker-a", "token-b")
    assert retried is not None
    assert retried.kind is JobKind.INITIAL
    execution = store.get_flow_execution(flow.id)
    assert execution is not None
    assert execution.worker_id == "worker-a"
    assert execution.released_at is None


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
