"""A slow result must leave capacity for other Flows and local scheduling sweeps."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.contracts.protocol import InitialResult, Succeeded
from app.contracts.result_models import ResultPending
from app.control.config import ServeSettings
from app.control.gateway import WorkerGateway
from app.control.runtime import ControlRuntime
from app.domain.models import JobKind, JobState
from tests.test_gateway import RecordingCoordinator, RecordingStore
from tests.worker_fakes import NoPendingStops


class Inbox(RecordingStore):
    def __init__(self, count):
        super().__init__()
        self.jobs = {}
        self.pages = []
        for i in range(1, count + 1):
            message = Succeeded(job_id=i, assignment_token="token", connection_epoch="epoch",
                result=InitialResult(session_id="session", last_message="done", exit_code=0))
            self.jobs[i] = self.job.model_copy(update={
                "id": i, "flow_id": i, "state": JobState.RUNNING,
                "result_payload": message.model_dump(mode="json"),
            })

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def unapplied_result_ceiling(self):
        return max((j.id for j in self.jobs.values() if j.result_applied_at is None), default=0)

    def list_unapplied_results(self, *, after_id=0, through_id=None, limit=64):
        self.pages.append((after_id, limit))
        return [j for j in self.jobs.values()
                if j.id > after_id and (through_id is None or j.id <= through_id)
                and j.result_applied_at is None][:limit]


async def test_slow_result_does_not_block_another_flow():
    blocked, release, advanced = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Coordinator(RecordingCoordinator):
        async def apply_result(self, job_id):
            if job_id == 1:
                blocked.set()
                await release.wait()
            else:
                advanced.set()
            return await super().apply_result(job_id)

    inbox = Inbox(2)
    gateway = WorkerGateway(inbox, Coordinator(inbox), execution_control=NoPendingStops(), worker_tokens={})
    recovery = asyncio.create_task(gateway.recover_pending_results())
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        await asyncio.wait_for(advanced.wait(), 1)
        assert not recovery.done()
    finally:
        release.set()
        await recovery


async def test_recovery_pool_is_bounded_and_cancellation_joins_all_tasks():
    started, release = asyncio.Event(), asyncio.Event()
    active = set()

    class Coordinator(RecordingCoordinator):
        async def apply_result(self, job_id):
            active.add(job_id)
            if len(active) == 2:
                started.set()
            try:
                await release.wait()
            finally:
                active.remove(job_id)
            return await super().apply_result(job_id)

    inbox = Inbox(20)
    gateway = WorkerGateway(inbox, Coordinator(inbox), execution_control=NoPendingStops(), worker_tokens={}, recovery_concurrency=2)
    recovery = asyncio.create_task(gateway.recover_pending_results())
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert active == {1, 2}
    finally:
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery
    assert active == set()


async def test_bounded_batches_advance_past_a_persistently_pending_result():
    calls = []

    class Coordinator(RecordingCoordinator):
        async def apply_result(self, job_id):
            calls.append(job_id)
            if job_id == 1:
                raise ResultPending("waiting for a receipt")
            return await super().apply_result(job_id)

    inbox = Inbox(5)
    gateway = WorkerGateway(inbox, Coordinator(inbox), execution_control=NoPendingStops(), worker_tokens={}, recovery_batch_size=2)
    for _ in range(4):
        await gateway.recover_pending_results()
    assert calls == [1, 2, 3, 4, 5, 1]
    assert all(limit == 2 for _, limit in inbox.pages)


async def test_live_result_holds_the_same_job_lock_as_recovery():
    advanced = asyncio.Event()

    class Coordinator(RecordingCoordinator):
        async def apply_result(self, job_id):
            if job_id == 2:
                advanced.set()
            return await super().apply_result(job_id)

    inbox = Inbox(2)
    coordinator = Coordinator(inbox)
    gateway = WorkerGateway(inbox, coordinator, execution_control=NoPendingStops(), worker_tokens={})
    lock = gateway._result_lock(1)
    await lock.acquire()
    recovery = asyncio.create_task(gateway.recover_pending_results())
    duplicate_sweep = asyncio.create_task(gateway.recover_pending_results())
    try:
        await asyncio.wait_for(advanced.wait(), 1)
        # A live WebSocket handler commits before releasing the shared job lock.
        await coordinator.apply_result(1)
    finally:
        lock.release()
        await asyncio.gather(recovery, duplicate_sweep)
    assert len(coordinator.results) == 2


async def test_new_arrivals_cannot_extend_a_recovery_cycle_forever():
    calls = []

    class Coordinator(RecordingCoordinator):
        async def apply_result(self, job_id):
            calls.append(job_id)
            raise ResultPending("still waiting")

    inbox = Inbox(2)
    gateway = WorkerGateway(inbox, Coordinator(inbox), execution_control=NoPendingStops(), worker_tokens={}, recovery_batch_size=1)
    await gateway.recover_pending_results()
    # A busy service keeps producing new IDs; the initial scan must still end at ID 2.
    inbox.jobs[3] = Inbox(3).jobs[3]
    await gateway.recover_pending_results()
    inbox.jobs[4] = Inbox(4).jobs[4]
    await gateway.recover_pending_results()
    assert calls == [1, 2, 1]


async def test_runtime_sweeps_continue_while_result_recovery_waits():
    entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lease, settled = asyncio.Event(), asyncio.Event()

    async def recover():
        entered.set()
        try:
            await release.wait()
        finally:
            exited.set()

    runtime = ControlRuntime.__new__(ControlRuntime)
    runtime.settings = SimpleNamespace(worker_sweep_interval=0)
    runtime.store = SimpleNamespace(
        reviews=SimpleNamespace(claim_due=Mock(side_effect=lambda: (settled.set() or True))),
        sweep_expired_assignments=Mock(side_effect=lambda: (lease.set() or 0, 0)), close=Mock(),
    )
    runtime.gateway = SimpleNamespace(recover_pending_results=AsyncMock(side_effect=recover),
                                     notify_jobs=Mock())
    runtime.reconciler = SimpleNamespace(stop=Mock())
    loops = [runtime._sweep_results, runtime._sweep_assignments, runtime._sweep_reviews]
    runtime._tasks = [asyncio.create_task(loop()) for loop in loops]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(lease.wait(), 1)
        await asyncio.wait_for(settled.wait(), 1)
        assert not exited.is_set()
    finally:
        await runtime.shutdown()
    assert exited.is_set() and all(task.done() for task in runtime._tasks)
    runtime.store.close.assert_called_once()


def test_store_pages_only_unapplied_results_in_stable_job_order(store):
    jobs = []
    for i in range(5):
        flow = store.create_flow("owner/repo", i + 1, 42)
        job = store.enqueue_job(flow.id, JobKind.INITIAL)
        jobs.append(job)
        with store.connection() as conn:
            conn.execute("UPDATE jobs SET state='running', result_received_at=%s WHERE id=%s",
                         (store.now(), job.id))
    first = store.list_unapplied_results(limit=2)
    assert store.unapplied_result_ceiling() == jobs[-1].id
    assert [j.id for j in store.list_unapplied_results(through_id=jobs[1].id)] == [
        j.id for j in jobs[:2]
    ]
    assert [j.id for j in first] == [j.id for j in jobs[:2]]
    store.complete_persisted_result(jobs[2].id, JobState.DONE)
    assert [j.id for j in store.list_unapplied_results(after_id=first[-1].id, limit=2)] == [
        jobs[3].id, jobs[4].id,
    ]


@pytest.mark.parametrize("config", [
    {"result_recovery_concurrency": 0}, {"result_recovery_concurrency": 65},
    {"result_recovery_batch_size": 0}, {"result_recovery_batch_size": 1001},
])
def test_recovery_limits_are_validated(config):
    with pytest.raises(ValidationError):
        ServeSettings(_env_file=None, database_url="postgresql://localhost/test",
                      worker_tokens={"worker": "secret"}, **config)
