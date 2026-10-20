"""Planned restarts pause admissions without ending long-lived Flows."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.contracts.protocol import PROTOCOL_VERSION
from app.controler.runtime import ServerRuntime
from app.domain.models import FlowState, JobKind
from app.persistence.maintenance import MaintenanceStore
from app.persistence.workers import WorkerStore


def idle_binding(store):
    flow = store.flows.create_flow("owner/repo", 1, 1)
    store.workers.worker_connected("w", PROTOCOL_VERSION, "epoch")
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("w", "initial", "epoch")
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET state='done' WHERE id=%s", (job.id,))
        connection.execute("UPDATE workers SET current_job_id=NULL,status='online'")
    return flow


def test_pause_blocks_bound_followups_and_new_workers_without_changing_drain(store):
    # Drain intentionally permits revisions; maintenance must close that admission path too.
    flow = idle_binding(store)
    store.workers.drain("w", True)
    revision = store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    maintenance = MaintenanceStore(store.database)
    maintenance.start_instance()
    maintenance.pause("update")
    assert store.workers.assign_job("w", "revision", "epoch") is None
    store.workers.worker_connected("added", PROTOCOL_VERSION, "new")
    other = store.flows.create_flow("owner/other", 2, 1)
    store.jobs.enqueue_job(other.id, JobKind.INITIAL)
    assert store.workers.assign_job("added", "initial", "new") is None
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.workers.get_flow_execution(flow.id).released_at is None
    maintenance.resume("update")
    assert store.workers.assign_job("added", "initial", "new") is not None
    assert store.workers.assign_job("w", "revision", "epoch").id == revision.id
    assert next(w for w in store.workers.list_workers() if w.worker_id == "w").draining


def test_pause_waits_for_assignment_transaction_and_keeps_unstarted_work_visible(store):
    store.workers.worker_connected("w", PROTOCOL_VERSION, "epoch")
    flow = store.flows.create_flow("owner/repo", 1, 1)
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    maintenance = MaintenanceStore(store.database)
    entered = Event()

    def pause():
        entered.set()
        maintenance.pause("update")

    # The claim has not committed yet. A concurrent pause cannot overtake it and
    # report idle while preparation/dispatch is about to start.
    with ThreadPoolExecutor() as executor:
        with store.transaction() as tx:
            assert WorkerStore(tx.database).assign_job("w", "claimed", "epoch")
            future = executor.submit(pause)
            assert entered.wait(3)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
        future.result(timeout=3)
    claimed = store.jobs.get_job(job.id)
    assert claimed.state == "running" and claimed.started_at is None
    assert maintenance.status().operation_id == "update"


def test_pause_waits_for_admitted_controller_work_and_survives_restart(store):
    maintenance = MaintenanceStore(store.database)
    first = maintenance.start_instance()
    with maintenance.activity("triage") as admitted:
        assert admitted
        maintenance.pause("update")
        assert maintenance.active() == ["triage"]
        with maintenance.activity("webhooks") as another:
            assert not another
        assert maintenance.active() == ["triage"]
    assert maintenance.active() == []
    second = maintenance.start_instance()
    assert second != first
    assert maintenance.status().operation_id == "update"
    with pytest.raises(ValueError, match="owned"):
        maintenance.resume("another-update")
    maintenance.resume("update")
    with maintenance.activity("webhooks") as admitted:
        assert admitted


async def test_pause_drains_current_triage_without_admitting_another_batch(store):
    maintenance = MaintenanceStore(store.database)
    entered, release = asyncio.Event(), asyncio.Event()

    async def triage(*args):
        entered.set()
        await release.wait()

    runtime = ServerRuntime.__new__(ServerRuntime)
    runtime.maintenance = maintenance
    runtime.reviews = SimpleNamespace(pending_triage=Mock(return_value=[object()]))
    runtime.coordinator = SimpleNamespace(triage=AsyncMock(side_effect=triage))
    runtime.judge = Mock()
    runtime.gateway = SimpleNamespace(notify_jobs=Mock())
    task = asyncio.create_task(runtime._run_triage())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        maintenance.pause("update")
        assert maintenance.active() == ["triage"]
        await runtime._run_triage()
        assert runtime.coordinator.triage.await_count == 1
    finally:
        release.set()
        await task
    assert maintenance.active() == []


def test_maintenance_preserves_idle_binding_past_restart_until_normal_reconnect_grace(store):
    flow = idle_binding(store)
    maintenance = MaintenanceStore(store.database)
    maintenance.pause("update")
    store.workers.worker_disconnected("w", "restart", "epoch")
    store.workers.recover_abandoned_assignments()
    store._db._clock = lambda: flow.created_at + timedelta(days=1)
    assert store.workers.sweep_expired_assignments() == (0, 0)
    assert store.workers.get_flow_execution(flow.id).released_at is None
    maintenance.resume("update")
    assert store.workers.sweep_expired_assignments() == (0, 0)
    store.workers.worker_connected("w", PROTOCOL_VERSION, "reconnected")
    revision = store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    assert store.workers.assign_job("w", "followup", "reconnected").id == revision.id
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
