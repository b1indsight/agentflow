import asyncio
from collections.abc import Callable
from unittest.mock import Mock

import pytest

from app.models import Job, JobKind, JobState
from app.store import Store
from app.worker import Worker


class RecordingEngine:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.jobs: list[Job] = []
        self.stop_worker: Callable[[], None] | None = None

    async def run_job(self, job: Job) -> None:
        self.jobs.append(job)
        await asyncio.sleep(0)
        if self.stop_worker is not None:
            self.stop_worker()
        if self.error is not None:
            raise self.error


def enqueue_job(store: Store) -> Job:
    flow = store.create_flow("owner/repo", 21, installation_id=None)
    return store.enqueue_job(flow.id, JobKind.INITIAL)


@pytest.mark.asyncio
async def test_dispatch_finishes_job_and_cleans_up_heartbeat(
    store: Store, monkeypatch
) -> None:
    queued = enqueue_job(store)
    claimed = store.claim_job()
    assert claimed is not None
    engine = RecordingEngine()
    worker = Worker(store, engine)
    heartbeat_finished = asyncio.Event()

    async def heartbeat(job_id: int) -> None:
        assert job_id == claimed.id
        try:
            await asyncio.Event().wait()
        finally:
            heartbeat_finished.set()

    monkeypatch.setattr(worker, "_heartbeat", heartbeat)

    await worker._dispatch(claimed)

    finished = store.get_job(queued.id)
    assert finished is not None
    assert finished.state is JobState.DONE
    assert heartbeat_finished.is_set()


@pytest.mark.asyncio
async def test_dispatch_contains_heartbeat_task_failures(
    store: Store, monkeypatch
) -> None:
    queued = enqueue_job(store)
    claimed = store.claim_job()
    assert claimed is not None
    worker = Worker(store, RecordingEngine())

    async def failing_heartbeat(job_id: int) -> None:
        # A transient persistence failure must not terminate the worker loop.
        assert job_id == claimed.id
        raise RuntimeError("heartbeat failed")

    monkeypatch.setattr(worker, "_heartbeat", failing_heartbeat)

    await worker._dispatch(claimed)

    finished = store.get_job(queued.id)
    assert finished is not None
    assert finished.state is JobState.DONE


@pytest.mark.asyncio
async def test_heartbeat_retries_after_transient_store_failure(
    store: Store, monkeypatch
) -> None:
    worker = Worker(store, RecordingEngine())
    heartbeat = Mock(side_effect=[RuntimeError("database busy"), None])
    sleep_calls = 0

    async def stop_after_retry(interval: float) -> None:
        # Model one failed lease renewal followed by a successful retry.
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(store, "heartbeat", heartbeat)
    monkeypatch.setattr(asyncio, "sleep", stop_after_retry)

    await worker._heartbeat(42)

    assert heartbeat.call_count == 2
    heartbeat.assert_called_with(42)


@pytest.mark.asyncio
async def test_dispatch_marks_failed_job(store: Store) -> None:
    queued = enqueue_job(store)
    claimed = store.claim_job()
    assert claimed is not None
    engine = RecordingEngine(error=RuntimeError("agent failed"))
    worker = Worker(store, engine)

    await worker._dispatch(claimed)

    failed = store.get_job(queued.id)
    assert failed is not None
    assert failed.state is JobState.FAILED


@pytest.mark.asyncio
async def test_run_recovers_before_consuming_jobs(store: Store, monkeypatch) -> None:
    queued = enqueue_job(store)
    recover = Mock(wraps=store.recover_stale_jobs)
    monkeypatch.setattr(store, "recover_stale_jobs", recover)
    engine = RecordingEngine()
    worker = Worker(store, engine)
    engine.stop_worker = worker.stop

    await asyncio.wait_for(worker.run(), timeout=1)

    recover.assert_called_once_with()
    assert [job.id for job in engine.jobs] == [queued.id]
    finished = store.get_job(queued.id)
    assert finished is not None
    assert finished.state is JobState.DONE
