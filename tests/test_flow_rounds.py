"""Historical corrections use real PostgreSQL locks, audit triggers and transactions."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.errors import CheckViolation, LockNotAvailable

from app.contracts.protocol import PROTOCOL_VERSION
from app.controler.workflow.service import ControlService
from app.domain.models import ArchiveReason, DeliveryState, Flow, FlowRound, FlowState, JobState
from app.github import PullRequestMerge
from app.github.console import Observation
from app.persistence.activity import ActivityStore
from app.persistence.console import ConsoleStore
from app.persistence.rounds import RoundStore
from app.persistence.rounds_schema import ROUND_AUDIT_SCHEMA
from app.persistence.store import Store

NOW = datetime(2026, 11, 9, 12, 0, 5, 500000, tzinfo=UTC)
MERGED_AT = datetime(2026, 11, 9, 12, 0, 2, tzinfo=UTC)
MERGE = PullRequestMerge(number=17, merged=True, merged_at=MERGED_AT, merge_commit_sha="a" * 40)
CORRECTION = {
    "expected_version": 0,
    "actor": "maintainer",
    "reason": "Verified delayed merge webhook against the archived PR",
    "confirmed": True,
}


@pytest.fixture
def store(postgresql_url: str) -> Iterator[Store]:
    instance = Store(postgresql_url, clock=lambda: NOW)
    try:
        instance.init_db()
        yield instance
    finally:
        instance.close()


@pytest.fixture
def archived(store: Store) -> Flow:
    service = ControlService(database=store.database)
    flow = service.on_trigger("owner/repo", 7, 42)
    assert flow is not None
    store.workers.worker_connected("worker", PROTOCOL_VERSION)
    assert store.workers.assign_job("worker", "first-assignment") is not None
    delivery = store.deliveries.get_or_create_delivery(flow.id, "flow/7")
    delivery.pr_number, delivery.state = MERGE.number, DeliveryState.OPEN
    store.deliveries.update_delivery(delivery)
    # An undispatched assignment can be cancelled without pretending a stop was received.
    service.cancel(flow)
    result = store.flows.get_flow(flow.id)
    assert result is not None and result.state is FlowState.ARCHIVED
    return result


def snapshot(store: Store, flow_id: int) -> dict:
    """Capture authority and publication rows so refusals prove absence of side effects."""
    with store.connection() as connection:
        return {
            "flow": store.flows.get_flow(flow_id),
            "delivery": store.deliveries.get_delivery(flow_id),
            "execution": store.workers.get_flow_execution(flow_id),
            "workers": store.workers.list_workers(),
            "jobs": store.jobs.list_jobs(flow_id),
            "attempts": store.flows.list_attempts(flow_id),
            "owners": connection.execute(
                "SELECT * FROM issue_flow_owners ORDER BY repo,issue_number"
            ).fetchall(),
            "stops": connection.execute(
                "SELECT * FROM execution_stops WHERE flow_id=%s ORDER BY request_id", (flow_id,)
            ).fetchall(),
            "rounds": connection.execute(
                "SELECT * FROM flow_rounds WHERE flow_id=%s ORDER BY round_no", (flow_id,)
            ).fetchall(),
            "corrections": connection.execute(
                "SELECT * FROM flow_round_corrections WHERE flow_id=%s ORDER BY round_no,version",
                (flow_id,),
            ).fetchall(),
            "console": connection.execute(
                "SELECT * FROM console_flows WHERE flow_id=%s", (flow_id,)
            ).fetchone(),
            "activity": connection.execute(
                "SELECT * FROM console_activity WHERE flow_id=%s ORDER BY version", (flow_id,)
            ).fetchall(),
        }


def authority(state: dict) -> dict:
    return {
        key: value
        for key, value in state.items()
        if key not in {"rounds", "corrections", "console", "activity"}
    }


def test_late_merge_only_records_evidence_and_rejects_conflicting_observations(store, archived):
    rounds = RoundStore(store.database)
    original = rounds.by_pr(archived.repo, MERGE.number)
    assert original is not None and original.execution.released_at == archived.archived_at
    assert rounds.by_pr("other/repo", MERGE.number) is None
    assert rounds.by_pr(archived.repo, 18) is None
    before = snapshot(store, archived.id)
    assert rounds.observe_merge(archived.id, 1, MERGE)
    saved = rounds.by_pr(archived.repo, MERGE.number)
    assert saved == original.model_copy(
        update={
            "merged_at": MERGE.merged_at,
            "merge_commit_sha": MERGE.merge_commit_sha,
        }
    )
    after = snapshot(store, archived.id)
    assert authority(after) == authority(before)
    assert after["corrections"] == []
    assert after["console"]["facts"] == before["console"]["facts"]
    assert after["console"]["state_version"] == before["console"]["state_version"]
    assert after["console"]["activity_version"] == before["console"]["activity_version"] + 1
    assert not rounds.observe_merge(archived.id, 1, MERGE)
    # Older and newer arrivals cannot replace immutable merge identity/time.
    for changes in (
        {"merged_at": MERGED_AT - timedelta(seconds=2)},
        {"merged_at": MERGED_AT + timedelta(seconds=2)},
        {"merge_commit_sha": "b" * 40},
        {"number": 18},
    ):
        with pytest.raises(ValueError, match="conflicts|does not belong"):
            rounds.observe_merge(archived.id, 1, MERGE.model_copy(update=changes))
    assert snapshot(store, archived.id) == after


def test_current_archived_round_correction_preserves_history_and_audits_every_field(
    store, archived
):
    rounds = RoundStore(store.database)
    rounds.observe_merge(archived.id, 1, MERGE)
    before = snapshot(store, archived.id)
    original = rounds.by_pr(archived.repo, MERGE.number)
    corrected = rounds.correct_archive(archived.id, 1, **CORRECTION)
    assert corrected == original.model_copy(
        update={
            "archive_reason": ArchiveReason.SUCCEEDED,
            "version": 1,
        }
    )
    assert corrected.original_reason is ArchiveReason.ABANDONED
    after = snapshot(store, archived.id)
    expected_authority = authority(before)
    expected_authority["flow"] = archived.model_copy(
        update={"archive_reason": ArchiveReason.SUCCEEDED}
    )
    assert authority(after) == expected_authority
    assert len(after["corrections"]) == 1
    audit = after["corrections"][0]
    assert audit == {
        "flow_id": archived.id,
        "round_no": 1,
        "version": 1,
        "actor": CORRECTION["actor"],
        "reason": CORRECTION["reason"],
        "corrected_at": NOW,
        "evidence": {
            "repo": archived.repo,
            "pr_number": MERGE.number,
            "merged_at": original.merged_at.isoformat(),
            "merge_commit_sha": MERGE.merge_commit_sha,
            "merge_time_precision_seconds": 1,
            "archived_at": archived.archived_at.isoformat(),
            "original_reason": "abandoned",
            "before": {"archive_reason": "abandoned", "version": 0},
            "after": {"archive_reason": "succeeded", "version": 1},
        },
    }
    publication = next(item for item in reversed(after["activity"]) if item["kind"] == "round")
    event = json.loads(publication["body"])
    assert publication["source"] == "flow" and publication["author"] == CORRECTION["actor"]
    assert FlowRound.model_validate(event["before"]) == original
    assert FlowRound.model_validate(event["after"]) == corrected
    event["correction"]["corrected_at"] = datetime.fromisoformat(
        event["correction"]["corrected_at"]
    )
    assert event["correction"] == audit
    assert after["console"]["facts"] == {
        **before["console"]["facts"],
        "archive_reason": "succeeded",
    }
    assert after["console"]["state_version"] == before["console"]["state_version"] + 1
    assert after["console"]["activity_version"] == before["console"]["activity_version"] + 2
    assert "成功" in ConsoleStore(store.database).detail(archived.id).reason
    # Neither a replay with the old version nor a new request for the corrected round applies twice.
    for version, error in ((0, "version conflict"), (1, "only an abandoned")):
        with pytest.raises(ValueError, match=error):
            rounds.correct_archive(archived.id, 1, **{**CORRECTION, "expected_version": version})
    assert snapshot(store, archived.id) == after


@pytest.mark.parametrize("new_flow", [False, True], ids=["new-round", "new-issue-owner"])
def test_old_round_correction_cannot_change_current_running_work(store, archived, new_flow):
    service = ControlService(database=store.database)
    if new_flow:
        current = service.on_trigger(archived.repo, archived.issue_number, archived.installation_id)
    else:
        current = service.resume(archived, expected_round_no=1)
    assert current is not None
    job = store.workers.assign_job("worker", "next-assignment")
    assert job is not None and job.state is JobState.RUNNING and job.flow_id == current.id
    delivery = store.deliveries.get_or_create_delivery(current.id, f"flow/{current.id}/round-2")
    delivery.pr_number, delivery.state = 18, DeliveryState.OPEN
    store.deliveries.update_delivery(delivery)
    current = store.flows.get_flow(current.id)
    assert current.state is FlowState.RUNNING
    before = snapshot(store, current.id)
    rounds = RoundStore(store.database)
    assert rounds.by_pr(archived.repo, MERGE.number).round_no == 1
    assert rounds.observe_merge(archived.id, 1, MERGE)
    original = rounds.by_pr(archived.repo, MERGE.number)
    corrected = rounds.correct_archive(archived.id, 1, **CORRECTION)
    assert corrected == original.model_copy(
        update={
            "archive_reason": ArchiveReason.SUCCEEDED,
            "version": 1,
        }
    )
    after = snapshot(store, current.id)
    assert authority(after) == authority(before)
    assert after["console"]["facts"] == before["console"]["facts"]
    assert after["console"]["state_version"] == before["console"]["state_version"]
    if new_flow:
        assert after == before
    else:
        assert current.current_round_no == 2
        assert after["console"]["activity_version"] == before["console"]["activity_version"] + 2


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"confirmed": False}, "explicit confirmation"),
        ({"confirmed": 1}, "explicit confirmation"),
        ({"actor": ""}, "actor and reason"),
        ({"actor": " \n "}, "actor and reason"),
        ({"reason": ""}, "actor and reason"),
        ({"reason": " \n "}, "actor and reason"),
        ({"expected_version": 1}, "version conflict"),
        ({"expected_version": -1}, "version conflict"),
    ],
)
def test_correction_requires_explicit_intent_and_current_version(store, archived, changes, error):
    rounds = RoundStore(store.database)
    rounds.observe_merge(archived.id, 1, MERGE)
    before = snapshot(store, archived.id)
    with pytest.raises(ValueError, match=error):
        rounds.correct_archive(archived.id, 1, **{**CORRECTION, **changes})
    assert snapshot(store, archived.id) == before


@pytest.mark.parametrize(
    "merged_at,sha",
    [
        (None, None),
        (None, MERGE.merge_commit_sha),
        (MERGED_AT, None),
        (MERGED_AT, " "),
    ],
)
def test_correction_requires_complete_saved_evidence(store, archived, merged_at, sha):
    # Imported historical rows may contain partial evidence; neither half establishes a merge.
    with store.connection() as connection:
        connection.execute(
            "UPDATE flow_rounds SET merged_at=%s,merge_commit_sha=%s WHERE flow_id=%s",
            (merged_at, sha, archived.id),
        )
    rounds = RoundStore(store.database)
    before = snapshot(store, archived.id)
    with pytest.raises(ValueError, match="saved PR merge evidence"):
        rounds.correct_archive(archived.id, 1, **CORRECTION)
    assert snapshot(store, archived.id) == before


@pytest.mark.parametrize(
    "archive_delay,allowed",
    [
        (timedelta(seconds=-1), False),
        (timedelta(0), False),
        (timedelta(microseconds=500000), False),
        (timedelta(microseconds=999999), False),
        (timedelta(seconds=1), False),
        (timedelta(seconds=1, microseconds=1), True),
    ],
)
def test_second_precision_requires_strictly_proven_order(store, archived, archive_delay, allowed):
    # Fixtures place the original archive around GitHub's second-resolution timestamp.
    at = MERGED_AT + archive_delay
    with store.connection() as connection:
        connection.execute("UPDATE flows SET archived_at=%s WHERE id=%s", (at, archived.id))
        connection.execute(
            "UPDATE flow_rounds SET archived_at=%s WHERE flow_id=%s", (at, archived.id)
        )
    rounds = RoundStore(store.database)
    assert rounds.observe_merge(archived.id, 1, MERGE)
    before = snapshot(store, archived.id)
    if allowed:
        assert (
            rounds.correct_archive(archived.id, 1, **CORRECTION).archive_reason
            is ArchiveReason.SUCCEEDED
        )
    else:
        with pytest.raises(ValueError, match="does not prove merge before archive"):
            rounds.correct_archive(archived.id, 1, **CORRECTION)
        assert snapshot(store, archived.id) == before


def test_only_existing_abandoned_archives_with_a_pr_are_eligible(store):
    service = ControlService(database=store.database)
    rounds = RoundStore(store.database)
    flow = service.on_trigger("owner/repo", 7, 42)
    assert flow is not None
    before = snapshot(store, flow.id)
    for method, args in (
        (rounds.correct_archive, CORRECTION),
        (rounds.observe_merge, {"merge": MERGE}),
    ):
        with pytest.raises(ValueError, match="archived round does not exist"):
            method(flow.id, 1, **args)
        with pytest.raises(ValueError, match="Flow does not exist"):
            method(flow.id + 100, 1, **args)
    assert snapshot(store, flow.id) == before
    service.cancel(flow)
    before = snapshot(store, flow.id)
    with pytest.raises(ValueError, match="does not belong"):
        rounds.observe_merge(flow.id, 1, MERGE)
    with pytest.raises(ValueError, match="saved PR merge evidence"):
        rounds.correct_archive(flow.id, 1, **CORRECTION)
    with pytest.raises(ValueError, match="archived round does not exist"):
        rounds.correct_archive(flow.id, 2, **CORRECTION)
    assert snapshot(store, flow.id) == before
    succeeded = service.on_trigger("owner/repo", 8, 42)
    assert succeeded is not None
    service.finish(
        succeeded, reason=ArchiveReason.SUCCEEDED, summary="No change needed", expected_round_no=1
    )
    with pytest.raises(ValueError, match="only an abandoned"):
        rounds.correct_archive(succeeded.id, 1, **CORRECTION)


@pytest.mark.parametrize("failure", ["outer-transaction", "activity-insert"])
def test_correction_audit_and_projection_roll_back_together(store, archived, failure):
    rounds = RoundStore(store.database)
    rounds.observe_merge(archived.id, 1, MERGE)
    before = snapshot(store, archived.id)
    if failure == "outer-transaction":
        with pytest.raises(RuntimeError, match="rollback"), store.transaction() as tx:
            corrected = RoundStore(tx.database).correct_archive(archived.id, 1, **CORRECTION)
            assert corrected.version == 1
            assert len(snapshot(tx, archived.id)["corrections"]) == 1
            # A second connection sees neither the conclusion nor the audit before commit.
            assert snapshot(store, archived.id) == before
            raise RuntimeError("rollback")
    else:
        with store.connection() as connection:
            connection.execute(
                "ALTER TABLE console_activity ADD CONSTRAINT reject_correction_activity"
                " CHECK (kind<>'round' OR body::jsonb->'correction'='null'::jsonb) NOT VALID"
            )
        with pytest.raises(CheckViolation, match="reject_correction_activity"):
            rounds.correct_archive(archived.id, 1, **CORRECTION)
    assert snapshot(store, archived.id) == before


def test_trigger_preserves_handler_activity_and_captures_direct_sql_writers(store, archived):
    before = snapshot(store, archived.id)
    inserted = next(item for item in before["activity"] if item["kind"] == "round")
    event = json.loads(inserted["body"])
    assert event["before"] is None and event["correction"] is None
    assert FlowRound.model_validate(event["after"]) == store.flows.get_round(archived.id, 1)
    history = ActivityStore(store.database)
    observation = Observation(
        key="issue:comment:1",
        source="issue.comments",
        kind="comment",
        author="maintainer",
        body="Keep this history",
        occurred_at=NOW,
        updated_at=NOW,
    )
    assert history.append(archived.id, observation)
    with store.connection() as connection:
        connection.execute(
            "UPDATE flow_rounds SET archive_summary='Historical context' WHERE flow_id=%s",
            (archived.id,),
        )
    after = snapshot(store, archived.id)
    assert authority(after) == authority(before)
    assert after["console"]["state_version"] == before["console"]["state_version"]
    assert after["console"]["facts"] == before["console"]["facts"]
    assert after["console"]["activity_version"] == before["console"]["activity_version"] + 2
    assert [item["version"] for item in after["activity"]] == list(
        range(1, after["console"]["activity_version"] + 1)
    )
    event = json.loads(after["activity"][-1]["body"])
    assert event["before"]["archive_summary"] == archived.archive_summary
    assert event["after"]["archive_summary"] == "Historical context"
    with store.connection() as connection:
        connection.execute(ROUND_AUDIT_SCHEMA)
        connection.execute(ROUND_AUDIT_SCHEMA)
        connection.execute(
            "UPDATE flow_rounds SET version=version WHERE flow_id=%s", (archived.id,)
        )
    assert not history.append(archived.id, observation)
    assert snapshot(store, archived.id) == after


@pytest.mark.parametrize("first", ["correction", "resume"])
def test_resume_and_correction_serialize_without_losing_either_result(store, archived, first):
    rounds = RoundStore(store.database)
    rounds.observe_merge(archived.id, 1, MERGE)
    with store.transaction() as tx:
        if first == "correction":
            RoundStore(tx.database).correct_archive(archived.id, 1, **CORRECTION)
        else:
            ControlService(database=tx.database).resume(archived, expected_round_no=1)
        with (
            pytest.raises(LockNotAvailable),
            store.transaction() as other,
            other.connection() as connection,
        ):
            connection.execute("SET LOCAL lock_timeout='50ms'")
            if first == "correction":
                ControlService(database=other.database).resume(archived, expected_round_no=1)
            else:
                RoundStore(other.database).correct_archive(archived.id, 1, **CORRECTION)
        # Both paths hold the Flow fence, not merely the same released worker's lock.
        with pytest.raises(LockNotAvailable), store.connection() as connection:
            connection.execute("SELECT id FROM flows WHERE id=%s FOR UPDATE NOWAIT", (archived.id,))
    if first == "correction":
        ControlService(database=store.database).resume(archived, expected_round_no=1)
    else:
        rounds.correct_archive(archived.id, 1, **CORRECTION)
    current = store.flows.get_flow(archived.id)
    assert current.current_round_no == 2 and current.state is FlowState.QUEUED
    assert current.archive_reason is None and current.archived_at is None
    assert rounds.by_pr(archived.repo, MERGE.number).archive_reason is ArchiveReason.SUCCEEDED
    assert len(snapshot(store, archived.id)["corrections"]) == 1
