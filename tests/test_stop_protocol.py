import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.contracts.protocol import (
    Assignment,
    ExecutionStopped,
    InitialTask,
    StopAccepted,
    StopExecution,
    parse_server_message,
    parse_worker_message,
)
from app.controler.workers.conversation import ConversationBroker
from app.controler.workers.gateway import WorkerGateway
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.service import ControlService
from app.controler.workflow.tools import FlowToolService
from app.domain.models import JobKind, JobState
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.vcs import _run
from app.worker.state.session import WorkerSession
from app.worker.transport.client import WorkerClient, WorkerProtocolError
from tests.github import RecordingGitHub
from tests.test_coordinator import coordinator
from tests.test_remote_e2e import QueueConnection, QueueSocket
from tests.test_vcs import _git
from tests.test_worker_executor import RecordingAgent
from tests.test_worker_workspaces import acquire, managed_seed


def assignment():
    return Assignment(job_id=1, flow_id=1, assignment_token='token', connection_epoch='epoch',
                      task=InitialTask(flow_id=1, attempt_id=1, repo='owner/repo', issue_number=1,
                                       issue_title='test', issue_body='', branch='flow/1'))


class AckConnection:
    def __init__(self, request, lost=False):
        self.request, self.lost, self.sent = request, lost, []
    async def send(self, raw): self.sent.append(parse_worker_message(raw))
    async def recv(self):
        if self.lost: raise ConnectionError('ack lost')
        return StopAccepted(**self.request.model_dump(exclude={'type'})).model_dump_json()


async def test_stop_kills_owned_process_and_child_then_replays_lost_ack(tmp_path):
    work = assignment()
    journal = WorkerSession(tmp_path)
    journal.record_received(work)
    journal.record_started()
    ready = tmp_path / 'pids'
    script = ("import os,subprocess,sys,time; from pathlib import Path; "
              "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
              "Path(sys.argv[1]).write_text(str(os.getpid())+' '+str(child.pid)); time.sleep(60)")
    execution = asyncio.create_task(_run(sys.executable, '-c', script, str(ready)))
    try:
        async with asyncio.timeout(5):
            while not ready.exists(): await asyncio.sleep(0.01)
        client = WorkerClient(worker_id='w', worker_token='secret', serve_ws_url='unused',
                              executor=SimpleNamespace(reclaim_workspace=AsyncMock()), session=journal)
        client.session.assignment, client._execution, client._connection_epoch = work, execution, 'epoch'
        request = StopExecution(job_id=1, flow_id=1, assignment_token='token', connection_epoch='epoch', request_id='stop')
        with pytest.raises(ConnectionError): await client._handle_stop(AckConnection(request, lost=True), request)
        assert execution.cancelled()
        assert journal.load().stopped is not None
        assert not journal.load().acknowledged
        # Both processes must actually be gone, not just absent from the asyncio task table.
        async with asyncio.timeout(5):
            for pid in map(int, ready.read_text().split()):
                while True:
                    try: os.kill(pid, 0)
                    except ProcessLookupError: break
                    await asyncio.sleep(0.01)
        restarted = WorkerClient(worker_id='w', worker_token='secret', serve_ws_url='unused',
                                 executor=SimpleNamespace(reclaim_workspace=AsyncMock()), session=WorkerSession(tmp_path))
        request = request.model_copy(update={'connection_epoch': 'new'})
        restarted._connection_epoch = 'new'
        connection = AckConnection(request)
        await restarted._handle_stop(connection, request)
        assert isinstance(connection.sent[0], ExecutionStopped)
        assert journal.load().acknowledged
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


async def test_restarted_worker_does_not_infer_stopped_from_absent_task(tmp_path):
    work = assignment()
    journal = WorkerSession(tmp_path)
    journal.record_received(work)
    journal.record_started()
    client = WorkerClient(worker_id='w', worker_token='secret', serve_ws_url='unused',
                          executor=SimpleNamespace(), session=journal)
    client._connection_epoch = 'epoch'
    request = StopExecution(job_id=1, flow_id=1, assignment_token='token', connection_epoch='epoch', request_id='stop')
    with pytest.raises(WorkerProtocolError, match='ownership is unknown'):
        await client._handle_stop(AckConnection(request), request)
    assert journal.load().stopped is None


@pytest.mark.parametrize("overflow", [False, True])
async def test_gateway_and_worker_cancel_over_duplex_protocol(store, tmp_path, overflow):
    # Two cancellations used to keep both pool positions occupied, blocking the third Flow.
    root, _ = managed_seed(tmp_path)
    started = asyncio.Event()
    to_worker, to_serve = asyncio.Queue(), asyncio.Queue()

    class Connection(QueueConnection):
        async def send(self, raw):
            if isinstance(parse_worker_message(raw), ExecutionStopped):
                # Cleanup must finish before control can release the binding and dispatch again.
                repo = executor.workspaces.path(session.load().workspace)
                assert repo.is_dir() and (repo / 'README.md').read_text() == 'base\n'
                assert not (repo / 'untracked').exists()
                assert store.workers.get_flow_execution(flow.id).released_at is None
            await super().send(raw)

    class Executor(WorkerExecutor):
        async def execute(self, task, secret=None, *, tool_caller=None):
            binding = await acquire(self.workspaces, task.flow_id)
            self.session.bind_workspace(binding)
            path = self.workspaces.path(binding)
            (path / 'README.md').write_text('cancelled staged edit')
            _git('add', 'README.md', cwd=path)
            (path / 'README.md').write_text('cancelled tracked edit')
            (path / 'untracked').write_text('cancelled untracked edit')
            (path / '.venv').mkdir(exist_ok=True)
            (path / '.venv/cache').write_text('cancelled ignored cache')
            started.set()
            await asyncio.Event().wait()

    class Coordinator:
        async def prepare_for_assignment(self, job, worker, token, epoch):
            task = InitialTask(flow_id=flow.id, attempt_id=attempt.id, repo=flow.repo,
                               issue_number=1, issue_title='test', issue_body='', branch='flow/1')
            return Assignment(job_id=job.id, flow_id=job.flow_id, assignment_token=token,
                              connection_epoch=epoch, task=task)
        def mark_started(self, job, task, worker_id): pass
        def worker_connected(self, *args): pass
        def worker_disconnected(self, *args): pass

    gateway_coordinator = Coordinator()
    gateway = WorkerGateway(
        gateway_coordinator, database=store.database,
        worker_tokens={"worker-1": "worker-secret"},
        heartbeat_interval=0.01,
        poll_interval=0.01,
        tools=Mock(spec=FlowToolService),
        results=Mock(spec=ResultApplication),
        conversations=ConversationBroker(),
    )
    session = WorkerSession(root)
    executor = Executor(root, RecordingAgent(), worker_id='worker-1', session=session)
    unrelated = await acquire(executor.workspaces, 999) if overflow else None
    client = WorkerClient(worker_id='worker-1', worker_token='worker-secret', serve_ws_url='unused',
                          executor=executor, session=session)
    server = asyncio.create_task(gateway.handle(QueueSocket(to_serve, to_worker)))
    worker = asyncio.create_task(client.run_connection(Connection(to_serve, to_worker)))
    allocations = []
    try:
        async with asyncio.timeout(10):
            for issue_number in (1, 2):
                flow = store.flows.create_flow('owner/repo', issue_number, 1)
                attempt = store.flows.create_attempt(flow.id)
                flow.current_attempt_id = attempt.id
                store.flows.update_flow(flow)
                store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
                started.clear()
                gateway.notify_jobs()
                while not started.is_set():
                    if server.done(): server.result()
                    if worker.done(): worker.result()
                    await asyncio.sleep(0.01)
                binding = session.load().workspace
                path = executor.workspaces.path(binding)
                allocations.append((binding, path.stat().st_ino))
                ControlService(jobs_available=gateway.notify_jobs, database=store.database).cancel(flow)
                while not session.load().acknowledged:
                    if server.done(): server.result()
                    if worker.done(): worker.result()
                    await asyncio.sleep(0.01)
                assert (path / '.venv/cache').read_text() == 'cancelled ignored cache'
                assert store.stops.pending('worker-1') is None
        replacement = await acquire(executor.workspaces, 3)
        assert replacement.slot_id == allocations[0][0].slot_id == allocations[1][0].slot_id
        assert replacement.generation > allocations[1][0].generation > allocations[0][0].generation
        assert executor.workspaces.path(replacement).stat().st_ino == allocations[0][1] == allocations[1][1]
        assert (root / 'repos/owner/repo/README.md').read_text() == 'base\n'
        if unrelated is not None:
            assert executor.workspaces.lookup('owner/repo', 999) == unrelated
    finally:
        server.cancel(); worker.cancel()
        await asyncio.gather(server, worker, return_exceptions=True)
        await client._cancel_execution()


@pytest.mark.parametrize("failure", ["cleanup", "lost-ack"])
async def test_stop_reconnect_reconciles_successor(store, tmp_path, monkeypatch, recording_resources, failure):
    # A lost stop ACK can leave an older journal behind a newer authorized dispatch.
    flows, attempts, jobs = [], {}, []
    for issue in (1, 2):
        flow = store.flows.create_flow("owner/repo", issue, 1)
        attempt = store.flows.create_attempt(flow.id)
        flow.current_attempt_id = attempt.id
        store.flows.update_flow(flow)
        flows.append(flow)
        attempts[flow.id] = attempt
        jobs.append(store.jobs.enqueue_job(flow.id, JobKind.INITIAL))
    started, successor_dispatched = asyncio.Event(), asyncio.Event()
    subject = coordinator(store, RecordingGitHub())

    async def prepare(job, worker_id, token, epoch):
        return Assignment(job_id=job.id, flow_id=job.flow_id, assignment_token=token,
                          connection_epoch=epoch, task=InitialTask(
                              flow_id=job.flow_id, attempt_id=attempts[job.flow_id].id,
                              repo="owner/repo", issue_number=job.flow_id, issue_title="test",
                              issue_body="", branch="flow/test"))

    monkeypatch.setattr(subject, "prepare_for_assignment", prepare)

    class Executor:
        def __init__(self):
            self.executed, self.cleanups = [], 0

        async def execute(self, task, secret=None, *, tool_caller=None):
            self.executed.append(task.flow_id)
            started.set()
            await asyncio.Event().wait()

        async def reclaim_workspace(self):
            self.cleanups += 1
            if failure == "cleanup" and self.cleanups == 1:
                raise OSError("temporary cleanup failure")

    gateway = WorkerGateway(subject, database=store.database,
                            worker_tokens={"worker-1": "worker-secret"}, heartbeat_interval=.01,
                            poll_interval=.01, tools=subject.tools, results=subject.results,
                            conversations=ConversationBroker())
    session = WorkerSession(tmp_path)
    executor = Executor()
    client = WorkerClient(worker_id="worker-1", worker_token="worker-secret", serve_ws_url="unused",
                          executor=executor, session=session)

    class Socket(QueueSocket):
        async def send_text(self, raw):
            await super().send_text(raw)
            message = parse_server_message(raw)
            if isinstance(message, Assignment) and message.flow_id == flows[1].id:
                successor_dispatched.set()

    class LostAckConnection(QueueConnection):
        async def recv(self):
            raw = await super().recv()
            if isinstance(parse_server_message(raw), StopAccepted):
                await successor_dispatched.wait()
                raise ConnectionError("stop ACK lost after successor dispatch")
            return raw

    def connect(connection_type):
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        return (asyncio.create_task(gateway.handle(Socket(incoming, outgoing))),
                asyncio.create_task(client.run_connection(connection_type(incoming, outgoing))))

    server, worker = connect(LostAckConnection if failure == "lost-ack" else QueueConnection)
    try:
        async with asyncio.timeout(5):
            await started.wait()
            ControlService(database=store.database, jobs_available=gateway.notify_jobs).cancel(flows[0])
            with pytest.raises((OSError, ConnectionError), match="cleanup failure|ACK lost"):
                await worker
        assert not session.load().acknowledged
        assert store.jobs.get_job(jobs[1].id).started_at is None
        assert (store.stops.pending("worker-1") is not None) == (failure == "cleanup")
    finally:
        server.cancel(); worker.cancel()
        await asyncio.gather(server, worker, return_exceptions=True)
    started.clear()
    server, worker = connect(QueueConnection)
    try:
        async with asyncio.timeout(5):
            while not (
                store.jobs.get_job(jobs[1].id).state is JobState.FAILED
                if failure == "lost-ack" else started.is_set()
            ):
                if server.done(): server.result()
                if worker.done(): worker.result()
                await asyncio.sleep(.01)
        successor = store.jobs.get_job(jobs[1].id)
        if failure == "cleanup":
            assert successor.started_at is not None and executor.executed == [f.id for f in flows]
        else:
            assert session.load().acknowledged
            assert successor.result_applied_at is not None and successor.started_at is None
            assert "journal" in successor.last_error
            assert executor.executed == [flows[0].id]  # Unknown dispatch is failed, never rerun.
    finally:
        server.cancel(); worker.cancel()
        await asyncio.gather(server, worker, return_exceptions=True)
        await client._cancel_execution()
