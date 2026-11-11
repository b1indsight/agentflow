"""Real worker directories and PostgreSQL across the deletion/result/ACK crash windows."""

import asyncio
import shutil
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from app.communication.gateway import Gateway
from app.contracts.protocol import (
    PROTOCOL_VERSION,
    AcceptResultResponse,
    AssignmentArguments,
    InitialTask,
    InitialTaskResult,
    RevisionTask,
    Succeeded,
    parse_server_message,
)
from app.contracts.receipts import decode_result_inbox
from app.controler.workers.transport import serve_worker
from app.controler.workflow.archive import archive_progress
from app.controler.workflow.service import ControlService
from app.domain.models import FlowState, JobKind, JobState
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.execution.workspaces import RepositoryWorkspaces
from app.worker.runtime import WorkerRootLock
from app.worker.state.execution import ExecutionState
from tests.github import RecordingGitHub
from tests.test_archive_recovery import ready_archive, receive
from tests.test_coordinator import coordinator
from tests.test_merge import route_merge
from tests.test_remote_e2e import QueueConnection, QueueSocket
from tests.test_vcs import _git_fixture
from tests.test_worker_executor import RecordingAgent, RecordingToolCaller
from tests.test_workflow_transactions import accepted_coding
from tests.worker_fakes import run_worker, server_runtime, worker_runtime


async def matching_pool(store, root, flow, monkeypatch, *, overflow=False, retain_other=True):
    """Create real directories with the controller fixture's original allocation IDs."""
    worker = store.workers.list_workers()[0]
    original = store.workers.get_flow_execution(flow.id)
    other_id = uuid4().hex
    locations = [other_id, original.workspace] if overflow else [original.workspace, other_id]
    ids = iter([worker.workspace, *locations])
    pool = RepositoryWorkspaces(root, "worker-a", limit=2)
    with monkeypatch.context() as patch:
        patch.setattr("app.worker.execution.workspaces.uuid4", lambda: UUID(next(ids)))
        await pool.provision(flow.repo)
    binding = await pool.claim(flow.repo, flow.id, original.workspace, original.generation,
                               base_branch="main", remote="origin", push_token=None)
    # Retain another real Flow on the other location, not an unreported synthetic owner.
    for item in pool.inventory():
        assert store.workers.report_workspace("worker-a", "epoch-a", item)
    if not retain_other:
        return pool, binding, None
    other = store.flows.create_flow(flow.repo, 9000, 42)
    queued = store.jobs.enqueue_job(other.id, JobKind.INITIAL)
    claimed = store.workers.assign_job("worker-a", "other-token", "epoch-a")
    assert claimed.id == queued.id and claimed.workspace == other_id
    other_binding = await pool.claim(flow.repo, other.id, claimed.workspace, claimed.generation,
                                     base_branch="main", remote="origin", push_token=None)
    assert store.workers.finish_assignment(queued.id, "worker-a", "other-token", JobState.DONE, "epoch-a")
    return pool, binding, other_binding


class ArchiveSocket(QueueSocket):
    @property
    def headers(self):
        return {"x-flowtool-worker-id": "worker-a", "authorization": "Bearer secret"}


class AckConnection(QueueConnection):
    def __init__(self, outgoing, incoming, *, drop_ack=False):
        super().__init__(outgoing, incoming)
        self.drop_ack = drop_ack

    async def recv(self):
        raw = await super().recv()
        if self.drop_ack and any(
            isinstance(item, AcceptResultResponse) for item in parse_server_message(raw).payload
        ):
            raise ConnectionError("ACK lost")
        return raw


class ObservedExecution(WorkerExecution):
    def __init__(self, archive_id, **kwargs):
        super().__init__(**kwargs)
        self.archive_id = archive_id
        self.acked = asyncio.Event()

    def on_result_accepted(self, accepted):
        super().on_result_accepted(accepted)
        if accepted.job_id == self.archive_id:
            self.acked.set()


async def connect_once(store, subject, executor, journal, *, drop_ack=False, owner=None):
    outgoing, incoming = asyncio.Queue(), asyncio.Queue()
    handler = server_runtime(
        subject,
        database=store.database,
        peers=Gateway(),
        poll_interval=60,
        tools=subject.tools,
        results=subject.results,
    )
    archive_id = next(
        job.id
        for job in store.jobs.list_jobs(journal.load().flow_id)
        if job.kind is JobKind.ARCHIVE
    )
    if owner is None:
        owner = ObservedExecution(
            archive_id=archive_id, worker_id="worker-a", executor=executor, state=journal
        )
    client = worker_runtime(
        worker_id="worker-a",
        worker_token="secret",
        serve_ws_url="ws://test",
        execution=owner,
        read_history=journal.read,
    )
    server = asyncio.create_task(
        serve_worker(
            ArchiveSocket(outgoing, incoming), worker_tokens={"worker-a": "secret"}, serve=handler.serve_worker_session
        )
    )
    worker = asyncio.create_task(
        run_worker(client, AckConnection(outgoing, incoming, drop_ack=drop_ack))
    )
    try:
        if drop_ack:
            with pytest.raises(ConnectionError, match="ACK lost"):
                await asyncio.wait_for(worker, 5)
        else:
            async with asyncio.timeout(5):
                while not (owner.acked.is_set() and journal.load() is None
                           and store.flows.get_flow(executor.state.flow_id).state is FlowState.ARCHIVED):
                    for task in (server, worker):
                        if task.done():
                            task.result()
                            raise AssertionError("connection ended before archive settlement")
                    await asyncio.sleep(0.01)
    finally:
        server.cancel()
        worker.cancel()
        await asyncio.gather(server, worker, return_exceptions=True)
        await owner.shutdown()


@pytest.mark.parametrize(
    "window",
    [
        "before-received",
        "before-delete",
        "after-delete",
        "terminal",
        "pending",
        "ack",
        "started-lost",
        "result-started-lost",
    ],
)
@pytest.mark.parametrize("overflow", [False, True])
async def test_original_worker_recovers_cleanup_and_replays_without_redeleting(
    store,
    tmp_path,
    monkeypatch,
    window,
    recording_resources,
    overflow,
):
    flow, job, task, subject = await ready_archive(
        store,
        start=window not in {"before-received", "started-lost", "result-started-lost"},
    )
    seed, _ = _git_fixture(tmp_path)
    root = tmp_path / "worker"
    managed_seed = root / "repos/owner/repo"
    managed_seed.parent.mkdir(parents=True)
    shutil.move(seed, managed_seed)
    pool, binding, other = await matching_pool(store, root, flow, monkeypatch, overflow=overflow)
    repo = pool.path(binding)
    other_repo = pool.path(other)
    journal = ExecutionState(root, flow.id)
    prior = store.jobs.list_jobs(flow.id)[-2]
    # Preserve the preceding acknowledged job, as a real worker does between assignments.
    journal.record_received(
        AssignmentArguments(
            job_id=prior.id,
            flow_id=flow.id,
            assignment_token=prior.assignment_token,
            workspace=binding.slot_id,
            generation=binding.generation,
            task=InitialTask(
                flow_id=flow.id,
                attempt_id=task.attempt_id,
                repo=flow.repo,
                issue_number=703,
                issue_title="Issue",
                issue_body="",
                branch="flow/1",
            ),
        )
    )
    journal.record_started()
    journal.bind_workspace(binding)
    terminal = decode_result_inbox(
        prior.result_payload,
        job_id=prior.id,
        assignment_token=prior.assignment_token,
        result_digest=prior.result_digest,
    ).terminal
    assert isinstance(terminal, Succeeded)
    journal.record_terminal(terminal)
    journal.acknowledge(prior.id, JobState.DONE)
    journal.reconcile_acknowledgement()
    if window != "before-received":
        journal.record_received(
            AssignmentArguments(
                job_id=job.id, flow_id=flow.id, assignment_token="archive-token", task=task,
                workspace=binding.slot_id, generation=binding.generation,
            )
        )
        journal.record_started()
    agent = RecordingAgent()
    executor = WorkerExecutor(root, agent, worker_id="worker-a", state=journal, workspaces=pool)
    lock = WorkerRootLock(root, "worker-a")
    lock.acquire()
    try:
        if window not in {"before-received", "before-delete", "started-lost"}:
            result = await executor.execute(task, tool_caller=RecordingToolCaller())
            if window in {"terminal", "pending", "ack", "result-started-lost"}:
                journal.record_terminal(
                    Succeeded(job_id=job.id, assignment_token="archive-token", result=result)
                )
            if window == "pending":
                assert receive(store, job, result) == "pending"
            if window in {"terminal", "pending", "ack", "result-started-lost"}:

                async def no_execution(*args, **kwargs):
                    raise AssertionError("a durable terminal result must only replay")

                monkeypatch.setattr(executor, "execute", no_execution)
    finally:
        lock.release()
    # Process restart reacquires the same root lock before any cleanup can resume.
    lock.acquire()
    try:
        store.workers.recover_abandoned_assignments()
        if window in {"started-lost", "result-started-lost"}:
            # The original frozen token must survive a lost Started message and expired grace.
            now = store.now()
            store._db._clock = lambda: now + timedelta(hours=1)
            store.workers.sweep_expired_assignments()
            assert store.jobs.get_job(job.id).assignment_token == "archive-token"
        await connect_once(store, subject, executor, ExecutionState(root, flow.id), drop_ack=window == "ack")
        if window == "ack":
            assert journal.load() is not None
            await connect_once(store, subject, executor, ExecutionState(root, flow.id))
    finally:
        lock.release()
    assert repo.exists() is not overflow
    assert managed_seed.exists() and other_repo.exists()
    assert not agent.started
    assert journal.load() is None
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.workers.get_flow_execution(flow.id).released_at is not None
    assert store.jobs.get_job(job.id).state is JobState.DONE
    progress = archive_progress(
        flow, store.jobs.list_jobs(flow.id), result_store=store.results, workers=store.workers
    )
    assert progress.startswith("complete at ")
    assert ("workspace retained for reuse" in progress) is not overflow
    next_flow = ControlService(
        database=store.database,
    ).on_trigger("owner/repo", 999, 42)
    default = next(item for item in pool.inventory() if item.repository is None)
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "next",
                                   workspace=default.workspace, repo_workspace_limit=2)
    for item in pool.inventory():
        assert store.workers.report_workspace("worker-a", "next", item)
    # The next INITIAL materializes a removed worktree under the higher controller generation.
    next_job = store.workers.assign_job("worker-a", "next-token", "next")
    assert next_job.flow_id == next_flow.id and next_job.kind is JobKind.INITIAL
    next_binding = await pool.claim(flow.repo, next_flow.id, next_job.workspace, next_job.generation,
                                    base_branch="main", remote="origin", push_token=None)
    assert next_binding.slot_id == binding.slot_id and next_binding.generation > binding.generation
    assert pool.path(next_binding).is_dir()


@pytest.mark.parametrize("window", ["completed-disconnected", "unstarted-rejected"])
async def test_merge_recovery_preserves_coding_completion_and_root_provenance(
    store,
    tmp_path,
    window,
    recording_resources,
    monkeypatch,
):
    old, result = accepted_coding(store, receive=False, started=window != "unstarted-rejected")
    flow = store.flows.get_flow(old.flow_id)
    await route_merge(store, flow)
    if window == "unstarted-rejected":
        retired = store.jobs.get_job(old.id)
        assert retired.state is JobState.FAILED
        assert retired.started_at is None and retired.dispatch_authorized_at is None
    archive_id = store.jobs.list_jobs(flow.id)[-1].id
    seed, _ = _git_fixture(tmp_path)
    root = tmp_path / "worker"
    managed_seed = root / "repos/owner/repo"
    managed_seed.parent.mkdir(parents=True)
    shutil.move(seed, managed_seed)
    pool, binding, _ = await matching_pool(store, root, flow, monkeypatch, retain_other=False)
    repo = pool.path(binding)
    journal = ExecutionState(root, flow.id)
    # The rejected revision has not started, but its preceding INITIAL owns this allocation.
    journal.record_received(
        AssignmentArguments(
            job_id=old.id - 1,
            flow_id=flow.id,
            assignment_token="initial-token",
            workspace=binding.slot_id,
            generation=binding.generation,
            task=InitialTask(
                flow_id=flow.id,
                attempt_id=1,
                repo=flow.repo,
                issue_number=1,
                issue_title="Issue",
                issue_body="",
                branch="flow/1",
            ),
        )
    )
    journal.record_started()
    journal.bind_workspace(binding)
    journal.record_terminal(
        Succeeded(
            job_id=old.id - 1,
            assignment_token="initial-token",
            result=InitialTaskResult(session_id="session", last_message="", exit_code=0),
        )
    )
    journal.acknowledge(old.id - 1, JobState.DONE)
    journal.reconcile_acknowledgement()
    assignment = AssignmentArguments(
        job_id=old.id,
        flow_id=flow.id,
        assignment_token=old.assignment_token,
        workspace=binding.slot_id,
        generation=binding.generation,
        task=RevisionTask.model_validate(old.payload["revision"]),
    )
    journal.record_received(assignment)

    class Executor(WorkerExecutor):
        async def execute(self, task, secret=None, *, tool_caller):
            if isinstance(task, RevisionTask):
                return result
            return await super().execute(task, secret, tool_caller=tool_caller)

    executor = Executor(root, RecordingAgent(), worker_id="worker-a", state=journal, workspaces=pool)
    owner = ObservedExecution(
        archive_id=archive_id, worker_id="worker-a", executor=executor, state=journal
    )
    if window == "completed-disconnected":
        owner.start()
        await owner.wait_terminal()
    lock = WorkerRootLock(root, "worker-a")
    lock.acquire()
    try:
        await connect_once(
            store, coordinator(store, RecordingGitHub()), executor, journal, owner=owner
        )
    finally:
        lock.release()
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.workers.get_flow_execution(flow.id).released_at is not None
    assert repo.exists() and managed_seed.exists()
