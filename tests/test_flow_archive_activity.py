"""Archive history and automatic correction never authorize another execution."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg.errors import CheckViolation, LockNotAvailable

from app.controler.events.router import EventRouter
from app.controler.workflow.service import ControlService
from app.domain.models import ArchiveReason, DeliveryState, FlowState, JobKind
from app.github import PullRequestMerge
from app.github.console import Observation
from app.persistence.activity import ActivityStore
from app.persistence.store import Store
from tests.workspace_fixtures import register_worker

NOW = datetime(2026, 11, 9, 12, 0, 5, 500000, tzinfo=UTC)
MERGE = PullRequestMerge(
    number=17, merged=True, merged_at=NOW - timedelta(seconds=3), merge_commit_sha="a" * 40
)


@pytest.fixture
def store(postgresql_url):
    instance = Store(postgresql_url, clock=lambda: NOW)
    instance.init_db()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def archived(store):
    service = ControlService(database=store.database)
    flow = service.on_trigger("owner/repo", 7, 42)
    delivery = store.deliveries.get_or_create_delivery(flow.id, flow.initial_branch)
    delivery.pr_number, delivery.state = MERGE.number, DeliveryState.OPEN
    store.deliveries.update_delivery(delivery)
    service.cancel(flow)
    return store.flows.get_flow(flow.id)


def merge_payload(flow, merge=MERGE):
    return {
        "action": "closed",
        "repository": {"full_name": flow.repo},
        "installation": {"id": flow.installation_id},
        "pull_request": merge.model_dump(mode="json"),
    }


def authority(store, flow_id):
    with store.connection() as connection:
        return {
            "jobs": store.jobs.list_jobs(flow_id),
            "attempts": store.flows.list_attempts(flow_id),
            "execution": store.workers.get_flow_execution(flow_id),
            "owners": connection.execute("SELECT * FROM issue_flow_owners").fetchall(),
        }


def test_delayed_merge_automatically_corrects_without_resuming(store, archived, monkeypatch):
    # GitHub merged before local archive, but its delivery arrives after abandonment.
    activity = ActivityStore(store.database)
    original = activity.archived(archived.id, 1)
    before = authority(store, archived.id)
    delivery = store.deliveries.get_delivery(archived.id)
    service = ControlService(database=store.database)
    router = EventRouter(service, SimpleNamespace(), database=store.database)
    router.dispatch("pull_request", "delayed", merge_payload(archived))
    assert store.webhooks.event_seen("delayed")
    current = store.flows.get_flow(archived.id)
    assert current == archived.model_copy(update={"archive_reason": ArchiveReason.SUCCEEDED})
    assert authority(store, archived.id) == before
    assert store.deliveries.get_delivery(archived.id) == delivery
    assert activity.archived(archived.id, 1) == original
    assert original.metadata["archive_reason"] == "abandoned"
    assert original.occurred_at == archived.archived_at
    events = activity.history(archived.id, 1)
    merges = [e for e in events if e.kind == "merge"]
    assert len(merges) == 1
    assert merges[0].metadata["merge_commit_sha"] == MERGE.merge_commit_sha
    corrections = [e for e in events if e.kind == "archive_correction"]
    assert len(corrections) == 1
    assert corrections[0].metadata["before"] == "abandoned"
    assert corrections[0].metadata["after"] == "succeeded"
    # A later webhook or poll must reuse the fact, not compare origin/arrival time.
    monkeypatch.setattr(store.database, "_clock", lambda: NOW + timedelta(days=1))
    router.dispatch("pull_request", "delayed", merge_payload(archived))
    router.dispatch("pull_request", "duplicate-fact", merge_payload(archived))
    assert not store.flows.observe_archived_merge(archived.id, 1, MERGE, origin="reconciliation")
    assert activity.history(archived.id, 1) == events
    assert authority(store, archived.id) == before
    assert store.deliveries.get_delivery(archived.id) == delivery


def test_delayed_old_round_merge_does_not_change_resumed_flow(store, archived):
    # A delayed webhook still names the previous PR after a new delivery starts.
    service = ControlService(database=store.database)
    activity = ActivityStore(store.database)
    original = activity.archived(archived.id, 1)
    assert original is not None and original.metadata["archive_reason"] == "abandoned"
    resumed = service.resume(archived, expected_round_no=1)
    before = authority(store, archived.id)
    router = EventRouter(service, SimpleNamespace(), database=store.database)
    payload = merge_payload(resumed)
    payload["installation"]["id"] = 99
    router.dispatch("pull_request", "late-merge", payload)
    assert not store.webhooks.event_seen("late-merge")
    payload["installation"]["id"] = 42
    router.dispatch("pull_request", "late-merge", payload)
    router.dispatch("pull_request", "late-merge", payload)
    assert store.flows.get_flow(resumed.id) == resumed
    assert authority(store, resumed.id) == before
    assert activity.archived(resumed.id, 1) == original
    assert activity.archived_pr(resumed.repo, MERGE.number) == (resumed.id, 1)
    history = activity.history(resumed.id, 1)
    merges = [item for item in history if item.kind == "merge"]
    assert len(merges) == 1
    assert merges[0].metadata["merge_commit_sha"] == MERGE.merge_commit_sha
    assert len([item for item in history if item.kind == "archive_correction"]) == 1
    assert activity.archived(resumed.id, 2) is None
    assert not any(e.kind == "archive_correction" for e in activity.history(resumed.id, 2))
    assert [job.kind for job in store.jobs.list_jobs(resumed.id)] == [JobKind.INITIAL, JobKind.INITIAL]
    assert store.deliveries.get_delivery(resumed.id).pr_number is None


@pytest.mark.parametrize("seconds_before", [2, 1.000001, 1, 0.999999, 0, -1])
def test_actual_merge_time_controls_correction_not_arrival(store, archived, seconds_before):
    merge = MERGE.model_copy(update={"merged_at": NOW - timedelta(seconds=seconds_before)})
    ControlService(database=store.database).on_merged(
        archived, "merge-time", merge_payload(archived, merge)
    )
    current = store.flows.get_flow(archived.id)
    assert current.state is FlowState.ARCHIVED
    expected = ArchiveReason.SUCCEEDED if seconds_before > 1 else ArchiveReason.ABANDONED
    assert current.archive_reason is expected
    assert current.archived_at == NOW


def test_conflicting_merge_is_rejected_without_changing_history(store, archived):
    service = ControlService(database=store.database)
    service.on_merged(archived, "first", merge_payload(archived))
    activity = ActivityStore(store.database)
    before = activity.history(archived.id, 1)
    conflict = MERGE.model_copy(update={"merge_commit_sha": "b" * 40})
    with pytest.raises(ValueError, match="conflict"):
        service.on_merged(archived, "conflict", merge_payload(archived, conflict))
    assert activity.history(archived.id, 1) == before


def test_correction_activity_failure_rolls_back_merge_and_outcome(store, archived, monkeypatch):
    original_append = ActivityStore.append_flow

    def fail_correction(self, flow_id, round_no, kind, **kwargs):
        if kind == "archive_correction":
            raise RuntimeError("audit unavailable")
        return original_append(self, flow_id, round_no, kind, **kwargs)

    activity = ActivityStore(store.database)
    before = activity.history(archived.id, 1)
    with monkeypatch.context() as patch:
        patch.setattr(ActivityStore, "append_flow", fail_correction)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            ControlService(database=store.database).on_merged(
                archived, "retryable", merge_payload(archived)
            )
    assert store.flows.get_flow(archived.id) == archived
    assert activity.history(archived.id, 1) == before
    ControlService(database=store.database).on_merged(
        archived, "retryable", merge_payload(archived)
    )
    assert store.flows.get_flow(archived.id).archive_reason is ArchiveReason.SUCCEEDED


def test_unsettled_archive_cannot_be_written_through_general_update(store):
    flow = ControlService(database=store.database).on_trigger("owner/repo", 8, 42)
    flow.state, flow.archive_reason, flow.archived_at = (
        FlowState.ARCHIVED,
        ArchiveReason.ABANDONED,
        NOW,
    )
    with pytest.raises(ValueError, match="archive"):
        store.flows.update_flow(flow)
    assert store.flows.get_flow(flow.id).state is FlowState.QUEUED
    assert ActivityStore(store.database).archived(flow.id, 1) is None


def test_archive_activity_failure_rolls_back_release_and_cancellation(store, monkeypatch):
    service = ControlService(database=store.database)
    flow = service.on_trigger("owner/repo", 8, 42)
    register_worker(store, "worker", repo=flow.repo)
    store.workers.assign_job("worker", "undispatched")
    before = authority(store, flow.id)
    original_flow = store.flows.get_flow(flow.id)
    original_append = ActivityStore.append_flow

    def fail_archive(self, flow_id, round_no, kind, **kwargs):
        if kind == "archive":
            raise RuntimeError("history unavailable")
        return original_append(self, flow_id, round_no, kind, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ActivityStore, "append_flow", fail_archive)
        with pytest.raises(RuntimeError, match="history unavailable"):
            service.cancel(flow)
    assert authority(store, flow.id) == before
    assert store.flows.get_flow(flow.id) == original_flow
    assert ActivityStore(store.database).archived(flow.id, 1) is None
    service.cancel(flow)
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.workers.get_flow_execution(flow.id).released_at is not None


@pytest.mark.parametrize("first", ["correction", "resume"])
def test_resume_and_automatic_correction_share_the_workflow_fence(store, archived, first):
    def correct(database):
        ControlService(database=database).on_merged(archived, "concurrent", merge_payload(archived))

    def resume(database):
        ControlService(database=database).resume(archived, expected_round_no=1)

    before, after = (correct, resume) if first == "correction" else (resume, correct)
    with store.transaction() as tx:
        before(tx.database)
        with pytest.raises(LockNotAvailable), store.transaction() as other:
            with other.connection() as connection:
                connection.execute("SET LOCAL lock_timeout='50ms'")
            after(other.database)
    after(store.database)
    current = store.flows.get_flow(archived.id)
    assert current.state is FlowState.QUEUED and current.current_round_no == 2
    assert current.archive_reason is None and current.archived_at is None
    assert (
        len(
            [
                item
                for item in ActivityStore(store.database).history(archived.id, 1)
                if item.kind == "archive_correction"
            ]
        )
        == 1
    )


def test_saved_merge_evidence_can_be_corrected_despite_unrelated_comments(store, archived):
    activity = ActivityStore(store.database)
    activity.append_flow(
        archived.id,
        1,
        "merge",
        occurred_at=MERGE.merged_at,
        metadata={
            "repo": archived.repo,
            "pr_number": MERGE.number,
            "merged_at": MERGE.merged_at.isoformat(),
            "merge_commit_sha": MERGE.merge_commit_sha,
            "origin": "reconciliation",
        },
    )
    activity.append(
        archived.id,
        Observation(
            key="github:comment:123",
            source="issue.comments",
            kind="comment",
            author="user",
            occurred_at=NOW,
            updated_at=NOW,
            body="Another unrelated observation",
        ),
    )
    ControlService(database=store.database).on_merged(
        archived, "already-observed", merge_payload(archived)
    )
    assert store.flows.get_flow(archived.id).archive_reason is ArchiveReason.SUCCEEDED
    events = activity.history(archived.id, 1)
    assert len([e for e in events if e.kind == "merge"]) == 1
    assert len([e for e in events if e.kind == "archive_correction"]) == 1


def test_workflow_evidence_cannot_be_overwritten_or_forged_by_observation(store, archived):
    activity = ActivityStore(store.database)
    original = activity.archived(archived.id, 1)
    with pytest.raises(ValueError, match="internal activity writer"):
        activity.append(archived.id, original)
    with pytest.raises(CheckViolation, match="immutable"), store.connection() as connection:
        connection.execute(
            "DELETE FROM console_activity WHERE flow_id=%s AND kind='archive'", (archived.id,)
        )
    assert activity.archived(archived.id, 1) == original
