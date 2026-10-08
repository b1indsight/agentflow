"""PostgreSQL archive receipts, failures, fencing, and atomic resource release."""

from datetime import timedelta

import pytest

from app.contracts.protocol import (
    PROTOCOL_VERSION,
    ArchiveResult,
    ArchiveTask,
    Failed,
    Succeeded,
    terminal_digest,
)
from app.control.service import ControlService
from app.domain.models import FlowState, JobKind, JobState
from app.persistence.stop_store import StopStore
from app.persistence.store import Store
from tests.test_coordinator import RecordingGitHub, coordinator
from tests.test_merge import route_merge
from tests.test_workflow_transactions import accepted_coding


async def ready_archive(store, *, start=True):
    old, _ = accepted_coding(store)
    flow = store.get_flow(old.flow_id)
    await route_merge(store, flow)
    subject = coordinator(store, RecordingGitHub())
    await subject.results.apply(old.id)
    job = store.assign_job("worker-a", "archive-token", "epoch-a")
    task = await subject.prepare(job)
    assert isinstance(task, ArchiveTask)
    if start:
        assert store.start_assignment(job.id, task.attempt_id, "worker-a", "archive-token", "epoch-a")
    return flow, job, task, subject


def receive(store, job, result, *, worker="worker-a", token="archive-token", epoch="epoch-a"):
    message = Succeeded(job_id=job.id, assignment_token=token, connection_epoch=epoch, result=result)
    return store.record_assignment_result(job.id, worker, token, epoch,
                                          message.model_dump(mode="json"), terminal_digest(message))


async def test_archive_release_rolls_back_and_replays_atomically(store, monkeypatch):
    flow, job, task, subject = await ready_archive(store)
    result = ArchiveResult(**task.model_dump(), cleanup="removed")
    assert receive(store, job, result) == "pending"
    original = Store.complete_persisted_result

    def interrupted(self, job_id, state):
        assert original(self, job_id, state)
        raise RuntimeError("commit interrupted")

    monkeypatch.setattr(Store, "complete_persisted_result", interrupted)
    with pytest.raises(RuntimeError, match="commit interrupted"):
        await subject.results.apply(job.id)
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.get_flow_execution(flow.id).released_at is None
    assert store.get_job(job.id).result_applied_at is None
    assert store.results.get_plan(job.id).archive == result
    other = ControlService(store).on_trigger("owner/repo", 999, 42)
    assert store.assign_job("worker-a", "next", "epoch-a") is None

    monkeypatch.setattr(Store, "complete_persisted_result", original)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert await subject.results.apply(job.id) is JobState.DONE
    assert store.get_flow(flow.id).state is FlowState.SUCCEEDED
    assert store.get_flow_execution(flow.id).released_at is not None
    assert flow.id not in [item.id for item in store.list_flows(active_only=True)]
    assert store.assign_job("worker-a", "next", "epoch-a").flow_id == other.id


@pytest.mark.parametrize("field,value", [("flow_id", 99), ("attempt_id", 99),
    ("worker_id", "worker-b"), ("repo", "owner/other"),
    ("repo_identity", "worker-b:owner/repo"), ("delivery_version", 999)])
async def test_mismatched_archive_receipt_retains_binding(store, field, value):
    flow, job, task, subject = await ready_archive(store)
    result = ArchiveResult(**task.model_dump(), cleanup="removed").model_copy(update={field: value})
    assert receive(store, job, result) == "pending"
    assert await subject.results.apply(job.id) is JobState.FAILED
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.get_flow_execution(flow.id).released_at is None
    assert "archive" in store.get_job(job.id).last_error
    assert subject.github.comments == []


async def test_archive_fences_and_digest_conflict_do_not_release_binding(store):
    flow, job, task, _subject = await ready_archive(store)
    result = ArchiveResult(**task.model_dump(), cleanup="removed")
    for kwargs in [{"worker": "worker-b"}, {"token": "old"}, {"epoch": "old"}]:
        assert receive(store, job, result, **kwargs) == "rejected"
    assert receive(store, job, result) == "pending"
    assert receive(store, job, result.model_copy(update={"cleanup": "already_absent"})) == "conflict"
    assert store.get_flow_execution(flow.id).released_at is None


async def test_failed_archive_retry_keeps_attempt_and_original_worker(store):
    flow, job, task, subject = await ready_archive(store)
    message = Failed(job_id=job.id, assignment_token="archive-token", connection_epoch="epoch-a",
                     error="Permission denied")
    assert store.record_assignment_result(job.id, "worker-a", "archive-token", "epoch-a",
                                          message.model_dump(mode="json"), terminal_digest(message)) == "pending"
    assert await subject.results.apply(job.id) is JobState.FAILED
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.get_flow_execution(flow.id).released_at is None
    service = ControlService(store)
    with pytest.raises(ValueError, match="merged.*archive|archive.*merged"):
        service.cancel(flow)
    service.retry(flow)
    store.worker_connected("worker-b", PROTOCOL_VERSION, "epoch-b")
    assert store.assign_job("worker-b", "other", "epoch-b") is None
    retried = store.assign_job("worker-a", "retry", "epoch-a")
    assert retried.kind is JobKind.ARCHIVE
    assert await subject.prepare(retried) == task
    assert store.get_flow(flow.id).current_attempt_id == task.attempt_id


async def test_expired_merged_executor_is_pinned_until_original_worker_returns(store):
    old, _ = accepted_coding(store, receive=False)
    flow = store.get_flow(old.flow_id)
    await route_merge(store, flow)
    store.worker_disconnected("worker-a", connection_epoch="epoch-a")
    now = store.now()
    store._clock = lambda: now + timedelta(hours=1)
    store.sweep_expired_assignments()
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.get_flow_execution(flow.id).released_at is None
    assert store.get_job(old.id).state is JobState.RUNNING
    assert "executor" in store.get_job(old.id).last_error
    with pytest.raises(ValueError, match="executor|running"):
        ControlService(store).retry(flow)
    store.worker_connected("worker-b", PROTOCOL_VERSION, "epoch-b")
    assert store.assign_job("worker-b", "other", "epoch-b") is None
    store.worker_connected("worker-a", PROTOCOL_VERSION, "new-epoch")
    assert store.resume_assignment(old.id, "worker-a", "triage-token", "new-epoch")


async def test_archive_without_original_binding_cannot_be_adopted(store):
    flow, job, _task, subject = await ready_archive(store)
    await subject.fail(job, "cleanup failed")
    store.release_flow_execution(flow.id)
    with pytest.raises(ValueError, match="binding"):
        ControlService(store).retry(flow)
    store.enqueue_job(flow.id, JobKind.ARCHIVE)
    store.worker_connected("worker-b", PROTOCOL_VERSION, "epoch-b")
    assert store.assign_job("worker-b", "other", "epoch-b") is None


async def test_delivery_change_after_receipt_freeze_blocks_commit(store, monkeypatch):
    from app.control.results import ResultPending

    flow, job, task, subject = await ready_archive(store)
    assert receive(store, job, ArchiveResult(**task.model_dump(), cleanup="removed")) == "pending"

    async def changed_delivery(plan):
        delivery = store.get_delivery(flow.id)
        delivery.version += 1
        store.update_delivery(delivery)

    monkeypatch.setattr(subject.results.effects, "run", changed_delivery)
    with pytest.raises(ResultPending, match="archive ownership changed"):
        await subject.results.apply(job.id)
    assert store.get_flow_execution(flow.id).released_at is None
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.get_job(job.id).result_applied_at is None
    with pytest.raises(ValueError, match="pending result"):
        ControlService(store).retry(flow)


@pytest.mark.parametrize("journal", ["missing", "started-without-executor"])
async def test_merged_coding_process_without_exit_proof_cannot_unlock_archive(store, journal):
    from app.contracts.protocol import RecoveryDescriptor
    from app.control.gateway import ProtocolViolation, WorkerGateway

    old, _ = accepted_coding(store, receive=False)
    flow = store.get_flow(old.flow_id)
    await route_merge(store, flow)
    subject = coordinator(store, RecordingGitHub())
    gateway = WorkerGateway(store, subject, execution_control=StopStore(store), worker_tokens={"worker-a": "secret"})
    store.worker_connected("worker-a", PROTOCOL_VERSION, "restart")
    with pytest.raises(ProtocolViolation, match="archive blocked"):
        if journal == "missing":
            await gateway._reject_missing_journal("worker-a", "restart", old.id)
        else:
            from tests.test_gateway import ScriptedSocket

            await gateway._recover(ScriptedSocket(), "worker-a", "restart", RecoveryDescriptor(
                job_id=old.id, assignment_token=old.assignment_token,
                state="started", executor_active=False,
            ))
    assert store.get_job(old.id).state is JobState.RUNNING
    assert store.get_job(old.id).result_received_at is None
    assert store.get_flow_execution(flow.id).released_at is None


async def test_restart_between_archive_claim_and_freeze_requeues_safely(store):
    # A claim commits before coordinator.prepare(). This is a current, unstarted delivery.
    old, _ = accepted_coding(store)
    flow = store.get_flow(old.flow_id)
    await route_merge(store, flow)
    subject = coordinator(store, RecordingGitHub())
    await subject.results.apply(old.id)
    job = store.assign_job("worker-a", "archive-token", "epoch-a")
    assert job.payload is None and job.started_at is None
    store.recover_abandoned_assignments()
    now = store.now()
    store._clock = lambda: now + timedelta(hours=1)
    store.sweep_expired_assignments()
    assert store.get_job(job.id).state is JobState.QUEUED
    store.worker_connected("worker-a", PROTOCOL_VERSION, "restart")
    recovered = store.assign_job("worker-a", "new-token", "restart")
    assert recovered.id == job.id
    assert isinstance(await subject.prepare(recovered), ArchiveTask)
    assert store.get_flow_execution(flow.id).released_at is None


async def test_superseded_queued_coding_does_not_block_archive_retry(store):
    # Lease recovery can requeue coding work after merge already retired its initial queue.
    flow, job, _task, subject = await ready_archive(store)
    stale = store.enqueue_job(flow.id, JobKind.REVISION)
    await subject.fail(job, "cleanup permission denied")
    ControlService(store).retry(flow)
    retry = store.assign_job("worker-a", "retry", "epoch-a")
    assert retry.kind is JobKind.ARCHIVE and retry.id != job.id
    assert store.get_job(stale.id).state is JobState.QUEUED


@pytest.mark.parametrize("mismatch", ["worker", "token", "epoch", "delivery", "attempt", "binding"])
async def test_lost_archive_started_requires_original_fence_and_frozen_identity(store, mismatch):
    flow, job, _task, _subject = await ready_archive(store, start=False)
    worker, token, epoch = "worker-a", "archive-token", "epoch-a"
    if mismatch == "worker":
        worker = "worker-b"
        store.worker_connected(worker, PROTOCOL_VERSION, epoch)
    elif mismatch == "token":
        token = "old-token"
    elif mismatch == "epoch":
        epoch = "old-epoch"
    elif mismatch == "delivery":
        delivery = store.get_delivery(flow.id)
        delivery.version += 1
        store.update_delivery(delivery)
    elif mismatch == "attempt":
        flow.current_attempt_id = store.create_attempt(flow.id).id
        store.update_flow(flow)
    else:
        store.release_flow_execution(flow.id)
    assert not store.resume_assignment(job.id, worker, token, epoch)
    assert store.get_job(job.id).started_at is None
