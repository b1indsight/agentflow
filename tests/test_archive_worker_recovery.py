"""Real worker directories and PostgreSQL across the deletion/result/ACK crash windows."""

import asyncio
import json
import shutil
from datetime import timedelta

import pytest

from app.contracts.protocol import (
    PROTOCOL_VERSION,
    Assignment,
    InitialTask,
    ResultAccepted,
    RevisionTask,
    Succeeded,
    parse_server_message,
    parse_worker_message,
)
from app.controler.workers.gateway import WorkerGateway
from app.controler.workflow.service import ControlService
from app.domain.models import FlowState, JobKind, JobState
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.vcs import prepare_flow_repo
from app.worker.runtime import WorkerRootLock
from app.worker.state.session import WorkerSession
from app.worker.transport.client import WorkerClient
from tests.github import RecordingGitHub
from tests.test_archive_recovery import ready_archive, receive
from tests.test_coordinator import coordinator
from tests.test_merge import route_merge
from tests.test_remote_e2e import QueueConnection, QueueSocket
from tests.test_vcs import _git_fixture
from tests.test_worker_executor import RecordingAgent, RecordingToolCaller
from tests.test_workflow_transactions import accepted_coding


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
        if isinstance(parse_server_message(raw), ResultAccepted) and self.drop_ack:
            raise ConnectionError("ACK lost")
        return raw


class ObservedClient(WorkerClient):
    def __init__(self, archive_id, **kwargs):
        super().__init__(**kwargs)
        self.archive_id = archive_id
        self.acked = asyncio.Event()

    def _acknowledge(self, accepted):
        super()._acknowledge(accepted)
        if accepted.job_id == self.archive_id:
            self.acked.set()


async def connect_once(store, subject, executor, journal, *, drop_ack=False, client=None):
    outgoing, incoming = asyncio.Queue(), asyncio.Queue()
    gateway = WorkerGateway(store, subject, worker_tokens={"worker-a": "secret"}, poll_interval=60)
    archive_id = next(job.id for job in store.list_jobs(journal.load().flow_id)
                      if job.kind is JobKind.ARCHIVE)
    if client is None:
        client = ObservedClient(archive_id=archive_id,
                                worker_id="worker-a", worker_token="secret", serve_ws_url="ws://test",
                                executor=executor, session=journal)
    server = asyncio.create_task(gateway.handle(ArchiveSocket(outgoing, incoming)))
    worker = asyncio.create_task(client.run_connection(AckConnection(outgoing, incoming,
                                                                     drop_ack=drop_ack)))
    try:
        if drop_ack:
            with pytest.raises(ConnectionError, match="ACK lost"):
                await asyncio.wait_for(worker, 5)
        else:
            await asyncio.wait_for(client.acked.wait(), 5)
    finally:
        server.cancel()
        worker.cancel()
        await asyncio.gather(server, worker, return_exceptions=True)
        await client._cancel_execution()
    return gateway


@pytest.mark.parametrize("window", ["before-received", "before-delete", "after-delete",
                                   "terminal", "pending", "ack", "started-lost", "result-started-lost"])
async def test_original_worker_recovers_cleanup_and_replays_without_redeleting(
    store, tmp_path, monkeypatch, window, recording_resources,
):
    flow, job, task, subject = await ready_archive(
        store, start=window not in {"before-received", "started-lost", "result-started-lost"},
    )
    seed, _ = _git_fixture(tmp_path)
    root = tmp_path / "worker"
    managed_seed = root / "repos/owner/repo"
    managed_seed.parent.mkdir(parents=True)
    shutil.move(seed, managed_seed)
    repo = await prepare_flow_repo(managed_seed, flow.id)
    other_repo = await prepare_flow_repo(managed_seed, 999)
    journal = WorkerSession(root)
    prior = store.list_jobs(flow.id)[-2]
    # Preserve the preceding acknowledged job, as a real worker does between assignments.
    journal.record_received(Assignment(
        job_id=prior.id, flow_id=flow.id, assignment_token=prior.assignment_token,
        connection_epoch="epoch-a", task=InitialTask(flow_id=flow.id, attempt_id=task.attempt_id,
            repo=flow.repo, issue_number=703, issue_title="Issue", issue_body="", branch="flow/1"),
    ))
    journal.record_started()
    terminal = parse_worker_message(json.dumps(prior.result_payload))
    assert isinstance(terminal, Succeeded)
    journal.record_terminal(terminal)
    journal.acknowledge(prior.id, JobState.DONE)
    if window != "before-received":
        journal.record_received(Assignment(job_id=job.id, flow_id=flow.id,
            assignment_token="archive-token", connection_epoch="epoch-a", task=task))
        journal.record_started()
    agent = RecordingAgent()
    executor = WorkerExecutor(root, agent, worker_id="worker-a", session=journal)
    lock = WorkerRootLock(root, "worker-a")
    lock.acquire()
    try:
        if window not in {"before-received", "before-delete", "started-lost"}:
            result = await executor.execute(task, tool_caller=RecordingToolCaller())
            if window in {"terminal", "pending", "ack", "result-started-lost"}:
                journal.record_terminal(Succeeded(job_id=job.id, assignment_token="archive-token",
                    connection_epoch="epoch-a", result=result))
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
        store.recover_abandoned_assignments()
        if window in {"started-lost", "result-started-lost"}:
            # The original frozen token must survive a lost Started frame and expired grace.
            now = store.now()
            store._clock = lambda: now + timedelta(hours=1)
            store.sweep_expired_assignments()
            assert store.get_job(job.id).assignment_token == "archive-token"
        await connect_once(store, subject, executor, WorkerSession(root), drop_ack=window == "ack")
        if window == "ack":
            assert journal.load() is not None
            await connect_once(store, subject, executor, WorkerSession(root))
    finally:
        lock.release()
    assert not repo.exists() and managed_seed.exists() and other_repo.exists()
    assert not agent.started
    assert journal.load() is None
    assert store.get_flow(flow.id).state is FlowState.SUCCEEDED
    assert store.get_flow_execution(flow.id).released_at is not None
    assert store.get_job(job.id).state is JobState.DONE
    next_flow = ControlService(store).on_trigger("owner/repo", 999, 42)
    store.worker_connected("worker-a", PROTOCOL_VERSION, "next")
    next_job = store.assign_job("worker-a", "next-token", "next")
    assert next_job.flow_id == next_flow.id and next_job.kind is JobKind.INITIAL


@pytest.mark.parametrize("window", ["completed-disconnected", "unstarted-rejected"])
async def test_merge_recovery_preserves_coding_completion_and_root_provenance(
    store, tmp_path, window, recording_resources,
):
    old, result = accepted_coding(store, receive=False, started=window != "unstarted-rejected")
    flow = store.get_flow(old.flow_id)
    await route_merge(store, flow)
    archive_id = store.list_jobs(flow.id)[-1].id
    seed, _ = _git_fixture(tmp_path)
    root = tmp_path / "worker"
    managed_seed = root / "repos/owner/repo"
    managed_seed.parent.mkdir(parents=True)
    shutil.move(seed, managed_seed)
    repo = await prepare_flow_repo(managed_seed, flow.id)
    journal = WorkerSession(root)
    assignment = Assignment(job_id=old.id, flow_id=flow.id, assignment_token=old.assignment_token,
        connection_epoch="epoch-a", task=RevisionTask.model_validate(old.payload["revision"]))
    journal.record_received(assignment)
    executor = WorkerExecutor(root, RecordingAgent(), worker_id="worker-a",
                              session=journal)
    client = ObservedClient(archive_id=archive_id, worker_id="worker-a", worker_token="secret",
                            serve_ws_url="ws://test", executor=executor, session=journal)
    if window == "completed-disconnected":
        journal.record_started()
        client.session.assignment = assignment
        client._execution = asyncio.create_task(asyncio.sleep(0, result=result))
        await client._execution
    lock = WorkerRootLock(root, "worker-a")
    lock.acquire()
    try:
        await connect_once(store, coordinator(store, RecordingGitHub()), executor, journal, client=client)
    finally:
        lock.release()
    assert store.get_flow(flow.id).state is FlowState.SUCCEEDED
    assert store.get_flow_execution(flow.id).released_at is not None
    assert not repo.exists() and managed_seed.exists()
