"""PostgreSQL merge archival, original stop fences, and atomic resource release."""

from datetime import timedelta

import pytest

from app.contracts.protocol import ExecutionStopped, RecoveryDescriptor
from app.controler.workflow.service import ControlService
from app.domain.models import ArchiveReason, FlowState, JobKind, JobState
from app.persistence.activity import ActivityStore
from app.persistence.workers import WorkerStore
from tests.test_merge import merge_payload, route_merge
from tests.test_workflow_transactions import accepted_coding
from tests.worker_fixtures import server_callbacks
from tests.workspace_fixtures import register_worker


async def test_archive_release_rolls_back_and_replays_atomically(store, monkeypatch):
    # A failed stop transaction must not publish a reusable controller generation.
    job, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(job.flow_id)
    await route_merge(store, flow)
    archived = store.flows.get_flow(flow.id)
    activity = ActivityStore(store.database).archived(flow.id, 1)
    stop = store.stops.pending("worker-a")
    receipt = ExecutionStopped(**stop.fence().model_dump(), execution_nonce="original-executor")
    original = WorkerStore.release_flow_execution

    def interrupted(self, flow_id):
        assert original(self, flow_id)
        raise RuntimeError("commit interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(WorkerStore, "release_flow_execution", interrupted)
        with pytest.raises(RuntimeError, match="commit interrupted"):
            store.stops.confirm("worker-a", receipt, "epoch-a")
    assert store.stops.pending("worker-a") == stop
    assert store.workers.get_flow_execution(flow.id).released_at is None
    current = next(w for w in store.workers.list_workspaces("worker-a") if w.workspace == job.workspace)
    assert (current.current_job_id, current.generation) == (job.id, job.generation)
    other = ControlService(database=store.database).on_trigger("owner/repo", 999, 42)
    assert store.workers.assign_job("worker-a", "next", "epoch-a") is None

    assert store.stops.confirm("worker-a", receipt, "epoch-a")
    assert store.stops.confirm("worker-a", receipt, "epoch-a")
    assert store.flows.get_flow(flow.id) == archived
    assert ActivityStore(store.database).archived(flow.id, 1) == activity
    assert store.workers.get_flow_execution(flow.id).released_at is not None
    assert flow.id not in [item.id for item in store.flows.list_flows(active_only=True)]
    assert store.workers.assign_job("worker-a", "next", "epoch-a").flow_id == other.id


async def test_archive_fences_do_not_release_binding(store):
    job, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(job.flow_id)
    await route_merge(store, flow)
    stop = store.stops.pending("worker-a")
    receipt = ExecutionStopped(**stop.fence().model_dump(), execution_nonce="original-executor")
    for worker, evidence, epoch in [
        ("worker-b", receipt, "epoch-a"),
        ("worker-a", receipt.model_copy(update={"assignment_token": "old"}), "epoch-a"),
        ("worker-a", receipt, "old"),
    ]:
        assert not store.stops.confirm(worker, evidence, epoch)
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert store.stops.confirm("worker-a", receipt, "epoch-a")
    assert store.workers.get_flow_execution(flow.id).released_at is not None


async def test_merge_after_abandonment_cannot_adopt_a_released_binding(
    store, completed_open_initial
):
    # A historical merge observation cannot resurrect the archived round's workspace.
    flow, _subject, _github, _request, _result = completed_open_initial
    service = ControlService(database=store.database)
    service.cancel(flow)
    stop = store.stops.pending("worker-a")
    assert store.stops.confirm(
        "worker-a", ExecutionStopped(**stop.fence().model_dump(), execution_nonce="owned"), "epoch",
    )
    with pytest.raises(ValueError, match="archived"):
        service.retry(flow)
    activity = ActivityStore(store.database)
    archived = activity.archived(flow.id, 1)
    assert archived is not None and archived.metadata["archive_reason"] == "abandoned"
    payload = merge_payload(flow)
    payload["pull_request"]["merged_at"] = (
        archived.occurred_at - timedelta(seconds=5)
    ).isoformat()
    await route_merge(store, flow, payload=payload)
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.flows.get_flow(flow.id).archive_reason is ArchiveReason.SUCCEEDED
    assert activity.archived(flow.id, 1) == archived
    history = activity.history(flow.id, 1)
    merges = [item for item in history if item.kind == "merge"]
    assert len(merges) == 1
    assert merges[0].metadata["merge_commit_sha"] == "a" * 40
    assert len([item for item in history if item.kind == "archive_correction"]) == 1
    assert store.workers.get_flow_execution(flow.id).released_at is not None
    assert [job for job in store.jobs.list_jobs(flow.id) if job.kind is JobKind.ARCHIVE] == []
    register_worker(store, "worker-b", repo=flow.repo, epoch="epoch-b")
    assert store.workers.assign_job("worker-b", "adopt", "epoch-b") is None
    assert store.workers.assign_job("worker-a", "archive", "epoch") is None


async def test_expired_merged_executor_is_pinned_until_original_worker_returns(store):
    old, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(old.flow_id)
    await route_merge(store, flow)
    store.workers.worker_disconnected("worker-a", connection_epoch="epoch-a")
    now = store.now()
    store._db._clock = lambda: now + timedelta(hours=1)
    store.workers.sweep_expired_assignments()
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.flows.get_flow(flow.id).archive_reason is ArchiveReason.SUCCEEDED
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert store.jobs.get_job(old.id).state is JobState.RUNNING
    assert "executor" in store.jobs.get_job(old.id).last_error
    with pytest.raises(ValueError, match="archived"):
        ControlService(database=store.database).retry(flow)
    register_worker(store, "worker-b", repo=flow.repo, epoch="epoch-b")
    assert store.workers.assign_job("worker-b", "other", "epoch-b") is None
    register_worker(store, "worker-a", repo=flow.repo, epoch="new-epoch")
    assert not store.workers.resume_assignment(old.id, "worker-a", "triage-token", "new-epoch")


@pytest.mark.parametrize("journal", ["missing", "started-without-executor"])
async def test_merged_coding_process_without_exit_proof_cannot_unlock_archive(store, journal, server_runtime):
    old, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(old.flow_id)
    await route_merge(store, flow)
    runtime = server_runtime()
    handler = server_callbacks(runtime, "worker-a", "restart")
    register_worker(store, "worker-a", repo=flow.repo, epoch="restart")
    if journal == "missing":
        await handler._reject_missing_journal(old.id)
    else:
        assert await handler._recover(RecoveryDescriptor(
            job_id=old.id, assignment_token=old.assignment_token,
            state="started", executor_active=False,
        )) is None
    assert store.jobs.get_job(old.id).state is JobState.RUNNING
    assert store.jobs.get_job(old.id).result_received_at is None
    assert store.workers.get_flow_execution(flow.id).released_at is None
