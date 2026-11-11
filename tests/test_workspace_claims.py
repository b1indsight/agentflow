"""Concrete inventory, controller generations, and writer fences on PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
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
        workspace=identity(worker + ":default"), repository=None, generation=1, flow_id=None,
    ))


def inventory(worker: str, index: int = 0, repo: str = "owner/repo", **changes) -> WorkspaceInventory:
    return WorkspaceInventory.model_validate({
        "workspace": identity(f"{worker}:{repo}:{index}"), "repository": repo,
        "generation": 1, "flow_id": None, **changes,
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
    assert current.current_job_id == job.id
    assert not store.workers.report_workspace("a", "epoch", item.model_copy(update={"generation": 2}))
    finish(store, job)
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


def test_terminal_commit_reuses_binding_without_ack_and_preserves_drain(store: Store) -> None:
    connect(store, "a")
    item = provision(store, "a")
    queued(store)
    job = claim(store)
    next_job = store.jobs.enqueue_job(job.required_flow_id, JobKind.REVISION)
    assert store.workers.assign_job("a", "too-soon", "epoch") is None
    finish(store, job)
    queued(store, 2)
    store.workers.drain("a", True)
    revision = claim(store, "a", "revision")
    assert revision.id == next_job.id
    assert (revision.workspace, revision.generation) == (job.workspace, job.generation)
    assert store.workers.report_workspace("a", "epoch", item)
    assert next(w for w in store.workers.list_workspaces("a") if w.workspace == job.workspace).current_job_id == revision.id


def test_stop_receipt_clears_exact_claim_before_cleanup_without_touching_siblings(store: Store) -> None:
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
    remaining = [w.current_job_id for w in store.workers.list_workspaces("a") if w.current_job_id is not None]
    assert remaining == [next(job.id for job in jobs if job.id != intent.job_id)]


def test_stop_and_terminal_replays_after_reuse_cannot_clear_successor(store: Store) -> None:
    connect(store, "a")
    item = provision(store, "a")
    queued(store)
    job = claim(store)
    assert store.stops.authorize_dispatch(job.id, "a", "token", "epoch")
    finish(store, job)
    ControlService(database=store.database).cancel(required(store.flows.get_flow(job.required_flow_id)))
    intent = store.stops.pending_for_worker("a")[0]
    receipt = ExecutionStopped(**intent.fence().model_dump(), execution_nonce="stopped")
    assert store.stops.confirm("a", receipt, "epoch")
    assert required(store.flows.get_flow(job.required_flow_id)).state is FlowState.ARCHIVED
    queued(store, 2)
    successor = claim(store, token="successor")
    assert successor.generation == 2
    assert store.stops.confirm("a", receipt, "epoch")
    assert not store.stops.confirm("a", receipt.model_copy(update={"execution_nonce": "changed"}), "epoch")
    assert store.workers.record_assignment_result(
        job.id, "a", "token", "epoch", Failed(job_id=job.id, assignment_token="token", error="test outcome"),
    ) == "applied"
    assert store.workers.record_assignment_result(
        job.id, "a", "token", "epoch", Failed(job_id=job.id, assignment_token="token", error="changed"),
    ) == "conflict"
    assert store.workers.report_workspace("a", "epoch", item.model_copy(update={"flow_id": job.flow_id}))
    assert next(w for w in store.workers.list_workspaces("a") if w.workspace == job.workspace).current_job_id == successor.id


def test_release_advances_once_and_reconnect_accepts_lower_inventory_without_reviving_binding(store: Store) -> None:
    connect(store, "a")
    item = provision(store, "a")
    queued(store)
    job = claim(store)
    finish(store, job)
    # A business transaction can fail after release; neither ownership nor its
    # generation may escape the rollback and advertise a reusable successor.
    with pytest.raises(RuntimeError, match="interrupted"), store.transaction() as tx:
        assert tx.workers.release_flow_execution(job.required_flow_id)
        raise RuntimeError("interrupted")
    assert required(store.workers.get_flow_execution(job.required_flow_id)).released_at is None
    assert next(w for w in store.workers.list_workspaces("a") if w.workspace == item.workspace).generation == 1
    assert store.workers.release_flow_execution(job.required_flow_id)
    assert not store.workers.release_flow_execution(job.required_flow_id)
    connect(store, "a", epoch="new")
    assert store.workers.report_workspace("a", "new", item.model_copy(update={"flow_id": job.flow_id}))
    row = next(w for w in store.workers.list_workspaces("a") if w.workspace == item.workspace)
    assert row.generation == 2 and row.current_job_id is None and row.inventory_epoch == "new"
    assert required(store.workers.get_flow_execution(job.required_flow_id)).released_at is not None
    assert not store.workers.report_workspace("a", "new", item.model_copy(update={"generation": 3}))
    assert not store.workers.report_workspace("a", "new", item.model_copy(update={"repository": "owner/other"}))
    queued(store, 2)
    assert claim(store, token="new-generation", epoch="new").generation == 2


def test_started_control_failure_retains_unknown_writer_claim(store: Store) -> None:
    connect(store, "a")
    provision(store, "a")
    queued(store)
    job = claim(store)
    attempt = required(store.flows.get_flow(job.required_flow_id)).current_attempt_id
    assert attempt is not None
    assert store.workers.start_assignment(job.id, attempt, "a", "token", "epoch")
    store.jobs.record_control_result(job.id, Failed(job_id=job.id, assignment_token="token", error="lost"), "lost")
    assert store.jobs.complete_persisted_result(job.id, JobState.FAILED)
    store.jobs.enqueue_job(job.required_flow_id, JobKind.REVISION)
    assert not store.workers.release_flow_execution(job.required_flow_id)
    assert store.workers.assign_job("a", "unsafe", "epoch") is None
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


def test_obsolete_workspace_state_schema_is_explicitly_rejected(postgresql_url: str) -> None:
    with psycopg.connect(postgresql_url) as conn:
        conn.execute("CREATE TABLE workers(worker_id TEXT, workspace TEXT, workspace_state TEXT,"
                     " PRIMARY KEY(worker_id,workspace))")
    store = Store(postgresql_url)
    try:
        with pytest.raises(ValueError, match="obsolete workspace state schema"):
            store.init_db()
    finally:
        store.close()
