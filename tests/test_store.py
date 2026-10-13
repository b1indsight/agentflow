from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Event

import psycopg
import pytest
from psycopg.errors import UniqueViolation

from app.contracts.protocol import PROTOCOL_VERSION, InitialTaskResult, Succeeded, terminal_digest
from app.controler.workflow.service import ControlService
from app.domain.delivery_models import OpenedDelivery
from app.domain.models import DeliveryState, FlowState, JobKind, JobState
from app.persistence.store import Store


def test_persisted_timestamps_follow_the_injected_clock(postgresql_url):
    # Recovery simulations inject a clock; newly created work must share their timeline.
    now = datetime(2026, 9, 1, tzinfo=UTC)
    with closing(Store(postgresql_url, clock=lambda: now)) as store:
        store.init_db()
        flow = store.flows.create_flow("owner/clock", 1, None)
        attempt = store.flows.create_attempt(flow.id)
        job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
        assert flow.created_at == attempt.created_at == job.created_at == now

        now += timedelta(seconds=10)
        store.flows.update_flow(flow)
        store.flows.update_attempt(attempt)
        store.webhooks.record_event("clock-event", "issues", flow.id, {})
        with store.connection() as connection:
            event = connection.execute(
                "SELECT processed_at FROM events WHERE delivery_id='clock-event'"
            ).fetchone()
        assert flow.updated_at == attempt.updated_at == now
        assert event is not None and event["processed_at"] == now


@pytest.mark.parametrize("state,received,applied,blocked", [
    (JobState.QUEUED, False, False, False),
    (JobState.RUNNING, False, False, True),
    (JobState.DONE, True, True, False),
])
def test_unsettled_jobs_distinguishes_executors_and_pending_results(
    store, state, received, applied, blocked,
):
    flow = store.flows.create_flow("owner/repo", 17, installation_id=42)
    archive = store.jobs.enqueue_job(flow.id, JobKind.ARCHIVE)
    prior = store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    other = store.flows.create_flow("owner/repo", 18, installation_id=42)
    unrelated = store.jobs.enqueue_job(other.id, JobKind.INITIAL)
    now = store.now()
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET state=%s WHERE id=ANY(%s)",
                           (JobState.RUNNING, [archive.id, unrelated.id]))
        connection.execute(
            "UPDATE jobs SET state=%s,result_received_at=%s,result_applied_at=%s WHERE id=%s",
            (state, now if received else None, now if applied else None, prior.id),
        )
    # Cleanup must wait for every earlier executor/result, but not itself or another Flow.
    assert store.jobs.has_unsettled_jobs(flow.id, excluding_job_id=archive.id) is blocked


def test_only_one_active_flow_exists_per_issue(store: Store) -> None:
    flow = store.flows.create_flow("owner/repo", 7, installation_id=11)

    with pytest.raises(UniqueViolation):
        store.flows.create_flow("owner/repo", 7, installation_id=11)

    flow.state = FlowState.CANCELLED
    store.flows.update_flow(flow)
    replacement = store.flows.create_flow("owner/repo", 7, installation_id=11)

    assert replacement.id != flow.id
    assert store.flows.get_active_flow("owner/repo", 7) == replacement


def test_attempt_and_job_lifecycle_round_trip(store: Store) -> None:
    flow = store.flows.create_flow("owner/repo", 8, installation_id=12)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    store.workers.worker_connected("worker-1", protocol_version=PROTOCOL_VERSION)

    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL, payload={"issue_number": 8})
    claimed = store.workers.assign_job("worker-1", "assignment-1")

    assert store.flows.get_current_attempt(flow) == attempt
    assert delivery.branch == f"flow/{flow.id}"
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.state is JobState.RUNNING
    assert claimed.attempt == 1
    assert claimed.payload == {"issue_number": 8}
    assert claimed.assigned_worker_id == "worker-1"
    assert claimed.assignment_token == "assignment-1"

    assert store.workers.start_assignment(claimed.id, attempt.id, "worker-1", "wrong") is False
    assert store.workers.start_assignment(
        claimed.id, attempt.id, "worker-1", "assignment-1"
    ) is True
    assert store.workers.get_flow_execution(flow.id).worker_id == "worker-1"  # type: ignore[union-attr]
    assert store.workers.heartbeat_assignment(claimed.id, "worker-1", "assignment-1") is True
    assert store.workers.finish_assignment(
        claimed.id, "worker-1", "assignment-1", JobState.DONE
    ) is True
    finished = store.jobs.get_job(claimed.id)
    assert finished is not None
    assert finished.state is JobState.DONE


def test_flow_binding_delivery_and_tool_ledger_round_trip(store: Store) -> None:
    store.workers.worker_connected("worker-a", protocol_version=PROTOCOL_VERSION, connection_epoch="epoch-a")
    flow = store.flows.create_flow("owner/repo", 18, installation_id=42)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)

    claimed = store.workers.assign_job("worker-a", "token-a", "epoch-a")
    assert claimed is not None and claimed.id == job.id
    execution = store.workers.get_flow_execution(flow.id)
    assert execution is not None
    assert execution.worker_id == "worker-a"
    assert execution.repo_identity == "worker-a:owner/repo"

    delivery = store.deliveries.get_or_create_delivery(flow.id, "flow/1")
    delivery.base_branch = "trunk"
    store.deliveries.update_delivery(delivery)
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.NONE  # type: ignore[union-attr]

    call, created = store.deliveries.reserve_tool_call(
        call_id="call-a",
        flow_id=flow.id,
        attempt_id=attempt.id,
        job_id=job.id,
        name="flow.open_pr",
        arguments={"title": "Fix", "body": "Done"},
    )
    assert created is True
    assert call.response is None
    store.deliveries.complete_tool_call("call-a", OpenedDelivery(pr_number=7, url="https://github.com/owner/repo/pull/17", head_sha="abc", delivery_version=1))
    replay, created = store.deliveries.reserve_tool_call(
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
    first = store.flows.create_flow("owner/first", 1, installation_id=42)
    second = store.flows.create_flow("owner/second", 1, installation_id=42)
    first_delivery = store.deliveries.get_or_create_delivery(first.id, f"flow/{first.id}")
    second_delivery = store.deliveries.get_or_create_delivery(second.id, f"flow/{second.id}")
    first_delivery.pr_number = 7
    second_delivery.pr_number = 7

    store.deliveries.update_delivery(first_delivery)
    store.deliveries.update_delivery(second_delivery)

    assert store.flows.get_flow_by_pr("owner/first", 7) == first
    assert store.flows.get_flow_by_pr("owner/second", 7) == second


def test_only_unstarted_remote_assignments_can_be_requeued(store: Store) -> None:
    flow = store.flows.create_flow("owner/repo", 9, installation_id=None)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    store.workers.worker_connected("worker-1", protocol_version=PROTOCOL_VERSION)
    first = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assigned = store.workers.assign_job("worker-1", "assignment-1")
    assert assigned is not None
    assert store.workers.requeue_assignment(first.id, "worker-1", "assignment-1") is True
    requeued = store.jobs.get_job(first.id)
    assert requeued is not None
    assert requeued.state is JobState.QUEUED

    assigned = store.workers.assign_job("worker-1", "assignment-2")
    assert assigned is not None
    assert store.workers.start_assignment(
        first.id, attempt.id, "worker-1", "assignment-2"
    ) is True
    # Once the remote executor starts, replay may duplicate external effects; fail closed instead.
    assert store.workers.requeue_assignment(first.id, "worker-1", "assignment-2") is False
    current = store.jobs.get_job(first.id)
    assert current is not None and current.state is JobState.RUNNING


def test_result_inbox_deduplicates_and_rejects_conflicting_payloads(store: Store) -> None:
    flow = store.flows.create_flow("owner/results", 33, installation_id=None)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    store.workers.worker_connected("worker-1", protocol_version=PROTOCOL_VERSION, connection_epoch="epoch-1")
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("worker-1", "token-1", "epoch-1") is not None
    assert store.workers.start_assignment(
        job.id, attempt.id, "worker-1", "token-1", "epoch-1"
    )
    terminal = Succeeded(
        job_id=job.id, assignment_token="token-1", connection_epoch="epoch-1",
        result=InitialTaskResult(session_id=None, last_message="completed", exit_code=0),
    )
    payload = terminal.model_dump(mode="json")
    digest = terminal_digest(terminal)

    assert store.workers.record_assignment_result(
        job.id, "worker-1", "token-1", "epoch-1", payload, digest
    ) == "pending"
    assert store.workers.record_assignment_result(
        job.id, "worker-1", "token-1", "epoch-1", payload, digest
    ) == "pending"
    assert [pending.id for pending in store.jobs.list_unapplied_results()] == [job.id]
    assert store.jobs.complete_persisted_result(job.id, JobState.DONE)
    assert store.workers.record_assignment_result(
        job.id, "worker-1", "token-1", "epoch-1", payload, digest
    ) == "applied"


def test_startup_preserves_assignments_for_lease_recovery(store: Store) -> None:
    store.workers.worker_connected("worker-1", protocol_version=PROTOCOL_VERSION)
    store.workers.worker_connected("worker-2", protocol_version=PROTOCOL_VERSION)
    unstarted_flow = store.flows.create_flow("owner/unstarted", 1, installation_id=None)
    unstarted_job = store.jobs.enqueue_job(unstarted_flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("worker-1", "unstarted-token") is not None

    started_flow = store.flows.create_flow("owner/started", 2, installation_id=None)
    started_attempt = store.flows.create_attempt(started_flow.id)
    started_flow.current_attempt_id = started_attempt.id
    store.flows.update_flow(started_flow)
    started_job = store.jobs.enqueue_job(started_flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("worker-2", "started-token") is not None
    assert store.workers.start_assignment(
        started_job.id, started_attempt.id, "worker-2", "started-token"
    ) is True

    store.workers.recover_abandoned_assignments()

    recovered_unstarted = store.jobs.get_job(unstarted_job.id)
    recovered_started = store.jobs.get_job(started_job.id)
    recovered_flow = store.flows.get_flow(started_flow.id)
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
        flow = store.flows.create_flow("owner/recovery", 31, installation_id=None)
        attempt = store.flows.create_attempt(flow.id)
        flow.current_attempt_id = attempt.id
        store.flows.update_flow(flow)
        store.workers.worker_connected("worker-1", protocol_version=PROTOCOL_VERSION, connection_epoch="epoch-1")
        queued = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
        assigned = store.workers.assign_job("worker-1", "token-1", "epoch-1")
        assert assigned is not None and assigned.id == queued.id
        assert assigned.lease_expires_at == now + timedelta(seconds=30)
        assert store.workers.start_assignment(
            queued.id, attempt.id, "worker-1", "token-1", "epoch-1"
        )

        now += timedelta(seconds=10)
        store.workers.worker_disconnected(
            "worker-1", "network lost", connection_epoch="epoch-1"
        )
        recovering = store.jobs.get_job(queued.id)
        assert recovering is not None
        assert recovering.recovery_deadline == now + timedelta(seconds=90)

        store.workers.worker_connected("worker-1", protocol_version=PROTOCOL_VERSION, connection_epoch="epoch-2")
        assert not store.workers.heartbeat_assignment(
            queued.id, "worker-1", "token-1", "epoch-1"
        )
        assert store.workers.resume_assignment(
            queued.id, "worker-1", "token-1", "epoch-2"
        )
        assert store.workers.heartbeat_assignment(
            queued.id, "worker-1", "token-1", "epoch-2"
        )

        now += timedelta(seconds=91)
        store.workers.worker_disconnected(
            "worker-1", "network lost again", connection_epoch="epoch-2"
        )
        now += timedelta(seconds=91)
        assert store.workers.sweep_expired_assignments() == (0, 1)
        assert store.jobs.get_job(queued.id).state is JobState.FAILED  # type: ignore[union-attr]
        assert store.flows.get_flow(flow.id).state is FlowState.FAILED  # type: ignore[union-attr]
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
        store.workers.worker_connected("worker-a", protocol_version=PROTOCOL_VERSION, connection_epoch="epoch-a")
        flow = store.flows.create_flow("owner/idle", 51, installation_id=42)
        job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
        assert store.workers.assign_job("worker-a", "token-a", "epoch-a") is not None
        assert store.workers.finish_assignment(
            job.id, "worker-a", "token-a", JobState.DONE, "epoch-a"
        )

        store.workers.worker_disconnected("worker-a", "lost", "epoch-a")
        execution = store.workers.get_flow_execution(flow.id)
        assert execution is not None
        assert execution.recovery_deadline == now + timedelta(seconds=30)

        now += timedelta(seconds=31)
        assert store.workers.sweep_expired_assignments() == (0, 1)
        assert store.flows.get_flow(flow.id).state is FlowState.FAILED  # type: ignore[union-attr]
        assert store.workers.get_flow_execution(flow.id).released_at is not None  # type: ignore[union-attr]
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
        flow = store.flows.create_flow("owner/unstarted-lease", 32, installation_id=None)
        job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
        store.workers.worker_connected("worker-1", protocol_version=PROTOCOL_VERSION, connection_epoch="epoch-1")
        assert store.workers.assign_job("worker-1", "token-1", "epoch-1") is not None
        store.workers.worker_disconnected(
            "worker-1", "network lost", connection_epoch="epoch-1"
        )
        assert store.workers.sweep_expired_assignments() == (0, 0)

        now += timedelta(seconds=31)
        assert store.workers.sweep_expired_assignments() == (1, 0)
        assert store.jobs.get_job(job.id).state is JobState.QUEUED  # type: ignore[union-attr]
    finally:
        store.close()


@pytest.mark.parametrize("contender_id", ["worker-2", "worker-1"])
def test_concurrent_stores_do_not_claim_the_same_job(
    store: Store, postgresql_url: str, monkeypatch, contender_id
) -> None:
    # Two idle workers can poll together. Hold worker-2 after its fair selection
    # of worker-1; that selection must not lock worker-1 out of claiming the job.
    # A second request for worker-1 must also recheck its slot after the first wins.
    flow = store.flows.create_flow("owner/queue", 12, installation_id=None)
    queued = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    following = store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    other = Store(postgresql_url)
    store.workers.worker_connected("worker-1", protocol_version=PROTOCOL_VERSION)
    store.workers.worker_connected("worker-2", protocol_version=PROTOCOL_VERSION)

    selected, release = Event(), Event()
    execute = psycopg.Connection.execute
    try:
        with other.transaction() as contender:
            def observed(connection, query, *args, **kwargs):
                cursor = execute(connection, query, *args, **kwargs)
                if (connection is contender._db._bound_connection
                        and query.startswith("SELECT w.worker_id FROM workers w")):
                    selected.set()
                    assert release.wait(5)
                return cursor

            monkeypatch.setattr(psycopg.Connection, "execute", observed)
            with ThreadPoolExecutor(max_workers=2) as executor:
                second = executor.submit(contender.workers.assign_job, contender_id, "assignment-2")
                try:
                    assert selected.wait(5)
                    first = executor.submit(store.workers.assign_job, "worker-1", "assignment-1")
                    winner = first.result(timeout=5)
                finally:
                    release.set()
                loser = second.result(timeout=5)
    finally:
        other.close()

    assert winner is not None and winner.id == queued.id
    assert loser is None
    persisted = store.jobs.get_job(queued.id)
    pending = store.jobs.get_job(following.id)
    assert persisted is not None and persisted.assigned_worker_id == "worker-1"
    assert persisted.attempt == 1
    assert pending is not None and pending.state is JobState.QUEUED


def test_assignment_rechecks_worker_after_fair_selection(store, postgresql_url, monkeypatch):
    # As in the concurrent claim test above, pause at the fair SELECT boundary.
    # A reconnect can commit before the caller locks itself.
    flow = store.flows.create_flow("owner/changed-worker", 13, installation_id=None)
    queued = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    store.workers.worker_connected("worker-1", PROTOCOL_VERSION, "epoch")
    other = Store(postgresql_url)
    execute = psycopg.Connection.execute

    def observed(connection, query, *args, **kwargs):
        cursor = execute(connection, query, *args, **kwargs)
        if query.startswith("SELECT w.worker_id FROM workers w"):
            with other.transaction() as tx:
                with tx.connection() as conn:
                    conn.execute("SET LOCAL statement_timeout = '2s'")
                tx.workers.worker_connected("worker-1", PROTOCOL_VERSION, "new-epoch")
        return cursor

    monkeypatch.setattr(psycopg.Connection, "execute", observed)
    try:
        assert store.workers.assign_job("worker-1", "token", "epoch") is None
    finally:
        other.close()
    assert store.jobs.get_job(queued.id).state is JobState.QUEUED
    assert store.workers.get_flow_execution(flow.id) is None


def test_workers_are_single_slot_and_attempt_affinity_is_enforced(store: Store) -> None:
    store.workers.worker_connected("worker-a", protocol_version=PROTOCOL_VERSION)
    store.workers.worker_connected("worker-b", protocol_version=PROTOCOL_VERSION)
    first_flow = store.flows.create_flow("owner/first", 21, installation_id=None)
    second_flow = store.flows.create_flow("owner/second", 22, installation_id=None)
    first_attempt = store.flows.create_attempt(first_flow.id)
    first_flow.current_attempt_id = first_attempt.id
    store.flows.update_flow(first_flow)
    first = store.jobs.enqueue_job(first_flow.id, JobKind.INITIAL)
    second = store.jobs.enqueue_job(second_flow.id, JobKind.INITIAL)

    first_claim = store.workers.assign_job("worker-a", "token-a")
    assert first_claim is not None and first_claim.id == first.id
    assert store.workers.assign_job("worker-a", "another-token") is None
    second_claim = store.workers.assign_job("worker-b", "token-b")
    assert second_claim is not None and second_claim.id == second.id
    assert store.workers.start_assignment(
        first.id, first_attempt.id, "worker-a", "token-a"
    ) is True
    assert store.workers.finish_assignment(first.id, "worker-a", "token-a", JobState.DONE)

    revision = store.jobs.enqueue_job(first_flow.id, JobKind.REVISION)
    assert store.workers.finish_assignment(second.id, "worker-b", "token-b", JobState.DONE)
    assert store.workers.assign_job("worker-b", "wrong-worker") is None
    affinity_claim = store.workers.assign_job("worker-a", "right-worker")
    assert affinity_claim is not None and affinity_claim.id == revision.id


def test_failed_flow_retry_reactivates_its_released_execution(store: Store) -> None:
    # A failed Flow keeps its execution history row; retry must reactivate that row instead of
    # inserting the same flow_id again and making the queued retry permanently unassignable.
    store.workers.worker_connected("worker-a", protocol_version=PROTOCOL_VERSION)
    flow = store.flows.create_flow("owner/retry", 23, installation_id=None)
    failed = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("worker-a", "token-a") is not None
    assert store.workers.finish_assignment(failed.id, "worker-a", "token-a", JobState.FAILED)
    flow.state = FlowState.FAILED
    store.flows.update_flow(flow)
    assert store.workers.release_flow_execution(flow.id)

    ControlService(store).retry(flow)

    retried = store.workers.assign_job("worker-a", "token-b")
    assert retried is not None
    assert retried.kind is JobKind.INITIAL
    execution = store.workers.get_flow_execution(flow.id)
    assert execution is not None
    assert execution.worker_id == "worker-a"
    assert execution.released_at is None


def test_worker_presence_round_trip(store: Store) -> None:
    online = store.workers.worker_connected("worker-a", protocol_version=PROTOCOL_VERSION)

    assert online.status == "online"
    assert online.current_job_id is None
    store.workers.worker_disconnected("worker-a", "connection closed")

    worker = store.workers.list_workers()[0]
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
                    lambda current: current.webhooks.record_event(
                        "shared-delivery", "issues", None, {"action": "labeled"}
                    ),
                    (store, other),
                )
            )
    finally:
        other.close()

    assert sorted(recorded) == [False, True]
    assert store.webhooks.event_seen("shared-delivery") is True
