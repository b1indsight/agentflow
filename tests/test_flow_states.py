"""Business-state contracts through the existing service and PostgreSQL transactions."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.controler.events.router import EventRouter
from app.controler.workflow.service import ControlService
from app.domain.models import ArchiveReason, DeliveryState, FlowState, JobKind, ReviewItemKind
from app.domain.review_models import FeedbackUpdate
from app.persistence.console import ConsoleStore


def test_investigation_archive_and_explicit_new_round(store):
    service = ControlService(database=store.database)
    flow = service.on_trigger("owner/repo", 7, 42)
    assert flow.state is FlowState.QUEUED
    with pytest.raises(ValueError, match="conclusion"):
        service.finish(flow, reason=ArchiveReason.SUCCEEDED, summary="", expected_round_no=1)
    service.finish(
        flow,
        reason=ArchiveReason.SUCCEEDED,
        summary="Investigation complete: no change needed.",
        expected_round_no=1,
    )
    archived = store.flows.get_flow(flow.id)
    assert archived.state is FlowState.ARCHIVED
    assert archived.archive_reason is ArchiveReason.SUCCEEDED
    assert store.deliveries.get_delivery(flow.id) is None
    assert ConsoleStore(store.database).detail(flow.id).phase == "archived"
    resumed = service.resume(archived, expected_round_no=1)
    assert resumed.id == flow.id and resumed.current_round_no == 2
    assert resumed.state is FlowState.QUEUED and resumed.archived_at is None
    assert store.flows.get_round(flow.id, 1).archive_summary == archived.archive_summary
    with pytest.raises(ValueError):
        service.resume(archived, expected_round_no=1)


def test_blocked_still_owns_issue_and_archive_releases_it(store):
    service = ControlService(database=store.database)
    flow = service.on_trigger("owner/repo", 7, 42)
    flow.state = FlowState.BLOCKED
    store.flows.update_flow(flow)
    assert service.on_trigger("owner/repo", 7, 42) is None
    service.cancel(flow)
    assert store.flows.get_flow(flow.id).archive_reason is ArchiveReason.ABANDONED
    newer = service.on_trigger("owner/repo", 7, 42)
    assert newer is not None
    with pytest.raises(ValueError, match="already active"):
        service.resume(flow, expected_round_no=1)


def test_old_pr_merge_records_evidence_without_affecting_resumed_round(store):
    # A delayed merge webhook still names the previous PR after a new delivery starts.
    service = ControlService(database=store.database)
    flow = service.on_trigger("owner/repo", 7, 42)
    delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.state, delivery.pr_number = DeliveryState.OPEN, 17
    store.deliveries.update_delivery(delivery)
    service.cancel(flow)
    archived = store.flows.get_round(flow.id, 1)
    resumed = service.resume(flow, expected_round_no=1)
    router = EventRouter(service, SimpleNamespace(), database=store.database)
    payload = {
        "action": "closed",
        "repository": {"full_name": flow.repo},
        "installation": {"id": 99},
        "pull_request": {
            "number": 17,
            "merged": True,
            "merged_at": (archived.archived_at - timedelta(seconds=5)).isoformat(),
            "merge_commit_sha": "a" * 40,
        },
    }
    router.dispatch("pull_request", "late-merge", payload)
    assert not store.webhooks.event_seen("late-merge")
    payload["installation"]["id"] = 42
    router.dispatch("pull_request", "late-merge", payload)
    router.dispatch("pull_request", "late-merge", payload)
    assert store.flows.get_flow(flow.id) == resumed
    historical = store.flows.get_round(flow.id, 1)
    assert historical.merge_commit_sha == "a" * 40
    assert historical.archive_reason is ArchiveReason.ABANDONED
    assert historical.archived_at == archived.archived_at
    assert [job.kind for job in store.jobs.list_jobs(flow.id)] == [JobKind.INITIAL, JobKind.INITIAL]
    assert store.deliveries.get_delivery(flow.id).pr_number is None


def test_resume_does_not_replay_prior_round_feedback_or_cursors(store):
    service = ControlService(database=store.database)
    flow = service.on_trigger("owner/repo", 7, 42)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.state, delivery.pr_number = DeliveryState.OPEN, 17
    store.deliveries.update_delivery(delivery)
    store.reviews.ingest(
        flow.id,
        "old-feedback",
        "pull_request_review",
        {},
        FeedbackUpdate(
            kind=ReviewItemKind.REVIEW_BODY,
            github_id=12,
            author="reviewer",
            body="Old delivery only",
            signal="request_changes",
            submitted=True,
            updated_at=store.now(),
        ),
        settle_seconds=0,
        submit_grace_seconds=0,
    )
    store.reconcile.feedback_seed(10)
    assert store.reviews.pending(flow.id)
    assert store.reconcile.feedback_get(flow.id, "reviews") is not None
    service.cancel(flow)
    resumed = service.resume(flow, expected_round_no=1)
    resumed.current_attempt_id = store.flows.create_attempt(flow.id).id
    store.flows.update_flow(resumed)
    assert store.reviews.pending(flow.id) == []
    assert store.reviews.claim_due() == []
    assert store.reconcile.feedback_get(flow.id, "reviews") is None
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM review_versions").fetchone()["n"] == 1
