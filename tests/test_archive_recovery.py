"""PostgreSQL archive receipts, failures, fencing, and atomic resource release."""

from datetime import timedelta

import pytest

from app.contracts.protocol import (
    PROTOCOL_VERSION,
    ArchiveResult,
    ArchiveTask,
    Failed,
    RecoveryDescriptor,
    Succeeded,
    terminal_digest,
)
from app.controler.workers.conversation import ConversationBroker
from app.controler.workers.gateway import ProtocolViolation, WorkerGateway
from app.controler.workflow.service import ControlService
from app.domain.models import DeliveryState, FlowState, JobKind, JobState
from app.persistence.jobs import JobStore
from tests.github import RecordingGitHub
from tests.test_coordinator import coordinator
from tests.test_gateway import ScriptedSocket
from tests.test_merge import route_merge
from tests.test_result_recovery import (
    completed_open_initial as completed_open_initial,  # noqa: PLC0414
)
from tests.test_workflow_transactions import accepted_coding


async def ready_archive(store, *, start=True):
    old, _ = accepted_coding(store)
    flow = store.flows.get_flow(old.flow_id)
    await route_merge(store, flow)
    subject = coordinator(store, RecordingGitHub())
    await subject.results.apply(old.id)
    job = store.workers.assign_job("worker-a", "archive-token", "epoch-a")
    task = await subject.prepare(job)
    assert isinstance(task, ArchiveTask)
    if start:
        assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "archive-token", "epoch-a")
    return flow, job, task, subject


def receive(store, job, result, *, worker="worker-a", token="archive-token", epoch="epoch-a"):
    message = Succeeded(job_id=job.id, assignment_token=token, connection_epoch=epoch, result=result)
    return store.workers.record_assignment_result(job.id, worker, token, epoch,
                                          message.model_dump(mode="json"), terminal_digest(message))


async def test_archive_release_rolls_back_and_replays_atomically(store, monkeypatch, recording_resources):
    flow, job, task, subject = await ready_archive(store)
    result = ArchiveResult(**task.model_dump(), cleanup="removed")
    assert receive(store, job, result) == "pending"
    original = JobStore.complete_persisted_result

    def interrupted(self, job_id, state):
        assert original(self, job_id, state)
        raise RuntimeError("commit interrupted")

    monkeypatch.setattr(JobStore, "complete_persisted_result", interrupted)
    with pytest.raises(RuntimeError, match="commit interrupted"):
        await subject.results.apply(job.id)
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert store.jobs.get_job(job.id).result_applied_at is None
    assert store.results.get_plan(job.id).archive == result
    other = ControlService(
        database=store.database,
    ).on_trigger("owner/repo", 999, 42)
    assert store.workers.assign_job("worker-a", "next", "epoch-a") is None

    monkeypatch.setattr(JobStore, "complete_persisted_result", original)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert await subject.results.apply(job.id) is JobState.DONE
    assert store.flows.get_flow(flow.id).state is FlowState.SUCCEEDED
    assert store.workers.get_flow_execution(flow.id).released_at is not None
    assert flow.id not in [item.id for item in store.flows.list_flows(active_only=True)]
    assert store.workers.assign_job("worker-a", "next", "epoch-a").flow_id == other.id


async def test_archive_fences_do_not_release_binding(store, recording_resources):
    flow, job, task, _subject = await ready_archive(store)
    result = ArchiveResult(**task.model_dump(), cleanup="removed")
    for kwargs in [{"worker": "worker-b"}, {"token": "old"}, {"epoch": "old"}]:
        assert receive(store, job, result, **kwargs) == "rejected"
    assert receive(store, job, result) == "pending"
    assert store.workers.get_flow_execution(flow.id).released_at is None


async def test_archive_after_initial_retry_cannot_adopt_a_released_binding(store, completed_open_initial):
    # Merge can arrive after retry commits ACTIVE but before a new worker reacquires ownership.
    flow, _subject, _github, _request, _result = completed_open_initial
    service = ControlService(
        database=store.database,
    )
    service.cancel(flow)
    service.retry(flow)
    await route_merge(store, flow)
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.MERGED
    assert store.workers.get_flow_execution(flow.id).released_at is not None
    archive, = [job for job in store.jobs.list_jobs(flow.id) if job.kind is JobKind.ARCHIVE]
    store.workers.worker_connected("worker-b", PROTOCOL_VERSION, "epoch-b")
    assert store.workers.assign_job("worker-b", "adopt", "epoch-b") is None
    assert store.workers.assign_job("worker-a", "archive", "epoch") is None
    assert store.jobs.get_job(archive.id).state is JobState.QUEUED


async def test_failed_archive_retry_keeps_attempt_and_original_worker(store, recording_resources):
    flow, job, task, subject = await ready_archive(store)
    message = Failed(job_id=job.id, assignment_token="archive-token", connection_epoch="epoch-a",
                     error="Permission denied")
    assert store.workers.record_assignment_result(job.id, "worker-a", "archive-token", "epoch-a",
                                          message.model_dump(mode="json"), terminal_digest(message)) == "pending"
    assert await subject.results.apply(job.id) is JobState.FAILED
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.workers.get_flow_execution(flow.id).released_at is None
    service = ControlService(
        database=store.database,
    )
    with pytest.raises(ValueError, match="merged.*archive|archive.*merged"):
        service.cancel(flow)
    service.retry(flow)
    store.workers.worker_connected("worker-b", PROTOCOL_VERSION, "epoch-b")
    assert store.workers.assign_job("worker-b", "other", "epoch-b") is None
    retried = store.workers.assign_job("worker-a", "retry", "epoch-a")
    assert retried.kind is JobKind.ARCHIVE
    assert await subject.prepare(retried) == task
    assert store.flows.get_flow(flow.id).current_attempt_id == task.attempt_id


async def test_expired_merged_executor_is_pinned_until_original_worker_returns(store):
    old, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(old.flow_id)
    await route_merge(store, flow)
    store.workers.worker_disconnected("worker-a", connection_epoch="epoch-a")
    now = store.now()
    store._db._clock = lambda: now + timedelta(hours=1)
    store.workers.sweep_expired_assignments()
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert store.jobs.get_job(old.id).state is JobState.RUNNING
    assert "executor" in store.jobs.get_job(old.id).last_error
    with pytest.raises(ValueError, match="executor|running"):
        ControlService(
            database=store.database,
        ).retry(flow)
    store.workers.worker_connected("worker-b", PROTOCOL_VERSION, "epoch-b")
    assert store.workers.assign_job("worker-b", "other", "epoch-b") is None
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "new-epoch")
    assert store.workers.resume_assignment(old.id, "worker-a", "triage-token", "new-epoch")


@pytest.mark.parametrize("journal", ["missing", "started-without-executor"])
async def test_merged_coding_process_without_exit_proof_cannot_unlock_archive(store, journal):
    old, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(old.flow_id)
    await route_merge(store, flow)
    subject = coordinator(store, RecordingGitHub())
    gateway = WorkerGateway(
        subject, database=store.database,
        worker_tokens={"worker-a": "secret"},
        tools=subject.tools,
        results=subject.results,
        conversations=ConversationBroker(),
    )
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "restart")
    with pytest.raises(ProtocolViolation, match="archive blocked"):
        if journal == "missing":
            await gateway._reject_missing_journal("worker-a", "restart", old.id)
        else:
            await gateway._recover(ScriptedSocket(), "worker-a", "restart", RecoveryDescriptor(
                job_id=old.id, assignment_token=old.assignment_token,
                state="started", executor_active=False,
            ))
    assert store.jobs.get_job(old.id).state is JobState.RUNNING
    assert store.jobs.get_job(old.id).result_received_at is None
    assert store.workers.get_flow_execution(flow.id).released_at is None


async def test_restart_between_archive_claim_and_freeze_requeues_safely(store, recording_resources):
    # A claim commits before coordinator.prepare(). This is a current, unstarted delivery.
    old, _ = accepted_coding(store)
    flow = store.flows.get_flow(old.flow_id)
    await route_merge(store, flow)
    subject = coordinator(store, RecordingGitHub())
    await subject.results.apply(old.id)
    job = store.workers.assign_job("worker-a", "archive-token", "epoch-a")
    assert job.payload is None and job.started_at is None
    store.workers.recover_abandoned_assignments()
    now = store.now()
    store._db._clock = lambda: now + timedelta(hours=1)
    store.workers.sweep_expired_assignments()
    assert store.jobs.get_job(job.id).state is JobState.QUEUED
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "restart")
    recovered = store.workers.assign_job("worker-a", "new-token", "restart")
    assert recovered.id == job.id
    assert isinstance(await subject.prepare(recovered), ArchiveTask)
    assert store.workers.get_flow_execution(flow.id).released_at is None


async def test_superseded_queued_coding_does_not_block_archive_retry(store, recording_resources):
    # Lease recovery can requeue coding work after merge already retired its initial queue.
    flow, job, _task, subject = await ready_archive(store)
    stale = store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    await subject.fail(job, "cleanup permission denied")
    ControlService(
        database=store.database,
    ).retry(flow)
    retry = store.workers.assign_job("worker-a", "retry", "epoch-a")
    assert retry.kind is JobKind.ARCHIVE and retry.id != job.id
    assert store.jobs.get_job(stale.id).state is JobState.QUEUED


@pytest.mark.parametrize("mismatch", ["worker", "token", "epoch"])
async def test_lost_archive_started_requires_original_fence_and_frozen_identity(store, mismatch, recording_resources):
    _flow, job, _task, _subject = await ready_archive(store, start=False)
    worker, token, epoch = "worker-a", "archive-token", "epoch-a"
    if mismatch == "worker":
        worker = "worker-b"
        store.workers.worker_connected(worker, PROTOCOL_VERSION, epoch)
    elif mismatch == "token":
        token = "old-token"
    else:
        epoch = "old-epoch"
    assert not store.workers.resume_assignment(job.id, worker, token, epoch)
    assert store.jobs.get_job(job.id).started_at is None
