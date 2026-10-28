"""A slow result must leave capacity for other Flows and local scheduling sweeps."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.communication.gateway import Gateway
from app.contracts.protocol import InitialTaskResult, Succeeded
from app.contracts.result_models import ResultPending
from app.controler.config import ServerSettings
from app.controler.runtime import ServerRuntime
from app.domain.models import JobKind, JobState
from tests.test_worker_handler import RecordingCoordinator, RecordingStore
from tests.worker_fakes import result_application


class Inbox(RecordingStore):
    def __init__(self, count):
        super().__init__()
        self.job_records = {}
        self.pages = []
        for i in range(1, count + 1):
            message = Succeeded(job_id=i, assignment_token="token", connection_epoch="epoch",
                result=InitialTaskResult(session_id="session", last_message="done", exit_code=0))
            self.job_records[i] = self.job.model_copy(update={
                "id": i, "flow_id": i, "state": JobState.RUNNING,
                "result_payload": message.model_dump(mode="json"),
            })

    def get_job(self, job_id):
        return self.job_records.get(job_id)

    def unapplied_result_ceiling(self):
        return max((j.id for j in self.job_records.values() if j.result_applied_at is None), default=0)

    def list_unapplied_results(self, *, after_id=0, through_id=None, limit=64):
        self.pages.append((after_id, limit))
        return [j for j in self.job_records.values()
                if j.id > after_id and (through_id is None or j.id <= through_id)
                and j.result_applied_at is None][:limit]


async def test_slow_result_does_not_block_another_flow():
    blocked, release, advanced = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Coordinator(RecordingCoordinator):
        async def _commit_result(self, job_id):
            if job_id == 1:
                blocked.set()
                await release.wait()
            else:
                advanced.set()
            return await super()._commit_result(job_id)

    inbox = Inbox(2)
    processor = result_application(inbox, Coordinator(inbox)._commit_result)
    recovery = asyncio.create_task(processor.recover_pending_results())
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
        async def _commit_result(self, job_id):
            active.add(job_id)
            if len(active) == 2:
                started.set()
            try:
                await release.wait()
            finally:
                active.remove(job_id)
            return await super()._commit_result(job_id)

    inbox = Inbox(20)
    processor = result_application(inbox, Coordinator(inbox)._commit_result)
    recovery = asyncio.create_task(processor.recover_pending_results(concurrency=2))
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
        async def _commit_result(self, job_id):
            calls.append(job_id)
            if job_id == 1:
                raise ResultPending("waiting for a receipt")
            return await super()._commit_result(job_id)

    inbox = Inbox(5)
    processor = result_application(inbox, Coordinator(inbox)._commit_result)
    for _ in range(4):
        await processor.recover_pending_results(batch_size=2)
    assert calls == [1, 2, 3, 4, 5, 1]
    assert all(limit == 2 for _, limit in inbox.pages)


async def test_live_result_holds_the_same_job_lock_as_recovery():
    entered, release, advanced = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Coordinator(RecordingCoordinator):
        async def _commit_result(self, job_id):
            if job_id == 1:
                entered.set()
                await release.wait()
            else:
                advanced.set()
            return await super()._commit_result(job_id)

    inbox = Inbox(2)
    coordinator = Coordinator(inbox)
    processor = result_application(inbox, coordinator._commit_result)
    # Live transport and recovery can observe the same accepted inbox payload.
    # Hold the real application, so removing the shared lock duplicates its commit.
    live = asyncio.create_task(processor.apply(1))
    await asyncio.wait_for(entered.wait(), 1)
    recovery = asyncio.create_task(processor.recover_pending_results())
    duplicate_sweep = asyncio.create_task(processor.recover_pending_results())
    try:
        await asyncio.wait_for(advanced.wait(), 1)
        assert not live.done()
    finally:
        release.set()
        await asyncio.gather(live, recovery, duplicate_sweep)
    assert len(coordinator.received_results) == 2


async def test_new_arrivals_cannot_extend_a_recovery_cycle_forever():
    calls = []

    class Coordinator(RecordingCoordinator):
        async def _commit_result(self, job_id):
            calls.append(job_id)
            raise ResultPending("still waiting")

    inbox = Inbox(2)
    processor = result_application(inbox, Coordinator(inbox)._commit_result)
    await processor.recover_pending_results(batch_size=1)
    # A busy service keeps producing new IDs; the initial scan must still end at ID 2.
    inbox.job_records[3] = Inbox(3).job_records[3]
    await processor.recover_pending_results(batch_size=1)
    inbox.job_records[4] = Inbox(4).job_records[4]
    await processor.recover_pending_results(batch_size=1)
    assert calls == [1, 2, 1]


async def test_runtime_sweeps_continue_while_result_recovery_waits():
    entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lease, settled = asyncio.Event(), asyncio.Event()

    async def recover(**options):
        entered.set()
        try:
            await release.wait()
        finally:
            exited.set()

    runtime = ServerRuntime.__new__(ServerRuntime)
    runtime._triage_task = None
    runtime.coordinator = SimpleNamespace(triage=AsyncMock())
    runtime.judge = Mock()
    runtime.settings = SimpleNamespace(worker_sweep_interval=0, result_recovery_concurrency=4, result_recovery_batch_size=64)
    runtime.reviews = SimpleNamespace(claim_due=Mock(side_effect=lambda: (settled.set() or True)),
                                     pending_triage=Mock(return_value=[]))
    runtime.workers = SimpleNamespace(sweep_expired_assignments=Mock(side_effect=lambda: (lease.set() or 0, 0)))
    runtime.database = SimpleNamespace(close=Mock())
    runtime.access = SimpleNamespace(close=AsyncMock())
    runtime.results = SimpleNamespace(recover_pending_results=AsyncMock(side_effect=recover))
    runtime.gateway = Gateway()
    runtime.worker_handler = SimpleNamespace(notify_jobs=Mock())
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
    runtime.access.close.assert_awaited_once()
    runtime.database.close.assert_called_once()


@pytest.mark.parametrize("failure", ["communication", "access"])
async def test_shutdown_releases_resources_even_when_connection_cleanup_fails(failure):
    # Transport failure cannot skip access cleanup or leave the DB pool open.
    runtime = ServerRuntime.__new__(ServerRuntime)
    runtime._tasks, runtime._triage_task = [], None
    order = []

    async def close_communication(code):
        assert code == 1012
        order.append("communication")
        if failure == "communication":
            raise OSError("communication failed")

    async def close_access():
        order.append("access")
        if failure == "access":
            raise OSError("access failed")

    runtime.gateway = Mock(spec=Gateway, seal=Mock(side_effect=lambda: order.append("seal")),
                           close=AsyncMock(side_effect=close_communication))
    runtime.reconciler = SimpleNamespace(stop=lambda: order.append("sweeps"))
    runtime.access = SimpleNamespace(close=close_access)
    runtime.database = SimpleNamespace(close=lambda: order.append("database"))
    with pytest.raises(OSError, match=failure):
        await runtime.shutdown()
    assert order == ["seal", "sweeps", "communication", "access", "database"]


def test_store_pages_only_unapplied_results_in_stable_job_order(store):
    jobs = []
    for i in range(5):
        flow = store.flows.create_flow("owner/repo", i + 1, 42)
        job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
        jobs.append(job)
        with store.connection() as conn:
            conn.execute("UPDATE jobs SET state='running', result_received_at=%s WHERE id=%s",
                         (store.now(), job.id))
    first = store.jobs.list_unapplied_results(limit=2)
    assert store.jobs.unapplied_result_ceiling() == jobs[-1].id
    assert [j.id for j in store.jobs.list_unapplied_results(through_id=jobs[1].id)] == [
        j.id for j in jobs[:2]
    ]
    assert [j.id for j in first] == [j.id for j in jobs[:2]]
    store.jobs.complete_persisted_result(jobs[2].id, JobState.DONE)
    assert [j.id for j in store.jobs.list_unapplied_results(after_id=first[-1].id, limit=2)] == [
        jobs[3].id, jobs[4].id,
    ]


def test_recovery_limits_are_validated():
    with pytest.raises(ValidationError):
        ServerSettings(_env_file=None, database_url="postgresql://localhost/test",
                      worker_tokens={"worker": "secret"}, result_recovery_concurrency=0)
