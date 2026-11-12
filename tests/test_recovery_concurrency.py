"""A slow result must leave capacity for other Flows and local scheduling sweeps."""

import asyncio

import pytest
from pydantic import ValidationError

from app.contracts.protocol import AssignmentArguments, Failed
from app.contracts.result_models import ResultPending
from app.controler.config import ServerSettings
from app.controler.workflow.service import ControlService
from app.domain.models import FlowState, JobState
from app.persistence.records import required
from tests.github import RecordingGitHub
from tests.test_coordinator import coordinator
from tests.workspace_fixtures import register_worker

pytestmark = pytest.mark.usefixtures("recording_resources")


async def pending_results(store, count):
    """Persist real assigned/started worker failures, leaving application to the caller."""
    subject = coordinator(store, RecordingGitHub())
    jobs = []
    for _ in range(count):
        issue = len(store.flows.list_flows()) + 1
        flow = required(ControlService(database=store.database).on_trigger("owner/repo", issue, 42))
        worker = f"worker-{flow.id}"
        register_worker(store, worker, repo=flow.repo, epoch="epoch")
        job = required(store.workers.assign_job(worker, f"token-{flow.id}", "epoch"))
        task = await subject.prepare_for_assignment(job, worker, job.assignment_token, "epoch")
        assert isinstance(task, AssignmentArguments)
        assert store.workers.start_assignment(job.id, task.task.attempt_id, worker,
                                              job.assignment_token, "epoch")
        subject.mark_started(job, task.task, worker)
        terminal = Failed(job_id=job.id, assignment_token=job.assignment_token, error="agent failed")
        assert store.workers.record_assignment_result(
            job.id, worker, job.assignment_token, "epoch", terminal,
        ) == "pending"
        jobs.append(required(store.jobs.get_job(job.id)))
    return subject.results, jobs


async def test_slow_result_does_not_block_another_flow(store, monkeypatch):
    processor, jobs = await pending_results(store, 2)
    blocked, release, advanced = asyncio.Event(), asyncio.Event(), asyncio.Event()
    apply = processor._apply

    async def commit(job_id):
        if job_id == jobs[0].id:
            blocked.set()
            await release.wait()
        result = await apply(job_id)
        if job_id == jobs[1].id:
            advanced.set()
        return result

    monkeypatch.setattr(processor, "_apply", commit)
    recovery = asyncio.create_task(processor.recover_pending_results())
    try:
        await asyncio.wait_for(blocked.wait(), 2)
        await asyncio.wait_for(advanced.wait(), 2)
        assert not recovery.done()
        assert required(store.jobs.get_job(jobs[0].id)).result_applied_at is None
        assert required(store.jobs.get_job(jobs[1].id)).state is JobState.FAILED
    finally:
        release.set()
        await recovery


async def test_recovery_pool_is_bounded_and_cancellation_joins_all_tasks(store, monkeypatch):
    processor, jobs = await pending_results(store, 20)
    started, release = asyncio.Event(), asyncio.Event()
    active = set()
    apply = processor._apply

    async def commit(job_id):
        active.add(job_id)
        if len(active) == 2:
            started.set()
        try:
            await release.wait()
        finally:
            active.remove(job_id)
        return await apply(job_id)

    monkeypatch.setattr(processor, "_apply", commit)
    recovery = asyncio.create_task(processor.recover_pending_results(concurrency=2))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert active == {job.id for job in jobs[:2]}
    finally:
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery
    assert active == set()
    assert len(store.jobs.list_unapplied_results()) == 20


async def test_bounded_batches_advance_past_a_persistently_pending_result(store, monkeypatch):
    processor, jobs = await pending_results(store, 5)
    calls = []
    apply = processor._apply

    async def commit(job_id):
        calls.append(job_id)
        if job_id == jobs[0].id:
            raise ResultPending("waiting for a receipt")
        return await apply(job_id)

    monkeypatch.setattr(processor, "_apply", commit)
    for _ in range(4):
        await processor.recover_pending_results(batch_size=2)
    assert calls == [*(job.id for job in jobs), jobs[0].id]
    assert [job.id for job in store.jobs.list_unapplied_results()] == [jobs[0].id]


async def test_live_application_and_duplicate_sweeps_share_result_lock(store, monkeypatch):
    processor, jobs = await pending_results(store, 2)
    entered, release, advanced = asyncio.Event(), asyncio.Event(), asyncio.Event()
    apply = processor._apply
    calls = []

    async def commit(job_id):
        calls.append(job_id)
        if job_id == jobs[0].id:
            entered.set()
            await release.wait()
        result = await apply(job_id)
        if job_id == jobs[1].id:
            advanced.set()
        return result

    monkeypatch.setattr(processor, "_apply", commit)
    # Live transport and recovery can observe the same durable inbox. Blocking the
    # actual application detects duplicate application if its shared lock is removed.
    live = asyncio.create_task(processor.apply(jobs[0].id))
    await asyncio.wait_for(entered.wait(), 2)
    recovery = asyncio.create_task(processor.recover_pending_results())
    duplicate = asyncio.create_task(processor.recover_pending_results())
    try:
        await asyncio.wait_for(advanced.wait(), 2)
        assert not live.done()
    finally:
        release.set()
        await asyncio.gather(live, recovery, duplicate)
    assert calls == [job.id for job in jobs]
    assert store.jobs.list_unapplied_results() == []
    assert all(required(store.flows.get_flow(job.flow_id)).state is FlowState.BLOCKED for job in jobs)


async def test_new_arrivals_cannot_extend_a_recovery_cycle_forever(store, monkeypatch):
    processor, jobs = await pending_results(store, 2)
    calls = []

    async def pending(job_id):
        calls.append(job_id)
        raise ResultPending("still waiting")

    monkeypatch.setattr(processor, "_apply", pending)
    await processor.recover_pending_results(batch_size=1)
    # New IDs are real accepted inboxes; the existing scan must still end at its original ceiling.
    await pending_results(store, 1)
    await processor.recover_pending_results(batch_size=1)
    await pending_results(store, 1)
    await processor.recover_pending_results(batch_size=1)
    assert calls == [jobs[0].id, jobs[1].id, jobs[0].id]


async def test_recovery_continues_after_one_broken_result(store, monkeypatch):
    processor, jobs = await pending_results(store, 2)
    apply = processor._apply

    async def broken(job_id):
        if job_id == jobs[0].id:
            raise RuntimeError("broken local result")
        return await apply(job_id)

    monkeypatch.setattr(processor, "_apply", broken)
    await processor.recover_pending_results()
    assert required(store.jobs.get_job(jobs[0].id)).result_applied_at is None
    assert required(store.jobs.get_job(jobs[1].id)).result_applied_at is not None


async def test_runtime_sweeps_continue_while_result_recovery_waits(store, monkeypatch, server_runtime):
    await pending_results(store, 1)
    runtime = server_runtime(settings={"worker_sweep_interval": 0.001})
    processor = runtime.results
    entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lease, settled = asyncio.Event(), asyncio.Event()
    apply = processor._apply

    async def commit(job_id):
        entered.set()
        try:
            await release.wait()
            return await apply(job_id)
        finally:
            exited.set()

    monkeypatch.setattr(processor, "_apply", commit)
    claim, sweep = runtime.reviews.claim_due, runtime.workers.sweep_expired_assignments

    def claim_due():
        result = claim()
        settled.set()
        return result

    def sweep_expired():
        result = sweep()
        lease.set()
        return result

    monkeypatch.setattr(runtime.reviews, "claim_due", claim_due)
    monkeypatch.setattr(runtime.workers, "sweep_expired_assignments", sweep_expired)
    loops = [runtime._sweep_results, runtime._sweep_assignments, runtime._sweep_reviews]
    runtime._tasks = [asyncio.create_task(loop()) for loop in loops]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(lease.wait(), 2)
        await asyncio.wait_for(settled.wait(), 2)
        assert not exited.is_set()
    finally:
        await runtime.shutdown()
    assert exited.is_set() and all(task.done() for task in runtime._tasks)
    assert runtime.access.client.is_closed


@pytest.mark.parametrize("failure", ["communication", "access"])
async def test_shutdown_releases_resources_even_when_connection_cleanup_fails(failure, server_runtime, monkeypatch):
    # These are resource-close fault injections, not alternate persistence behavior.
    runtime = server_runtime()
    order = []
    seal, stop = runtime.gateway.seal, runtime.reconciler.stop
    close_gateway, close_access = runtime.gateway.close, runtime.access.close
    close_database = runtime.database.close

    def seal_gateway():
        order.append("seal")
        seal()

    def stop_sweeps():
        order.append("sweeps")
        stop()

    async def close_communication(code):
        assert code == 1012
        order.append("communication")
        await close_gateway(code)
        if failure == "communication":
            raise OSError("communication failed")

    async def close_owner_access():
        order.append("access")
        await close_access()
        if failure == "access":
            raise OSError("access failed")

    def close_pool():
        order.append("database")
        close_database()

    with monkeypatch.context() as patch:
        patch.setattr(runtime.gateway, "seal", seal_gateway)
        patch.setattr(runtime.reconciler, "stop", stop_sweeps)
        patch.setattr(runtime.gateway, "close", close_communication)
        patch.setattr(runtime.access, "close", close_owner_access)
        patch.setattr(runtime.database, "close", close_pool)
        with pytest.raises(OSError, match=failure):
            await runtime.shutdown()
    # Gateway.close seals again itself before joining its owned connections.
    assert order == ["seal", "sweeps", "communication", "seal", "access", "database"]


async def test_store_pages_only_unapplied_results_in_stable_job_order(store):
    processor, jobs = await pending_results(store, 5)
    first = store.jobs.list_unapplied_results(limit=2)
    assert store.jobs.unapplied_result_ceiling() == jobs[-1].id
    assert [j.id for j in store.jobs.list_unapplied_results(through_id=jobs[1].id)] == [
        j.id for j in jobs[:2]
    ]
    assert [j.id for j in first] == [j.id for j in jobs[:2]]
    await processor.apply(jobs[2].id)
    assert [j.id for j in store.jobs.list_unapplied_results(after_id=first[-1].id, limit=2)] == [
        jobs[3].id, jobs[4].id,
    ]


def test_recovery_limits_are_validated():
    with pytest.raises(ValidationError):
        ServerSettings(
            _env_file=None, database_url="postgresql://localhost/test",
            worker_tokens={"worker": "secret"}, result_recovery_concurrency=0,
        )
