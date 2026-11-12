"""Direct inventory, concurrent execution, and independently recoverable Flow journals."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from app.contracts.protocol import (
    ArchiveTask,
    AssignmentArguments,
    ExecutionStopped,
    InitialTask,
)
from app.domain.models import (
    ArchiveReason,
    CodingExecutionResult,
    DeliveryState,
    FlowState,
    JobKind,
    JobState,
    WorkspaceInventory,
)
from app.persistence.activity import ActivityStore
from app.persistence.workers import WorkerStore
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.vcs import VcsVerificationError
from app.worker.execution.workspaces import RepositoryWorkspaces, WorkspaceBinding
from app.worker.state.execution import ExecutionState, ExecutionStateError
from tests.test_worker_executor import RecordingAgent, RecordingToolCaller, assignment_secret
from tests.test_worker_session import _previous_journal
from tests.test_worker_workspaces import managed_seed
from tests.test_workspace_claims import claim as claim_job
from tests.test_workspace_claims import connect, finish, provision, queued


def assignment(flow: int, workspace: str, generation: int = 1) -> AssignmentArguments:
    return AssignmentArguments(
        job_id=flow, flow_id=flow, assignment_token=f"token-{flow}",
        workspace=workspace, generation=generation,
        task=InitialTask(flow_id=flow, attempt_id=flow, repo="owner/repo",
                         repo_identity="worker-a:owner/repo", issue_number=flow,
                         task_content="Issue\n\nBody", branch=f"flow/{flow}"),
    )


async def claim(pool, flow, target):
    return await pool.claim("owner/repo", flow, target.workspace, target.generation,
                            base_branch="main", remote="origin", push_token=None)


def test_default_workspace_is_real_stable_and_not_adopted_after_replacement(tmp_path):
    pool = RepositoryWorkspaces(tmp_path, "worker-a")
    initial = pool.initialize()
    path = pool.inference_path
    assert initial.repository is None and initial.flow_id is None and path.is_dir()
    assert pool.inventory() == [initial]
    restarted = RepositoryWorkspaces(tmp_path, "worker-a")
    assert restarted.initialize() == initial and restarted.inference_path == path
    path.rename(path.with_name("saved"))
    path.mkdir()
    with pytest.raises(VcsVerificationError, match="identity"):
        restarted.initialize()


async def test_provision_obeys_each_worker_quota_without_binding_or_credentials(tmp_path):
    pools = [RepositoryWorkspaces(tmp_path / str(limit), f"worker-{limit}", limit=limit)
             for limit in (3, 2)]
    for pool in pools:
        await asyncio.gather(pool.provision("owner/repo"), pool.provision("owner/repo"))
        inventory = pool.inventory()
        slots = [item for item in inventory if item.repository == "owner/repo"]
        assert len(slots) == pool.limit
        assert all(item.flow_id is None and item.generation == 1 for item in slots)
        assert all(set(item.model_dump()) == {"workspace", "repository", "generation", "flow_id"}
                   for item in inventory)
        for item in slots:
            path = pool.root / "workspaces/owner/repo" / item.workspace
            assert path.is_dir() and not list(path.iterdir())
        assert not (pool.root / "repos").exists()
        smaller = RepositoryWorkspaces(pool.root, pool.worker_id, limit=1)
        assert await smaller.provision("owner/repo") == inventory
        await pool.provision("other/repo")
        assert len([i for i in pool.inventory() if i.repository == "other/repo"]) == pool.limit


async def test_exact_claim_accepts_assigned_generation_and_rejects_stale_or_conflicting_binding(tmp_path):
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a")
    first, second = (await pool.provision("owner/repo"))[1:]
    with pytest.raises(VcsVerificationError, match="identity or generation"):
        await claim(pool, 1, first.model_copy(update={"workspace": "f" * 32}))
    assert all(i.flow_id is None for i in pool.inventory())
    second = second.model_copy(update={"generation": 3})
    bound = await claim(pool, 1, second)
    assert bound.slot_id == second.workspace and bound.generation == second.generation
    with pytest.raises(VcsVerificationError, match="identity or generation"):
        await claim(pool, 1, second.model_copy(update={"generation": 2}))
    with pytest.raises(VcsVerificationError, match="bound"):
        await claim(pool, 2, second)
    assert await claim(pool, 1, second) == bound
    # After runtime proves quiescence, a controller-selected slot replaces the Flow's old fence.
    moved = await claim(pool, 1, first)
    inventory = pool.inventory()
    assert [item.workspace for item in inventory if item.flow_id == 1] == [first.workspace]
    previous = next(item for item in inventory if item.workspace == second.workspace)
    assert previous.flow_id is None and previous.generation == second.generation
    assert pool.lookup("owner/repo", 1) == moved


@pytest.mark.parametrize("overflow", [False, True])
async def test_cleanup_retains_fence_until_newer_claim_without_ack(tmp_path, overflow):
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a")
    targets = (await pool.provision("owner/repo"))[1:]
    target = targets[int(overflow)]
    binding = await claim(pool, 1, target)
    await pool.release(binding, job_id=2, token="archive")
    restarted = RepositoryWorkspaces(root, "worker-a")
    reported = next(i for i in restarted.inventory() if i.workspace == target.workspace)
    assert reported.flow_id == 1
    assert reported.generation == binding.generation
    with pytest.raises(VcsVerificationError, match="bound"):
        await claim(restarted, 2, target)
    assert next(i for i in await restarted.provision("owner/repo")
                if i.workspace == target.workspace) == reported
    fresh = reported.model_copy(update={"generation": binding.generation + 1})
    newer = await claim(restarted, 2, fresh)
    before = restarted.manifest_path.read_bytes()
    with pytest.raises(VcsVerificationError, match="ownership"):
        await restarted.release(binding, job_id=2, token="archive")
    with pytest.raises(VcsVerificationError, match="identity or generation"):
        await claim(restarted, 1, target)
    assert restarted.manifest_path.read_bytes() == before
    assert restarted.lookup("owner/repo", 2) == newer
    # A queued job can be older than the previous Flow's archive: order is generation-local.
    await restarted.release(newer, job_id=1, token="queued-before-archive")
    item = next(i for i in restarted.inventory() if i.workspace == target.workspace)
    assert item.flow_id == 2 and item.generation == newer.generation


async def test_slow_preparation_does_not_overwrite_other_repository_inventory(tmp_path, monkeypatch):
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a")
    target = (await pool.provision("owner/repo"))[1]
    from app.worker.execution import workspaces
    original = workspaces.ensure_seed
    entered, finish = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        entered.set()
        await finish.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(workspaces, "ensure_seed", delayed)
    pending = asyncio.create_task(claim(pool, 1, target))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(pool.provision("other/repo"), 2)
        assert len([i for i in pool.inventory() if i.repository == "other/repo"]) == 2
    finally:
        finish.set()
        await pending
    assert len([i for i in pool.inventory() if i.repository == "other/repo"]) == 2


async def test_two_executors_share_pool_but_agents_overlap_on_assigned_paths(tmp_path):
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a")
    targets = (await pool.provision("owner/repo"))[1:]
    entered, finish = asyncio.Event(), asyncio.Event()
    paths: list[Path] = []

    class Agent:
        async def start(self, prompt, cwd, **kwargs):
            paths.append(cwd)
            if len(paths) == 2:
                entered.set()
            await finish.wait()
            return CodingExecutionResult(session_id="s", last_message="controlled stop", exit_code=1)

        async def resume(self, session_id, *args, **kwargs):
            return CodingExecutionResult(session_id=session_id, last_message="controlled stop", exit_code=1)

    tasks = []
    for flow, target in enumerate(targets, 1):
        state = ExecutionState(root, flow)
        received = assignment(flow, target.workspace)
        state.record_received(received)
        state.record_started()
        executor = WorkerExecutor(root, Agent(), worker_id="worker-a", state=state, workspaces=pool)
        tasks.append(asyncio.create_task(executor.execute(
            received.task, assignment_secret(), tool_caller=RecordingToolCaller(),
        )))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert len(set(paths)) == 2
        assert {path.name for path in paths} == {item.workspace for item in targets}
    finally:
        finish.set()
        results = await asyncio.gather(*tasks)
    assert all(result.exit_code == 1 for result in results)
    assert ExecutionState.scan(root) == [1, 2]


async def test_flow_journals_isolate_unacknowledged_received_and_archive_resume(tmp_path):
    pool = RepositoryWorkspaces(tmp_path, "worker-a")
    targets = (await pool.provision("owner/repo"))[1:]
    first, second = ExecutionState(tmp_path, 1), ExecutionState(tmp_path, 2)
    received = assignment(1, targets[0].workspace)
    initial = first.record_received(received)
    assert first.record_received(received).execution_nonce == initial.execution_nonce
    second.record_received(assignment(2, targets[1].workspace))
    before = first.path.read_bytes()
    with pytest.raises(ExecutionStateError, match="unacknowledged"):
        first.record_received(received.model_copy(update={"assignment_token": "different"}))
    assert first.path.read_bytes() == before
    second.record_started()
    second.complete(RuntimeError("finished"))
    assert first.path.read_bytes() == before and ExecutionState.scan(tmp_path) == [1, 2]
    first.record_started()
    first.complete(RuntimeError("never bound"))
    first.acknowledge(1, JobState.FAILED)
    first.reconcile_acknowledgement()
    with pytest.raises(ExecutionStateError, match="settled assignment"):
        first.record_received(received)
    archive = received.model_copy(update={
        "job_id": 3, "assignment_token": "archive",
        "task": ArchiveTask(flow_id=1, attempt_id=1, repo="owner/repo", worker_id="worker-a",
                            repo_identity="worker-a:owner/repo", delivery_version=1),
    })
    first.record_received(archive)
    first.record_started()
    assert ExecutionState(tmp_path, 1).resume(executor_active=False) == archive
    assert not (tmp_path / "journals/current.json").exists()


@pytest.mark.parametrize("damage", ["symlink", "root", "record"])
def test_scan_preserves_all_evidence_on_invalid_inventory(tmp_path, damage):
    state = ExecutionState(tmp_path, 1)
    state.record_received(assignment(1, "a" * 32))
    if damage == "symlink":
        (state.directory / "flow-2.json").symlink_to(state.path)
    else:
        data = json.loads(state.path.read_text())
        data["root_identity" if damage == "root" else "flow_id"] = ["other", 1, 2] if damage == "root" else 2
        state.path.write_text(json.dumps(data))
    before = {path: path.read_bytes() for path in state.directory.iterdir()}
    with pytest.raises(ExecutionStateError):
        ExecutionState.scan(tmp_path)
    assert {path: path.read_bytes() for path in state.directory.iterdir()} == before


def test_binding_must_match_assignment_target(tmp_path):
    state = ExecutionState(tmp_path, 1)
    state.record_received(assignment(1, "a" * 32))
    state.record_started()
    with pytest.raises(ExecutionStateError, match="allocation"):
        state.bind_workspace(WorkspaceBinding(repository="owner/repo", slot_id="b" * 32,
                                               flow_id=1, generation=1))


@pytest.mark.parametrize("version", [4, 5])
def test_scan_migrates_legacy_selection_without_losing_unselected_records(tmp_path, monkeypatch, version):
    other = ExecutionState(tmp_path, 4)
    other.record_received(assignment(4, "b" * 32))
    other_bytes = other.path.read_bytes()
    legacy = tmp_path / "assignment-journal.json"
    if version == 4:
        legacy.write_text(json.dumps(_previous_journal(tmp_path)))
    else:
        state = ExecutionState(tmp_path, 3)
        state.record_received(assignment(3, "a" * 32))
        state.path.rename(legacy)
    original = legacy.read_bytes()
    selection = other.directory / "current.json"
    selection.write_text('{"flow_id": 3}')
    unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        if path == selection:
            raise OSError("pointer retirement interrupted")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupted)
    with pytest.raises(OSError, match="retirement interrupted"):
        ExecutionState.scan(tmp_path)
    migrated = other.directory / "flow-3.json"
    assert migrated.read_bytes() == original and not legacy.exists()
    assert other.path.read_bytes() == other_bytes
    monkeypatch.setattr(Path, "unlink", unlink)
    assert ExecutionState.scan(tmp_path) == [3, 4]
    assert not selection.exists() and migrated.read_bytes() == original
    assert not ExecutionState(tmp_path, 3).load().acknowledged
    assert not other.load().acknowledged


@pytest.mark.parametrize("state", ["occupied", "idle"])
async def test_legacy_pool_migration_preserves_real_flow_binding(tmp_path, state):
    root, _ = managed_seed(tmp_path)
    binding = WorkspaceBinding(repository="owner/repo", slot_id="a" * 32, flow_id=1, generation=7)
    relative = "workspaces/owner/repo/" + binding.slot_id
    shutil.copytree(root / "repos/owner/repo", root / relative)
    stat = root.stat()
    manifest_path = root / "workspace-pool.json"
    manifest_path.write_text(json.dumps({
        "version": 1, "worker_id": "worker-a",
        "root_identity": [str(root.resolve()), stat.st_dev, stat.st_ino],
        "slots": [{"binding": binding.model_dump(), "relative_path": relative,
                   "kind": "copy", "state": state}],
    }))
    pool = RepositoryWorkspaces(root, "worker-a", limit=1)
    pool.initialize()
    item = pool.inventory()[1]
    assert item.flow_id == 1 and item.generation == 7
    assert len(await pool.provision("owner/repo")) == 2
    with pytest.raises(VcsVerificationError, match="bound"):
        await claim(pool, 2, item)
    with pytest.raises(VcsVerificationError, match="identity"):
        pool.lookup("owner/repo", 1)
    with pytest.raises(VcsVerificationError, match="identity"):
        await pool.release(binding, job_id=1, token="archive")
    slot = json.loads(manifest_path.read_text())["slots"][0]
    assert slot["binding"] == binding.model_dump() and slot["directory_identity"] is None
    assert "state" not in slot
    assert (root / relative / "README.md").read_text() == "base\n"


async def test_cleanup_completion_cannot_rewrite_a_changed_allocation(tmp_path, monkeypatch):
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a", limit=1)
    target = (await pool.provision("owner/repo"))[1]
    binding = await claim(pool, 1, target)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def delayed(path):
        entered.set()
        await finish.wait()

    monkeypatch.setattr("app.worker.execution.workspaces.require_idle_repository", delayed)
    pending = asyncio.create_task(pool.release(binding, job_id=1, token="archive"))
    await asyncio.wait_for(entered.wait(), 2)
    # Simulate external reconciliation while the stale cleanup is suspended at an await.
    data = json.loads(pool.manifest_path.read_text())
    slot = data["slots"][0]
    slot["generation"] += 1
    slot["binding"]["generation"] += 1
    slot["binding"]["flow_id"] = 2
    pool.manifest_path.write_text(json.dumps(data))
    before = pool.manifest_path.read_bytes()
    finish.set()
    with pytest.raises(VcsVerificationError, match="ownership"):
        await pending
    assert pool.manifest_path.read_bytes() == before


@pytest.mark.parametrize("during_claim", [False, True])
async def test_failed_preparation_ack_does_not_mutate_selected_target(
    tmp_path, monkeypatch, during_claim,
):
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a")
    target = (await pool.provision("owner/repo"))[1]
    state = ExecutionState(root, 1)
    received = assignment(1, target.workspace)
    state.record_received(received)
    state.record_started()

    async def failed_seed(*args, **kwargs):
        raise VcsVerificationError("bootstrap", "controlled preparation failure")

    monkeypatch.setattr("app.worker.execution.workspaces.ensure_seed", failed_seed)
    agent = RecordingAgent()
    executor = WorkerExecutor(root, agent, worker_id="worker-a", state=state, workspaces=pool)
    result = await executor.execute(
        received.task, assignment_secret() if during_claim else None,
        tool_caller=RecordingToolCaller(),
    )
    assert result.exit_code != 0 and state.load().workspace is None
    assert not agent.started
    state.complete(result)
    before = pool.manifest_path.read_bytes()
    state.acknowledge(1, JobState.FAILED)
    restarted = ExecutionState(root, 1)
    restarted.reconcile_acknowledgement()
    assert pool.manifest_path.read_bytes() == before
    item = next(i for i in pool.inventory() if i.workspace == target.workspace)
    assert item.generation == target.generation
    assert item.flow_id == (1 if during_claim else None)
    assert restarted.load().acknowledged
    assert "pool_acknowledged" not in json.loads(restarted.path.read_text())
    restarted.record_received(received.model_copy(update={"job_id": 2, "assignment_token": "next"}))
    assert restarted.load().job_id == 2


async def test_late_result_ack_never_mutates_newer_binding(tmp_path):
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-a")
    target = (await pool.provision("owner/repo"))[1]
    binding = await claim(pool, 1, target)
    state = ExecutionState(root, 1)
    state.record_received(assignment(1, target.workspace))
    state.record_started()
    state.bind_workspace(binding)
    state.complete(RuntimeError("finished before the next allocation"))
    newer = await claim(pool, 2, target.model_copy(update={"generation": target.generation + 1}))
    before = pool.manifest_path.read_bytes()
    state.acknowledge(1, JobState.FAILED)
    state.reconcile_acknowledgement()
    assert pool.manifest_path.read_bytes() == before
    assert pool.lookup("owner/repo", 2) == newer
    assert state.load().workspace == binding


def merged_business(store, flow_id):
    """Model the business commit independently of the resource settlement under test."""
    flow = store.flows.get_flow(flow_id)
    delivery = store.deliveries.get_or_create_delivery(flow.id, flow.initial_branch)
    delivery.state, delivery.pr_number = DeliveryState.MERGED, 17
    delivery.merged_at, delivery.merge_commit_sha = store.now(), "a" * 40
    store.deliveries.update_delivery(delivery)
    flow.state = FlowState.ARCHIVED
    flow.archive_reason = ArchiveReason.SUCCEEDED
    flow.archived_at = store.now()
    ActivityStore(store.database).append_flow(
        flow.id, flow.current_round_no, "archive", occurred_at=flow.archived_at,
        metadata={"archive_reason": "succeeded"},
    )
    store.flows.update_flow(flow)
    return flow


def uncertain_effect(store, job_id):
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO result_applications(job_id,digest,plan,created_at) VALUES (%s,'test','{}',%s)",
            (job_id, store.now()),
        )
        conn.execute(
            "INSERT INTO result_effects(job_id,effect_key,position,payload,state,dispatched_at,next_attempt_at)"
            " VALUES (%s,'uncertain-comment',0,'{\"kind\":\"comment\"}','uncertain',%s,%s)",
            (job_id, store.now(), store.now()),
        )


@pytest.mark.parametrize("blocker", [None, "remote-result", "remote-effect", "release-failure"])
def test_archived_stop_settlement_reuses_only_exact_cleaned_generation(store, monkeypatch, blocker):
    # Inventory cannot prove writer exit; only the original stop and settled
    # remote work can publish a reusable controller-owned generation.
    connect(store, "a")
    for index in range(2):
        provision(store, "a", index)
        queued(store, index)
    job, sibling = claim_job(store), claim_job(store, token="sibling")
    assert store.stops.authorize_dispatch(job.id, "a", "token", "epoch")
    finish(store, job)
    with store.transaction() as tx:
        tx.lock_workflow(job.flow_id)
        tx.stops.request(job.flow_id)
    flow = merged_business(store, job.flow_id)
    intent = store.stops.pending_for_worker("a")[0]
    receipt = ExecutionStopped(**intent.fence().model_dump(), execution_nonce="original-cleanup")
    before = next(w for w in store.workers.list_workspaces("a") if w.workspace == sibling.workspace)
    fresh = WorkspaceInventory(
        workspace=job.workspace, repository=flow.repo, generation=job.generation + 1,
        flow_id=None,
    )
    assert not store.workers.report_workspace("a", "epoch", fresh)
    if blocker == "remote-result":
        with store.connection() as conn:
            conn.execute("UPDATE jobs SET result_applied_at=NULL WHERE id=%s", (job.id,))
    elif blocker == "remote-effect":
        uncertain_effect(store, job.id)
    if blocker in {"remote-result", "remote-effect"}:
        assert not store.stops.confirm("a", receipt, "epoch")
        assert not store.workers.report_workspace("a", "epoch", fresh)
        with store.connection() as conn:
            conn.execute("UPDATE jobs SET result_applied_at=%s WHERE id=%s", (store.now(), job.id))
            conn.execute("UPDATE result_effects SET receipt='{}' WHERE job_id=%s", (job.id,))
    successor = queued(store, 3)
    assert store.workers.assign_job("a", "too-soon", "epoch") is None
    if blocker == "release-failure":
        with monkeypatch.context() as patch:
            patch.setattr(WorkerStore, "release_flow_execution", lambda *args: False)
            assert store.stops.confirm("a", receipt, "epoch")
        current = next(w for w in store.workers.list_workspaces("a") if w.workspace == job.workspace)
        assert current.current_job_id is None and current.generation == job.generation
        assert store.workers.get_flow_execution(flow.id).released_at is None
        assert not store.workers.report_workspace("a", "epoch", fresh)
        assert store.flows.try_archive(flow.id)
    else:
        assert store.stops.confirm("a", receipt, "epoch")
    assert store.workers.get_flow_execution(flow.id).released_at is not None
    assert store.workers.report_workspace("a", "epoch", fresh)
    # No clock advance or sweeper: settlement immediately publishes the next generation.
    next_job = claim_job(store, token="next")
    assert next_job.id == successor.id and next_job.generation == fresh.generation
    assert next_job.workspace == job.workspace
    assert store.workers.report_workspace("a", "epoch", fresh.model_copy(update={"generation": job.generation}))
    assert store.workers.report_workspace("a", "epoch", fresh)
    current = next(w for w in store.workers.list_workspaces("a") if w.workspace == job.workspace)
    assert current.current_job_id == next_job.id
    assert next(w for w in store.workers.list_workspaces("a") if w.workspace == sibling.workspace) == before
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED


@pytest.mark.parametrize("mismatch", [None, "version", "source", "assigned", "kind"])
def test_only_local_merge_notice_is_independent_of_resource_settlement(store, mismatch):
    # Notification delivery may remain uncertain while the original stop finishes;
    # a lookalike remote result must keep its ordinary resource preconditions.
    connect(store, "a")
    item = provision(store, "a")
    queued(store)
    job = claim_job(store)
    assert store.stops.authorize_dispatch(job.id, "a", "token", "epoch")
    with store.transaction() as tx:
        tx.lock_workflow(job.flow_id)
        tx.stops.request(job.flow_id)
    flow = merged_business(store, job.flow_id)
    notice = store.jobs.enqueue_job(flow.id, JobKind.ARCHIVE)
    payload = {"version": 2, "source": "control", "terminal": {
        "type": "merge_archive", "job_id": notice.id, "flow_id": flow.id, "round_no": 1,
        "repo": flow.repo, "pr_number": 17, "merged_at": store.now().isoformat(),
        "merge_commit_sha": "a" * 40,
    }}
    if mismatch in {"version", "source"}:
        payload[mismatch] = "2" if mismatch == "version" else "worker"
    with store.connection() as conn:
        conn.execute(
            "UPDATE jobs SET state='running',result_payload=%s,result_digest='test',result_received_at=%s,"
            " assigned_worker_id=%s,kind=%s WHERE id=%s",
            (Jsonb(payload), store.now(), "a" if mismatch == "assigned" else None,
             JobKind.REVISION if mismatch == "kind" else JobKind.ARCHIVE, notice.id),
        )
    uncertain_effect(store, notice.id)
    intent = store.stops.pending_for_worker("a")[0]
    receipt = ExecutionStopped(**intent.fence().model_dump(), execution_nonce="original-cleanup")
    confirmed = store.stops.confirm("a", receipt, "epoch")
    assert confirmed is (mismatch is None)
    fresh = item.model_copy(update={"generation": 2})
    assert store.workers.report_workspace("a", "epoch", fresh) is (mismatch is None)
    assert (store.workers.get_flow_execution(flow.id).released_at is not None) is (mismatch is None)
    assert store.jobs.get_job(notice.id).result_applied_at is None
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED


@pytest.mark.parametrize("started", [False, True])
def test_archived_coding_cannot_resume_or_supply_cleanup_evidence(store, started):
    connect(store, "a")
    item = provision(store, "a")
    queued(store)
    job = claim_job(store)
    attempt = store.flows.get_flow(job.flow_id).current_attempt_id
    assert store.stops.authorize_dispatch(job.id, "a", "token", "epoch")
    if started:
        assert store.workers.start_assignment(job.id, attempt, "a", "token", "epoch")
    merged_business(store, job.flow_id)
    assert not store.workers.start_assignment(job.id, attempt, "a", "token", "epoch")
    assert not store.workers.resume_assignment(job.id, "a", "token", "epoch")
    finish(store, job)  # A late coding result may settle, but it never proves cleanup.
    assert not store.workers.report_workspace("a", "epoch", item.model_copy(update={"generation": 2}))
    current = next(w for w in store.workers.list_workspaces("a") if w.workspace == job.workspace)
    assert current.current_job_id is None and current.generation == job.generation
    assert store.workers.get_flow_execution(job.flow_id).released_at is None
