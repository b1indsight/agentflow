"""Concrete inventory ownership, concurrency, and terminal ACK fences on PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest

from app.contracts.protocol import PROTOCOL_VERSION, ExecutionStopped, Failed
from app.controler.workflow.service import ControlService
from app.domain.inference_models import (
    LOCAL_BUB_WORKER_ID,
    InferenceCapability,
    InferenceMessage,
    InferenceTask,
)
from app.domain.models import FlowState, Job, JobKind, JobState, WorkspaceInventory
from app.persistence.records import required
from app.persistence.store import Store


def identity(name: str) -> str:
    return uuid5(NAMESPACE_URL, name).hex


def connect(store: Store, worker: str, limit: int = 2, epoch: str = "epoch") -> None:
    store.workers.worker_connected(
        worker, PROTOCOL_VERSION, epoch, workspace=identity(worker + ":default"), repo_workspace_limit=limit,
    )
    assert store.workers.report_workspace(worker, epoch, WorkspaceInventory(
        workspace=identity(worker + ":default"), repository=None, generation=1, state="idle", flow_id=None,
    ))


def inventory(worker: str, index: int = 0, repo: str = "owner/repo", **changes) -> WorkspaceInventory:
    return WorkspaceInventory.model_validate({
        "workspace": identity(f"{worker}:{repo}:{index}"), "repository": repo,
        "generation": 1, "state": "idle", "flow_id": None, **changes,
    })


def provision(store: Store, worker: str, index: int = 0, repo: str = "owner/repo", epoch: str = "epoch") -> WorkspaceInventory:
    item = inventory(worker, index, repo)
    assert store.workers.report_workspace(worker, epoch, item)
    return item


def queued(store: Store, issue: int = 1, repo: str = "owner/repo") -> Job:
    flow = store.flows.create_flow(repo, issue, None)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    return store.jobs.enqueue_job(flow.id, JobKind.INITIAL)


def claim(store: Store, worker: str = "a", token: str = "token", epoch: str = "epoch") -> Job:
    job = store.workers.assign_job(worker, token, epoch)
    assert job is not None
    return job


def settle(store: Store, job: Job, *, state: Literal["idle", "releasing"] = "idle") -> bool:
    assert job.workspace is not None and job.generation is not None and job.assigned_worker_id is not None
    return store.workers.report_workspace(job.assigned_worker_id, "epoch", WorkspaceInventory(
        workspace=job.workspace, repository="owner/repo", generation=job.generation,
        state=state, flow_id=job.flow_id if state == "idle" else None,
        settled_job_id=job.id, settled_assignment_token=job.assignment_token,
    ))


def finish(store: Store, job: Job) -> None:
    assert job.assigned_worker_id is not None and job.assignment_token is not None
    terminal = Failed(job_id=job.id, assignment_token=job.assignment_token, error="test outcome")
    assert store.workers.record_assignment_result(job.id, job.assigned_worker_id, job.assignment_token, "epoch", terminal) == "pending"
    assert store.jobs.complete_persisted_result(job.id, JobState.FAILED)


def test_demand_never_claims_without_registered_inventory(store: Store) -> None:
    connect(store, "a", 3)
    job = queued(store)
    assert store.workers.workspace_demand("a", "epoch") == ["owner/repo"]
    assert store.workers.assign_job("a", "none", "epoch") is None
    assert required(store.jobs.get_job(job.id)).state is JobState.QUEUED
    assert store.workers.get_flow_execution(job.required_flow_id) is None
    item = provision(store, "a")
    assigned = claim(store)
    assert (assigned.workspace, assigned.generation) == (item.workspace, item.generation)
    assert required(store.workers.get_flow_execution(job.required_flow_id)).workspace == item.workspace


def test_worker_specific_limits_and_cross_repository_capacity(store: Store) -> None:
    for worker, limit in (("a", 3), ("b", 2)):
        connect(store, worker, limit)
        for index in range(limit):
            provision(store, worker, index)
        assert not store.workers.report_workspace(worker, "epoch", inventory(worker, limit))
    for issue in range(6):
        queued(store, issue)
    a = [claim(store, "a", f"a-{i}") for i in range(3)]
    b = [claim(store, "b", f"b-{i}") for i in range(2)]
    assert len({(job.assigned_worker_id, job.workspace) for job in a + b}) == 5
    assert store.workers.assign_job("a", "full", "epoch") is None
    assert store.workers.assign_job("b", "full", "epoch") is None
    other = queued(store, 1, "owner/other")
    assert store.workers.workspace_demand("a", "epoch") == ["owner/other"]
    provision(store, "a", repo="owner/other")
    assert claim(store, "a", "other").id == other.id
    assert len(store.workers.list_workers()) == 2
    assert len(store.workers.get_worker_executions("a")) == 4


def test_heartbeat_renews_only_its_job_not_sibling_workspaces(store: Store, monkeypatch) -> None:
    # One live agent must not mask a lost agent's lease on another workspace.
    connect(store, "a")
    for index in range(2):
        provision(store, "a", index)
        queued(store, index)
    first, second = [claim(store, token=f"token-{index}") for index in range(2)]
    before = required(store.jobs.get_job(second.id))
    now = store.database.now() + timedelta(seconds=5)
    monkeypatch.setattr(store.database, "_clock", lambda: now)
    assert store.workers.heartbeat_assignment(first.id, "a", "token-0", "epoch")
    renewed = required(store.jobs.get_job(first.id))
    untouched = required(store.jobs.get_job(second.id))
    assert renewed.heartbeat_at == now
    assert renewed.lease_expires_at == now + store.database.lease_duration
    assert (untouched.heartbeat_at, untouched.lease_expires_at) == (
        before.heartbeat_at, before.lease_expires_at,
    )


def test_lowered_limit_retains_bound_work_without_filling_old_idle_slots(store: Store) -> None:
    connect(store, "a", 3)
    items = [provision(store, "a", index) for index in range(3)]
    for issue in range(3):
        queued(store, issue)
    first = claim(store)
    finish(store, first)
    assert settle(store, first)
    revision = store.jobs.enqueue_job(first.required_flow_id, JobKind.REVISION)
    connect(store, "a", 1, epoch="new")
    for item in items:
        if item.workspace == first.workspace:
            item = item.model_copy(update={"flow_id": first.flow_id})
        assert store.workers.report_workspace("a", "new", item)
    assert claim(store, "a", "revision", "new").id == revision.id
    assert store.workers.assign_job("a", "over-limit", "new") is None


def test_competing_claims_cannot_overlap_flow_or_workspace(store: Store) -> None:
    connect(store, "a", 3)
    for index in range(3):
        provision(store, "a", index)
    first = queued(store)
    store.jobs.enqueue_job(first.required_flow_id, JobKind.REVISION)
    second = queued(store, 2)
    barrier = Barrier(4)

    def compete(index: int) -> Job | None:
        barrier.wait(timeout=5)
        return store.workers.assign_job("a", f"token-{index}", "epoch")

    with ThreadPoolExecutor(max_workers=4) as pool:
        assignments = [job for job in pool.map(compete, range(4)) if job is not None]
    assert {job.id for job in assignments} == {first.id, second.id}
    assert len({job.workspace for job in assignments}) == 2


def test_late_inventory_and_reconnect_cannot_erase_claim(store: Store) -> None:
    connect(store, "a")
    item = provision(store, "a")
    queued(store)
    job = claim(store)
    assert store.workers.report_workspace("a", "epoch", item)
    current = next(w for w in store.workers.list_workspaces("a") if w.workspace == item.workspace)
    assert current.current_job_id == job.id and current.workspace_state == "occupied"
    assert not store.workers.report_workspace("a", "epoch", item.model_copy(update={"generation": 2}))
    finish(store, job)
    assert settle(store, job)
    revision = store.jobs.enqueue_job(job.required_flow_id, JobKind.REVISION)
    connect(store, "a", epoch="new")
    assert not store.workers.report_workspace("a", "epoch", item)
    assert store.workers.assign_job("a", "new", "new") is None
    assert store.workers.report_workspace("a", "new", item.model_copy(update={"flow_id": job.flow_id}))
    assert claim(store, "a", "new", "new").id == revision.id


def test_recovery_before_inventory_page_uses_existing_durable_fence(store: Store) -> None:
    # Recovery and inventory pages are independently sliced. Existing execution
    # evidence remains valid, but its row cannot accept new work until reported.
    connect(store, "a")
    provision(store, "a")
    queued(store)
    job = claim(store)
    attempt = required(store.flows.get_flow(job.required_flow_id)).current_attempt_id
    assert attempt is not None
    assert store.workers.start_assignment(job.id, attempt, "a", "token", "epoch")
    connect(store, "a", epoch="new")
    row = next(w for w in store.workers.list_workspaces("a") if w.workspace == job.workspace)
    assert row.inventory_epoch is None
    assert store.workers.resume_assignment(job.id, "a", "token", "new")
    assert store.workers.heartbeat_assignment(job.id, "a", "token", "new")
    assert store.workers.record_assignment_result(
        job.id, "a", "token", "new", Failed(job_id=job.id, assignment_token="token", error="done"),
    ) == "pending"
    assert store.workers.assign_job("a", "another", "new") is None


def test_ack_gate_and_review_affinity_survive_drain(store: Store) -> None:
    connect(store, "a")
    item = provision(store, "a")
    queued(store)
    job = claim(store)
    next_job = store.jobs.enqueue_job(job.required_flow_id, JobKind.REVISION)
    assert not settle(store, job)
    finish(store, job)
    assert store.workers.assign_job("a", "too-soon", "epoch") is None
    queued(store, 2)
    assert settle(store, job)
    assert store.workers.report_workspace("a", "epoch", item.model_copy(update={
        "state": "releasing", "flow_id": job.flow_id,
    }))
    assert store.workers.assign_job("a", "coding-during-cleanup", "epoch") is None
    assert store.workers.report_workspace("a", "epoch", item.model_copy(update={"flow_id": job.flow_id}))
    store.workers.drain("a", True)
    revision = claim(store, "a", "revision")
    assert revision.id == next_job.id
    assert (revision.workspace, revision.generation) == (job.workspace, job.generation)
    assert settle(store, job)  # valid previous ACK is retained in inventory, not reapplied
    assert next(w for w in store.workers.list_workspaces("a") if w.workspace == job.workspace).current_job_id == revision.id


def test_stop_receipt_keeps_claim_until_exact_ack_and_does_not_clear_siblings(store: Store) -> None:
    connect(store, "a")
    for index in range(2):
        provision(store, "a", index)
        queued(store, index)
    jobs = [claim(store, token=f"token-{i}") for i in range(2)]
    for job in jobs:
        assert job.assignment_token is not None
        assert store.stops.authorize_dispatch(job.id, "a", job.assignment_token, "epoch")
        with store.transaction() as tx:
            tx.lock_workflow(job.required_flow_id)
            tx.stops.request(job.required_flow_id)
    intents = store.stops.pending_for_worker("a")
    assert len(intents) == 2
    intent = intents[0]
    receipt = ExecutionStopped(**intent.fence().model_dump(), execution_nonce="stopped")
    assert store.stops.confirm("a", receipt, "epoch")
    assert len(store.stops.pending_for_worker("a")) == 1
    assert len([w for w in store.workers.list_workspaces("a") if w.current_job_id is not None]) == 2
    stopped = next(job for job in jobs if job.id == intent.job_id)
    assert settle(store, stopped, state="releasing")
    remaining = [w.current_job_id for w in store.workers.list_workspaces("a") if w.current_job_id is not None]
    assert remaining == [next(job.id for job in jobs if job.id != intent.job_id)]


def test_retained_result_witness_cannot_ack_a_later_stop(store: Store) -> None:
    connect(store, "a")
    item = provision(store, "a")
    queued(store)
    job = claim(store)
    assert store.stops.authorize_dispatch(job.id, "a", "token", "epoch")
    finish(store, job)
    assert settle(store, job)
    ControlService(database=store.database).cancel(required(store.flows.get_flow(job.required_flow_id)))
    intent = store.stops.pending_for_worker("a")[0]
    prior = item.model_copy(update={
        "flow_id": job.flow_id, "state": "occupied", "settled_job_id": job.id,
        "settled_assignment_token": job.assignment_token,
    })
    # This previous ACK is part of ordinary polling and reconnect inventory even
    # while cancellation requests a second acknowledgement for the same journal.
    connect(store, "a", epoch="new")
    assert store.workers.report_workspace("a", "new", prior)
    receipt = ExecutionStopped(**intent.fence().model_dump(), execution_nonce="stopped")
    assert store.stops.confirm("a", receipt, "new")
    assert store.workers.report_workspace("a", "new", prior)
    row = next(w for w in store.workers.list_workspaces("a") if w.workspace == job.workspace)
    assert row.current_job_id == job.id
    assert required(store.workers.get_flow_execution(job.required_flow_id)).released_at is None
    assert store.workers.report_workspace("a", "new", prior.model_copy(update={
        "flow_id": None, "state": "idle", "generation": 2,
    }))
    assert required(store.flows.get_flow(job.required_flow_id)).state is FlowState.ARCHIVED


def test_retired_generation_and_absence_never_reopen_an_old_binding(store: Store) -> None:
    connect(store, "a")
    item = provision(store, "a")
    queued(store)
    job = claim(store)
    assert store.workers.report_workspace("a", "epoch", item.model_copy(update={"state": "absent"}))
    assert required(store.workers.get_flow_execution(job.required_flow_id)).released_at is None
    finish(store, job)
    assert not store.workers.release_flow_execution(job.required_flow_id)
    assert settle(store, job)
    assert store.workers.release_flow_execution(job.required_flow_id)
    queued(store, 2)
    assert store.workers.report_workspace("a", "epoch", item)
    assert store.workers.assign_job("a", "old-generation", "epoch") is None
    assert store.workers.report_workspace("a", "epoch", item.model_copy(update={"generation": 2}))
    assert claim(store, token="new-generation").generation == 2


def test_cleanup_ack_can_atomically_retire_binding_and_publish_next_generation(store: Store) -> None:
    connect(store, "a")
    item = provision(store, "a")
    queued(store)
    job = claim(store)
    assert store.stops.authorize_dispatch(job.id, "a", "token", "epoch")
    ControlService(database=store.database).cancel(required(store.flows.get_flow(job.required_flow_id)))
    intent = store.stops.pending_for_worker("a")[0]
    ready = item.model_copy(update={
        "generation": 2, "settled_job_id": job.id, "settled_assignment_token": job.assignment_token,
    })
    assert not store.workers.report_workspace("a", "epoch", ready)
    assert store.stops.confirm("a", ExecutionStopped(**intent.fence().model_dump(), execution_nonce="cleaned"), "epoch")
    assert store.workers.report_workspace("a", "epoch", ready)
    assert required(store.flows.get_flow(job.required_flow_id)).state is FlowState.ARCHIVED
    assert store.workers.report_workspace("a", "epoch", ready)  # reconnect/replay is idempotent
    queued(store, 2)
    successor = claim(store, token="successor")
    assert successor.generation == 2
    assert store.workers.report_workspace("a", "epoch", ready)
    assert next(w for w in store.workers.list_workspaces("a") if w.workspace == job.workspace).current_job_id == successor.id


def test_premature_generation_advance_rolls_back_ack_clear(store: Store) -> None:
    connect(store, "a")
    item = provision(store, "a")
    queued(store)
    job = claim(store)
    finish(store, job)
    # Ordinary completion keeps the Flow's directory through review. Claim clearing
    # and generation publication must roll back together if it cannot be archived.
    assert not store.workers.report_workspace("a", "epoch", item.model_copy(update={
        "generation": 2, "settled_job_id": job.id, "settled_assignment_token": job.assignment_token,
    }))
    assert next(w for w in store.workers.list_workspaces("a") if w.workspace == job.workspace).current_job_id == job.id


@pytest.mark.parametrize("corruption", ["missing", "generation"])
def test_missing_or_wrong_workspace_rejects_every_assignment_api(store: Store, corruption: str) -> None:
    connect(store, "a")
    provision(store, "a")
    queued(store)
    job = claim(store)
    attempt = required(store.flows.get_flow(job.required_flow_id)).current_attempt_id
    assert attempt is not None
    with store.connection() as conn:
        if corruption == "missing":
            conn.execute("DELETE FROM workers WHERE worker_id='a' AND workspace=%s", (job.workspace,))
        else:
            conn.execute("UPDATE workers SET generation=2 WHERE worker_id='a' AND workspace=%s", (job.workspace,))
    assert not store.workers.assignment_active(job.id, "a", "token", "epoch")
    assert not store.workers.start_assignment(job.id, attempt, "a", "token", "epoch")
    assert not store.workers.heartbeat_assignment(job.id, "a", "token", "epoch")
    assert not store.workers.resume_assignment(job.id, "a", "token", "epoch")
    assert not store.stops.authorize_dispatch(job.id, "a", "token", "epoch")
    assert not store.workers.finish_assignment(job.id, "a", "token", JobState.FAILED, "epoch")
    assert store.workers.record_assignment_result(
        job.id, "a", "token", "epoch", Failed(job_id=job.id, assignment_token="token", error="failure"),
    ) == "rejected"


def test_inference_is_one_default_lane_with_local_preference(store: Store) -> None:
    connect(store, "a", 3)
    connect(store, LOCAL_BUB_WORKER_ID)
    for i in range(3):
        provision(store, "a", i)
    for worker in ("a", LOCAL_BUB_WORKER_ID):
        assert store.workers.worker_ready(worker, "epoch", InferenceCapability(adapter_version="test"))
    first = store.jobs.enqueue_inference(InferenceTask(messages=[InferenceMessage(role="user", content="one")]))
    assert store.workers.assign_job("a", "remote", "epoch", slot="infer") is None
    local = store.workers.assign_job(LOCAL_BUB_WORKER_ID, "local", "epoch", slot="infer")
    assert local is not None
    assert local.id == first.id and local.workspace == identity(LOCAL_BUB_WORKER_ID + ":default")
    store.jobs.enqueue_inference(InferenceTask(messages=[InferenceMessage(role="user", content="two")]))
    remote = store.workers.assign_job("a", "remote", "epoch", slot="infer")
    assert remote is not None
    assert remote.workspace == identity("a:default")
    store.jobs.enqueue_inference(InferenceTask(messages=[InferenceMessage(role="user", content="three")]))
    assert store.workers.assign_job("a", "extra", "epoch", slot="infer") is None


def test_legacy_worker_schema_requires_explicit_physical_identity_migration(postgresql_url: str) -> None:
    with psycopg.connect(postgresql_url) as conn:
        conn.execute("CREATE TABLE workers(worker_id TEXT PRIMARY KEY)")
    store = Store(postgresql_url)
    try:
        with pytest.raises(ValueError, match="explicit workspace migration required"):
            store.init_db()
    finally:
        store.close()
