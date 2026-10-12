import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

from app.contracts.protocol import (
    Assignment,
    ExecutionStopped,
    InitialTask,
    InitialTaskResult,
    StopAccepted,
    StopExecution,
    parse_worker_message,
)
from app.control.gateway import WorkerGateway
from app.control.service import ControlService
from app.domain.models import JobKind
from app.persistence.stop_store import StopStore
from app.worker.execution.vcs import _run
from app.worker.state.session import WorkerSession
from app.worker.transport.client import WorkerClient, WorkerProtocolError


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
                              executor=SimpleNamespace(), session=journal)
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
                                 executor=SimpleNamespace(), session=WorkerSession(tmp_path))
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


async def test_gateway_and_worker_cancel_over_duplex_protocol(store, tmp_path):
    flow = store.create_flow('owner/repo', 1, 1)
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    store.enqueue_job(flow.id, JobKind.INITIAL)
    started, ended = asyncio.Event(), asyncio.Event()
    to_worker, to_serve = asyncio.Queue(), asyncio.Queue()

    class Socket:
        def __init__(self):
            self.headers = {'authorization': 'Bearer secret', 'x-flowtool-worker-id': 'w'}
        async def accept(self): pass
        async def close(self, code=1000): await to_worker.put(ConnectionError('closed'))
        async def send_text(self, raw): await to_worker.put(raw)
        async def receive_text(self): return await to_serve.get()

    class Connection:
        async def send(self, raw): await to_serve.put(raw)
        async def recv(self):
            raw = await to_worker.get()
            if isinstance(raw, Exception): raise raw
            return raw

    class Executor:
        async def execute(self, task, secret=None, *, tool_caller=None):
            started.set()
            try: await asyncio.Event().wait()
            finally: ended.set()
            return InitialTaskResult(session_id='unused', last_message='', exit_code=0)

    class Coordinator:
        async def prepare_for_assignment(self, job, worker, token, epoch):
            task = InitialTask(flow_id=flow.id, attempt_id=attempt.id, repo=flow.repo,
                               issue_number=1, issue_title='test', issue_body='', branch='flow/1')
            return Assignment(job_id=job.id, flow_id=job.flow_id, assignment_token=token,
                              connection_epoch=epoch, task=task)
        def mark_started(self, job, task, worker_id): pass
        def worker_connected(self, *args): pass
        def worker_disconnected(self, *args): pass

    gateway = WorkerGateway(store, Coordinator(), worker_tokens={'w': 'secret'},
                            heartbeat_interval=0.01, poll_interval=0.01, execution_control=StopStore(store))
    client = WorkerClient(worker_id='w', worker_token='secret', serve_ws_url='unused',
                          executor=Executor(), session=WorkerSession(tmp_path))
    server = asyncio.create_task(gateway.handle(Socket()))
    worker = asyncio.create_task(client.run_connection(Connection()))
    try:
        async with asyncio.timeout(3):
            while not started.is_set():
                if server.done(): server.result()
                if worker.done(): worker.result()
                await asyncio.sleep(0.01)
            ControlService(store, jobs_available=gateway.notify_jobs).cancel(flow)
            while store.get_flow_execution(flow.id).released_at is None:
                if server.done(): server.result()
                if worker.done(): worker.result()
                await asyncio.sleep(0.01)
        assert ended.is_set()
        assert StopStore(store).pending('w') is None
    finally:
        server.cancel(); worker.cancel()
        await asyncio.gather(server, worker, return_exceptions=True)
        await client._cancel_execution()
