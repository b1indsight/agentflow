"""Merged PR delivery and job retirement must share the PostgreSQL commit boundary."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr, ValidationError

from app.contracts.protocol import ExecutionStopped
from app.controler.config import ServerSettings
from app.controler.events.router import EventRouter
from app.controler.workflow.service import ControlService
from app.domain.models import ArchiveReason, DeliveryState, Flow, FlowState, JobKind, JobState
from app.domain.review_models import BatchState
from app.persistence.activity import ActivityStore
from app.persistence.jobs import JobStore
from app.persistence.store import Store
from tests.github import RecordingGitHub
from tests.test_coordinator import coordinator
from tests.test_workflow_transactions import accepted_coding
from tests.workspace_fixtures import report_settled


def merge_payload(flow: Flow) -> dict:
    return {
        "action": "closed",
        "repository": {"full_name": flow.repo},
        "installation": {"id": flow.installation_id},
        "sender": {"login": "maintainer", "type": "User"},
        "pull_request": {
            "number": 17, "merged": True,
            "merged_at": "2026-09-08T08:00:00Z", "merge_commit_sha": "a" * 40,
        },
    }


async def route_merge(store: Store, flow: Flow, event: str = "merge-1", payload=None):
    settings = ServerSettings.model_construct(database_url="postgresql://localhost/test",
                             worker_tokens={"worker-1": SecretStr("worker-secret")},
                             allowlist=["alice"], bot_login="flowtool[bot]", trigger_label="flow",
                               settle_seconds=75.0, submit_grace_seconds=15.0)
    EventRouter(
        ControlService(
            database=store.database,
        ),
        settings,
        database=store.database,
    ).dispatch(
        "pull_request",
        event,
        payload if payload is not None else merge_payload(flow),
    )


@pytest.fixture
def merge_flow(store: Store) -> Flow:
    flow = store.flows.create_flow("owner/repo", 7, 42)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.state, delivery.pr_number = DeliveryState.OPEN, 17
    delivery.head_sha = "b" * 40  # A squash/rebase merge need not equal the PR head.
    store.deliveries.update_delivery(delivery)
    return flow


@pytest.mark.parametrize("boundary", ["notice", "activity"])
async def test_merge_transaction_failure_can_replay_same_delivery(store, monkeypatch, boundary):
    # Neither a persisted stop/notice nor its archive activity may survive a failed commit.
    running, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(running.flow_id)
    queued = store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    owner, method = ((JobStore, "record_merge_notice") if boundary == "notice"
                     else (ActivityStore, "append_flow"))
    original = getattr(owner, method)

    def interrupted(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("archive commit interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(owner, method, interrupted)
        with pytest.raises(RuntimeError, match="archive commit interrupted"):
            await route_merge(store, flow)
    assert not store.webhooks.event_seen("merge-1")
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.OPEN
    assert store.flows.get_flow(flow.id).state is FlowState.RUNNING
    assert not store.stops.has_pending(flow.id)
    assert ActivityStore(store.database).archived(flow.id, 1) is None
    assert not any(job.kind is JobKind.ARCHIVE for job in store.jobs.list_jobs(flow.id))
    assert store.jobs.get_job(queued.id).state is JobState.QUEUED
    await route_merge(store, flow)
    assert store.webhooks.event_seen("merge-1")
    assert store.jobs.get_job(queued.id).state is JobState.FAILED


def test_concurrent_merge_deliveries_enqueue_once(store, merge_flow):
    # Different delivery IDs can describe the same merge, including simultaneous redelivery.
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(
            lambda event: asyncio.run(route_merge(store, merge_flow, event)),
            ["merge-1", "merge-1", "merge-2"],
        ))
    delivery = store.deliveries.get_delivery(merge_flow.id)
    assert delivery.state is DeliveryState.MERGED
    assert delivery.merge_commit_sha == "a" * 40
    assert delivery.merged_at == datetime(2026, 9, 8, 8, tzinfo=UTC)
    assert delivery.version == 1
    assert [job.kind for job in store.jobs.list_jobs(merge_flow.id)] == [JobKind.ARCHIVE]


@pytest.mark.parametrize("case", ["bot", "wrong-installation", "unmerged", "approved", "unknown"])
async def test_merge_routing_uses_pr_identity_not_feedback_authorization(store, merge_flow, case):
    payload = merge_payload(merge_flow)
    if case == "bot":
        payload["sender"] = {"login": "flowtool[bot]", "type": "Bot"}
    elif case == "wrong-installation":
        payload["installation"]["id"] = 99
    elif case == "unmerged":
        payload["pull_request"]["merged"] = False
    elif case == "approved":
        payload["action"] = "edited"
    else:
        payload["pull_request"]["number"] = 999
    await route_merge(store, merge_flow, payload=payload)
    assert bool(store.jobs.list_jobs(merge_flow.id)) is (case == "bot")


async def test_archived_flow_corrects_prearchive_merge_without_reactivation(store, merge_flow):
    ControlService(database=store.database).cancel(merge_flow)
    activity = ActivityStore(store.database)
    archived = activity.archived(merge_flow.id, 1)
    assert archived is not None and archived.metadata["archive_reason"] == "abandoned"
    delivery = store.deliveries.get_delivery(merge_flow.id)
    payload = merge_payload(merge_flow)
    payload["pull_request"]["merged_at"] = (
        archived.occurred_at - timedelta(seconds=5)
    ).isoformat()
    await route_merge(store, merge_flow, payload=payload)
    assert store.deliveries.get_delivery(merge_flow.id) == delivery
    assert activity.archived(merge_flow.id, 1) == archived
    history = activity.history(merge_flow.id, 1)
    merges = [item for item in history if item.kind == "merge"]
    assert len(merges) == 1
    assert merges[0].metadata["merge_commit_sha"] == "a" * 40
    assert len([item for item in history if item.kind == "archive_correction"]) == 1
    assert store.flows.get_flow(merge_flow.id).state is FlowState.ARCHIVED
    assert store.flows.get_flow(merge_flow.id).archive_reason is ArchiveReason.SUCCEEDED
    assert store.jobs.list_jobs(merge_flow.id) == []


async def test_invalid_merge_facts_can_be_corrected_before_redelivery(store, merge_flow):
    payload = merge_payload(merge_flow)
    payload["pull_request"]["merged_at"] = None
    with pytest.raises(ValidationError):
        await route_merge(store, merge_flow, payload=payload)
    assert not store.webhooks.event_seen("merge-1")
    await route_merge(store, merge_flow)
    assert store.deliveries.get_delivery(merge_flow.id).state is DeliveryState.MERGED


@pytest.mark.parametrize("binding", ["missing", "released", "running", "offline"])
async def test_merge_archives_business_without_waiting_for_worker(store, merge_flow, binding):
    # A released binding was the production blocker; an active executor must instead
    # retain its exact resources without delaying the business conclusion.
    flow = merge_flow
    if binding != "missing":
        job, _ = accepted_coding(store, receive=False)
        flow = store.flows.get_flow(job.flow_id)
        if binding == "released":
            with store.database.connection() as connection:
                connection.execute("UPDATE flow_executions SET released_at=%s WHERE flow_id=%s",
                                   (store.now(), flow.id))
        elif binding == "offline":
            store.workers.worker_disconnected("worker-a", connection_epoch="epoch-a")
    execution = store.workers.get_flow_execution(flow.id)
    await route_merge(store, flow)
    await route_merge(store, flow, "merge-duplicate")
    archived = store.flows.get_flow(flow.id)
    assert (archived.state, archived.archive_reason) == (FlowState.ARCHIVED, ArchiveReason.SUCCEEDED)
    history = ActivityStore(store.database).history(flow.id, 1)
    assert len([item for item in history if item.kind == "archive"]) == 1
    notices = [item for item in store.jobs.list_jobs(flow.id) if item.kind is JobKind.ARCHIVE]
    assert len(notices) == 1
    assert notices[0].assigned_worker_id is None and notices[0].assignment_token is None
    assert notices[0].result_payload["terminal"]["type"] == "merge_archive"
    assert store.workers.get_flow_execution(flow.id) == execution
    assert store.stops.has_pending(flow.id) is (binding in {"running", "offline"})


async def test_merge_retires_review_but_keeps_resources_until_stop_cleanup(store, recording_resources):
    # Merge can arrive after worker completion but before its durable result has been applied.
    job, _ = accepted_coding(store)
    flow = store.flows.get_flow(job.flow_id)
    await route_merge(store, flow)
    assert store.reviews.for_job(job).state is BatchState.CANCELLED
    assert store.workers.assign_job("worker-a", "archive-token", "epoch-a") is None
    github = RecordingGitHub()
    subject = coordinator(store, github)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert await subject.results.apply(job.id) is JobState.DONE
    assert github.comments == []
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.MERGED
    assert store.workers.assign_job("worker-a", "before-ack", "epoch-a") is None
    report_settled(store, job.id, epoch="epoch-a")
    assert store.workers.assign_job("worker-a", "after-coding-ack", "epoch-a") is None
    assert [j.id for j in store.jobs.list_jobs(flow.id) if j.kind is JobKind.REVISION] == [job.id]
    assert store.workers.get_flow_execution(flow.id).released_at is None
    stop = store.stops.pending("worker-a")
    assert store.stops.confirm("worker-a", ExecutionStopped(
        **stop.fence().model_dump(), execution_nonce="original-executor",
    ), "epoch-a")
    report_settled(store, job.id, epoch="epoch-a", stopped=True)
    assert store.workers.get_flow_execution(flow.id).released_at is not None
    notice = store.jobs.list_jobs(flow.id)[-1]
    assert notice.assigned_worker_id is None and notice.kind is JobKind.ARCHIVE
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.flows.get_flow(flow.id).archive_reason is ArchiveReason.SUCCEEDED
