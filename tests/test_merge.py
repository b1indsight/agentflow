"""Merged PR delivery and job retirement must share the PostgreSQL commit boundary."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr, ValidationError

from app.contracts.protocol import ArchiveResult, ArchiveTask, Succeeded, terminal_digest
from app.controler.config import ServerSettings
from app.controler.events.router import EventRouter
from app.controler.workflow.service import ControlService
from app.domain.models import DeliveryState, Flow, FlowState, JobKind, JobState
from app.domain.review_models import BatchState
from app.persistence.jobs import JobStore
from app.persistence.store import Store
from tests.github import RecordingGitHub
from tests.test_coordinator import coordinator
from tests.test_workflow_transactions import accepted_coding


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
            deliveries=store.deliveries,
            flows=store.flows,
            jobs=store.jobs,
            result_store=store.results,
            reviews=store.reviews,
            stops=store.stops,
            webhook_store=store.webhooks,
            workers=store.workers,
        ),
        settings,
        database=store.database,
        flows=store.flows,
        reconcile_store=store.reconcile,
        reviews=store.reviews,
        webhook_store=store.webhooks,
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


async def test_merge_transaction_failure_can_replay_same_delivery(store, merge_flow, monkeypatch):
    # A recorded webhook without its archive job would otherwise be lost on redelivery.
    queued = store.jobs.enqueue_job(merge_flow.id, JobKind.REVISION)
    original = JobStore.enqueue_job

    def interrupted(self, flow_id, kind, *args, **kwargs):
        if kind is JobKind.ARCHIVE:
            raise RuntimeError("archive enqueue interrupted")
        return original(self, flow_id, kind, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(JobStore, "enqueue_job", interrupted)
        with pytest.raises(RuntimeError, match="archive enqueue interrupted"):
            await route_merge(store, merge_flow)
    assert not store.webhooks.event_seen("merge-1")
    assert store.deliveries.get_delivery(merge_flow.id).state is DeliveryState.OPEN
    assert store.jobs.get_job(queued.id).state is JobState.QUEUED
    await route_merge(store, merge_flow)
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


@pytest.mark.parametrize("state", [FlowState.FAILED, FlowState.CANCELLED])
async def test_terminal_flow_keeps_merge_fact_without_reactivation(store, merge_flow, state):
    merge_flow.state = state
    store.flows.update_flow(merge_flow)
    await route_merge(store, merge_flow)
    assert store.deliveries.get_delivery(merge_flow.id).state is DeliveryState.MERGED
    assert store.flows.get_flow(merge_flow.id).state is state
    assert store.jobs.list_jobs(merge_flow.id) == []


async def test_invalid_merge_facts_can_be_corrected_before_redelivery(store, merge_flow):
    payload = merge_payload(merge_flow)
    payload["pull_request"]["merged_at"] = None
    with pytest.raises(ValidationError):
        await route_merge(store, merge_flow, payload=payload)
    assert not store.webhooks.event_seen("merge-1")
    await route_merge(store, merge_flow)
    assert store.deliveries.get_delivery(merge_flow.id).state is DeliveryState.MERGED


async def test_merge_retires_review_but_waits_for_running_result(store, recording_resources):
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
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.MERGED
    archive = store.workers.assign_job("worker-a", "archive-token", "epoch-a")
    assert archive is not None and archive.kind is JobKind.ARCHIVE
    assert [j.id for j in store.jobs.list_jobs(flow.id) if j.kind is JobKind.REVISION] == [job.id]
    assert store.workers.get_flow_execution(flow.id).released_at is None
    task = await subject.prepare(archive)
    assert isinstance(task, ArchiveTask)
    assert store.workers.start_assignment(archive.id, task.attempt_id, "worker-a", "archive-token", "epoch-a")
    message = Succeeded(job_id=archive.id, assignment_token="archive-token",
                        connection_epoch="epoch-a",
                        result=ArchiveResult(**task.model_dump(), cleanup="removed"))
    assert store.workers.record_assignment_result(
        archive.id, "worker-a", "archive-token", "epoch-a",
        message.model_dump(mode="json"), terminal_digest(message),
    ) == "pending"
    assert await subject.results.apply(archive.id) is JobState.DONE
    assert store.flows.get_flow(flow.id).state is FlowState.SUCCEEDED
