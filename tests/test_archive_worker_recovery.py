"""Real worker directories and PostgreSQL across merge, original stop, and ACK."""

import asyncio
import shutil
from uuid import UUID, uuid4

from app.contracts.protocol import (
    AssignmentArguments,
    InitialTask,
    InitialTaskResult,
    Registered,
    RevisionTask,
    StopAccepted,
    Succeeded,
    parse_server_message,
)
from app.controler.workers.transport import serve_worker
from app.domain.models import ArchiveReason, FlowState, JobKind, JobState
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.execution.workspaces import RepositoryWorkspaces
from app.worker.runtime import WorkerRootLock
from app.worker.state.execution import ExecutionState
from tests.github import RecordingGitHub
from tests.test_merge import route_merge
from tests.test_remote_e2e import QueueConnection, QueueSocket
from tests.test_vcs import _git_fixture
from tests.test_worker_executor import RecordingAgent
from tests.test_workflow_transactions import accepted_coding
from tests.worker_fixtures import run_worker, worker_runtime


async def matching_pool(store, root, flow, monkeypatch):
    """Create real directories with the controller fixture's original allocation IDs."""
    worker = store.workers.list_workers()[0]
    original = store.workers.get_flow_execution(flow.id)
    other_id = uuid4().hex
    ids = iter([worker.workspace, original.workspace, other_id])
    pool = RepositoryWorkspaces(root, "worker-a", limit=2)
    with monkeypatch.context() as patch:
        patch.setattr("app.worker.execution.workspaces.uuid4", lambda: UUID(next(ids)))
        await pool.provision(flow.repo)
    binding = await pool.claim(flow.repo, flow.id, original.workspace, original.generation,
                               base_branch="main", remote="origin", push_token=None)
    # Retain another real Flow on the other location, not an unreported synthetic owner.
    for item in pool.inventory():
        assert store.workers.report_workspace("worker-a", "epoch-a", item)
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


async def test_modern_merge_stops_original_worker_before_reusing_only_its_generation(
    store, tmp_path, monkeypatch, recording_resources, server_runtime,
):
    # The server's exact stop receipt releases occupancy even if its reply is held.
    # Reuse cleans only that generation, never the sibling's checkout or journal.
    job, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(job.flow_id)
    seed, _ = _git_fixture(tmp_path)
    root = tmp_path / "worker"
    managed_seed = root / "repos/owner/repo"
    managed_seed.parent.mkdir(parents=True)
    shutil.move(seed, managed_seed)
    lock = WorkerRootLock(root, "worker-a")
    lock.acquire()
    server = worker = None
    owner = None
    try:
        pool, binding, other = await matching_pool(store, root, flow, monkeypatch)
        repo, other_repo = pool.path(binding), pool.path(other)
        baseline = (repo / "README.md").read_bytes()
        (repo / "README.md").write_text("unfinished tracked edit")
        dirty = repo / "coding-output"
        dirty.mkdir()
        (dirty / "unfinished.txt").write_text("uncommitted work")
        sibling_file = other_repo / "sibling-owned.txt"
        sibling_file.write_text("keep this work")
        sibling_job = store.jobs.list_jobs(other.flow_id)[-1]
        sibling = ExecutionState(root, other.flow_id)
        sibling.record_received(AssignmentArguments(
            job_id=sibling_job.id, flow_id=other.flow_id, assignment_token="other-token",
            workspace=other.slot_id, generation=other.generation,
            task=InitialTask(
                flow_id=other.flow_id, attempt_id=1, repo=flow.repo, issue_number=9000,
                issue_title="Sibling", issue_body="", branch="flow/sibling",
            ),
        ))
        sibling.record_started()
        sibling.bind_workspace(other)
        sibling.record_terminal(Succeeded(
            job_id=sibling_job.id, assignment_token="other-token",
            result=InitialTaskResult(session_id="sibling-session", last_message="", exit_code=0),
        ))
        sibling.acknowledge(sibling_job.id, JobState.DONE)
        sibling.reconcile_acknowledgement()
        for item in pool.inventory():
            assert store.workers.report_workspace("worker-a", "epoch-a", item)
        sibling_bytes = sibling.path.read_bytes()
        sibling_inventory = next(item for item in pool.inventory() if item.workspace == other.slot_id)
        sibling_binding = store.workers.get_flow_execution(other.flow_id)
        journal = ExecutionState(root, flow.id)
        journal.record_received(AssignmentArguments(
            job_id=job.id, flow_id=flow.id, assignment_token=job.assignment_token,
            workspace=binding.slot_id, generation=binding.generation,
            task=RevisionTask.model_validate(job.payload["revision"]),
        ))
        started, cancelled = asyncio.Event(), asyncio.Event()

        class Executor(WorkerExecutor):
            async def execute(self, task, secret=None, *, tool_caller):
                self.state.bind_workspace(pool.lookup(task.repo, task.flow_id))
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        executor = Executor(root, RecordingAgent(), worker_id="worker-a", state=journal, workspaces=pool)
        owner = WorkerExecution(worker_id="worker-a", executor=executor, state=journal)
        client = await worker_runtime(
            worker_token="secret",
            execution=owner, read_history=journal.read,
        )
        registered, ack_waiting, deliver_ack = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Connection(QueueConnection):
            async def recv(self):
                raw = await super().recv()
                message = parse_server_message(raw)
                if isinstance(message, Registered) and message.ready:
                    registered.set()
                if any(isinstance(item, StopAccepted) for item in message.payload):
                    ack_waiting.set()
                    await deliver_ack.wait()
                return raw

        handler = server_runtime(
            github=RecordingGitHub(), settings={"worker_poll_interval": 60},
        )
        outgoing, incoming = asyncio.Queue(), asyncio.Queue()
        owner.start()
        server = asyncio.create_task(serve_worker(
            ArchiveSocket(outgoing, incoming), worker_tokens={"worker-a": "secret"},
            serve=handler.serve_worker_session,
        ))
        worker = asyncio.create_task(run_worker(client, Connection(outgoing, incoming)))
        async with asyncio.timeout(5):
            await started.wait()
            await registered.wait()
            await route_merge(store, flow)
            archived = store.flows.get_flow(flow.id)
            assert (archived.state, archived.archive_reason) == (
                FlowState.ARCHIVED, ArchiveReason.SUCCEEDED,
            )
            assert owner.lease() is not None and not cancelled.is_set() and dirty.exists()
            stop = store.stops.pending("worker-a")
            assert (stop.job_id, stop.assignment_token, stop.workspace, stop.generation) == (
                job.id, job.assignment_token, binding.slot_id, binding.generation,
            )
            notice = store.jobs.list_jobs(flow.id)[-1]
            assert notice.kind is JobKind.ARCHIVE and notice.assigned_worker_id is None
            assert notice.assignment_token is None
            assert notice.result_payload["terminal"]["type"] == "merge_archive"
            handler.gateway.wake_all()
            await ack_waiting.wait()
            stopped = journal.load()
            assert cancelled.is_set() and owner.lease() is None
            assert stopped.stopped.request_id == stop.request_id and not stopped.acknowledged
            assert dirty.exists() and (repo / "README.md").read_text() == "unfinished tracked edit"
            assert store.stops.receipt_request("worker-a", stop.request_id).state == "confirmed"
            assert store.workers.get_flow_execution(flow.id).released_at is not None
            released = next(item for item in store.workers.list_workspaces("worker-a")
                            if item.workspace == binding.slot_id)
            assert (released.current_job_id, released.generation) == (None, binding.generation + 1)
            deliver_ack.set()
            while not journal.load().acknowledged:
                for task in (server, worker):
                    if task.done():
                        task.result()
                        raise AssertionError("connection ended before stop acknowledgement")
                await asyncio.sleep(0.01)
        settled = next(item for item in pool.inventory() if item.workspace == binding.slot_id)
        assert settled.flow_id == flow.id and settled.generation == binding.generation
        assert store.flows.get_flow(flow.id) == archived
        assert store.jobs.get_job(notice.id).result_applied_at is None
        assert managed_seed.exists() and other_repo.exists()
        # No inventory release or recovery clock: the exact stop already enabled reuse.
        successor = store.flows.create_flow(flow.repo, 999, 42)
        queued = store.jobs.enqueue_job(successor.id, JobKind.INITIAL)
        epoch = store.workers.list_workers()[0].connection_epoch
        replacement = store.workers.assign_job("worker-a", "next-generation", epoch)
        assert replacement.id == queued.id and replacement.workspace == binding.slot_id
        assert replacement.generation == binding.generation + 1
        reused = await pool.claim(
            flow.repo, successor.id, replacement.workspace, replacement.generation,
            base_branch="main", remote="origin", push_token=None,
        )
        assert pool.path(reused) == repo and reused.generation == binding.generation + 1
        assert repo.exists() and not dirty.exists()
        assert (repo / "README.md").read_bytes() == baseline
        assert sibling_file.read_text() == "keep this work" and sibling.path.read_bytes() == sibling_bytes
        assert next(item for item in pool.inventory() if item.workspace == other.slot_id) == sibling_inventory
        assert pool.lookup(flow.repo, other.flow_id) == other
        assert store.workers.get_flow_execution(other.flow_id) == sibling_binding
        assert store.jobs.get_job(sibling_job.id) == sibling_job
    finally:
        tasks = [task for task in (server, worker) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if owner is not None:
            await owner.shutdown()
        lock.release()
