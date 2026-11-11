import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.communication.gateway import Gateway
from app.communication.session import Session
from app.contracts.protocol import (
    PROTOCOL_VERSION,
    AcceptResultResponse,
    AcceptStop,
    AcceptStopArguments,
    Assignment,
    AssignmentArguments,
    ExecutionStopped,
    Failed,
    FlowToolArguments,
    FlowToolRequest,
    Heartbeat,
    InitialTask,
    InitialTaskResult,
    RecoveryDescriptor,
    Registered,
    ServerMessage,
    StopAccepted,
    StopExecution,
    StopFence,
    StopResult,
    accept_stop_id,
    call_header,
    message_header,
    parse_server_message,
    parse_worker_message,
    result_header,
    stop_call_id,
    stop_receipt_id,
    terminal_digest,
)
from app.controler.workers.transport import serve_worker
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.service import ControlService
from app.controler.workflow.tools import FlowToolService
from app.domain.models import JobKind, JobState, VcsKind, utcnow
from app.persistence.stops import StopIntent
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.lifecycle import WorkerExecution, WorkerProtocolError
from app.worker.execution.vcs import _run
from app.worker.execution.workspaces import RepositoryWorkspaces
from app.worker.state.execution import ExecutionState
from tests.github import RecordingGitHub
from tests.test_coordinator import coordinator
from tests.test_remote_e2e import QueueConnection, QueueSocket
from tests.test_vcs import _git
from tests.test_worker_executor import RecordingAgent
from tests.test_worker_handler import (
    RecordingStore,
    ScriptedSocket,
    handler_for,
    register_message,
    sent_items,
    terminal_items,
    worker_message,
)
from tests.test_worker_workspaces import managed_seed
from tests.worker_fakes import NoPendingStops, run_worker, server_runtime, worker_runtime


def assignment():
    return AssignmentArguments(
        job_id=1,
        flow_id=1,
        assignment_token="token",
        workspace="a" * 32,
        generation=1,
        task=InitialTask(
            flow_id=1,
            attempt_id=1,
            repo="owner/repo",
            issue_number=1,
            issue_title="test",
            issue_body="",
            branch="flow/1",
        ),
    )


class AckConnection:
    def __init__(self, request, lost=False, epoch="epoch"):
        self.request, self.lost, self.sent = request, lost, []
        self.epoch, self.messages = epoch, []
        self.receipt = asyncio.Event()
        self.step = 0

    async def close(self, code=1000):
        pass

    async def send(self, raw):
        message = parse_worker_message(raw)
        self.messages.append(message)
        self.sent.extend(message.payload)
        if any(isinstance(item, StopResult) for item in message.payload):
            self.receipt.set()

    async def recv(self):
        self.step += 1
        if self.step == 1:
            call = StopExecution(
                **call_header(stop_call_id(self.request)),
                method="worker.stop",
                arguments=self.request,
            )
            return ServerMessage(
                **message_header(self.epoch), control=None, payload=[call]
            ).model_dump_json()
        if self.step == 2:
            await self.receipt.wait()
            if self.lost:
                raise ConnectionError("ack lost")
            call = next(item for item in self.sent if isinstance(item, AcceptStop))
            result = StopAccepted(
                **result_header(call.message_id), method="server.accept_stop", result=self.request
            )
            return ServerMessage(
                **message_header(self.epoch), control=None, payload=[result]
            ).model_dump_json()
        if self.step == 3:
            return Registered(
                **message_header(self.epoch),
                control="registered",
                payload=[],
                recoveries=[],
                ready=True,
                heartbeat_interval=60,
            ).model_dump_json()
        raise ConnectionError("script complete")


async def test_stop_kills_owned_process_and_child_then_replays_lost_ack(tmp_path):
    work = assignment()
    journal = ExecutionState(tmp_path, 1)
    ready = tmp_path / "pids"
    script = (
        "import os,subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "Path(sys.argv[1]).write_text(str(os.getpid())+' '+str(child.pid)); time.sleep(60)"
    )

    class Executor:
        async def execute(self, task, secret=None, *, tool_caller):
            await _run(sys.executable, "-c", script, str(ready))
            raise AssertionError("process should be cancelled")

        async def reclaim_workspace(self):
            pass

    owner = WorkerExecution(worker_id="w", executor=Executor(), state=journal)
    owner.receive(work)
    owner.start()
    try:
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(0.01)
        client = worker_runtime(
            worker_id="w",
            worker_token="secret",
            serve_ws_url="unused",
            execution=owner,
            read_history=journal.read,
        )
        request = StopFence(job_id=1, flow_id=1, assignment_token="token", request_id="stop")
        with pytest.raises(ConnectionError, match="ack lost"):
            await run_worker(client, AckConnection(request, lost=True))
        assert journal.load().stopped is not None
        assert not journal.load().acknowledged
        # Both processes must actually be gone, not just absent from the asyncio task table.
        async with asyncio.timeout(5):
            for pid in map(int, ready.read_text().split()):
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.01)
        restarted_state = ExecutionState(tmp_path, 1)
        restarted = worker_runtime(
            worker_id="w",
            worker_token="secret",
            serve_ws_url="unused",
            execution=WorkerExecution(
                worker_id="w",
                state=restarted_state,
                executor=SimpleNamespace(reclaim_workspace=AsyncMock()),
            ),
            read_history=restarted_state.read,
        )
        connection = AckConnection(request, epoch="new")
        with pytest.raises(ConnectionError, match="script complete"):
            await run_worker(restarted, connection)
        assert any(isinstance(message, StopResult) for message in connection.sent)
        assert journal.load().acknowledged
    finally:
        await owner.shutdown()


async def test_replacing_client_during_stop_joins_executor_and_preserves_original_receipt(tmp_path):
    # Disconnect while executor cancellation is suspended; the new client joins the same stop.
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Executor:
        invocations = 0

        async def execute(self, task, secret=None, *, tool_caller):
            self.invocations += 1
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
            # Returning during cancellation cleanup is not a successful job result.
            return InitialTaskResult(session_id="session", last_message="cancelled", exit_code=0)

        async def reclaim_workspace(self):
            raise AssertionError("stop must not clean or call the workspace pool")

    state, executor = ExecutionState(tmp_path, 1), Executor()
    owner = WorkerExecution(worker_id="w", executor=executor, state=state)
    owner.receive(assignment())
    owner.start()
    request = StopFence(job_id=1, flow_id=1, assignment_token="token", request_id="stop")

    def connect(request, epoch="epoch"):
        client = worker_runtime(
            worker_id="w",
            worker_token="test",
            serve_ws_url="unused",
            execution=owner,
            read_history=state.read,
        )
        connection = AckConnection(request, epoch=epoch)
        return asyncio.create_task(run_worker(client, connection)), connection

    first = second = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        first, _ = connect(request)
        await asyncio.wait_for(cleaning.wait(), 1)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert state.load().terminal is None and not state.load().acknowledged
        for field, wrong in (
            ("job_id", 99),
            ("flow_id", 99),
            ("assignment_token", "wrong"),
            ("request_id", "wrong"),
        ):
            with pytest.raises(WorkerProtocolError):
                await owner.stop(request.model_copy(update={field: wrong}))
        second, connection = connect(request, epoch="new")
        release.set()
        with pytest.raises(ConnectionError, match="script complete"):
            await asyncio.wait_for(second, 2)
        receipt = next(
            message.result for message in connection.sent if isinstance(message, StopResult)
        )
        assert all(
            message.connection_epoch == "new"
            for message in connection.messages
            if any(isinstance(item, StopResult) for item in message.payload)
        )
        assert receipt.execution_nonce == state.load().execution_nonce
        assert receipt.request_id == request.request_id
        assert state.load().acknowledged and state.load().terminal is None
        assert executor.invocations == 1
    finally:
        release.set()
        for task in (first, second):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await owner.shutdown()


async def test_restarted_worker_does_not_infer_stopped_from_absent_task(tmp_path):
    work = assignment()
    journal = ExecutionState(tmp_path, 1)
    journal.record_received(work)
    journal.record_started()
    client = worker_runtime(
        worker_id="w",
        worker_token="secret",
        serve_ws_url="unused",
        execution=WorkerExecution(worker_id="w", executor=SimpleNamespace(), state=journal),
        read_history=journal.read,
    )
    request = StopFence(job_id=1, flow_id=1, assignment_token="token", request_id="stop")
    with pytest.raises(WorkerProtocolError, match="ownership is unknown"):
        await run_worker(client, AckConnection(request))
    assert journal.load().stopped is None


@pytest.mark.parametrize("overflow", [False, True])
async def test_gateway_and_worker_cancel_over_duplex_protocol(store, tmp_path, overflow):
    # Two cancellations used to keep both pool positions occupied, blocking the third Flow.
    root, _ = managed_seed(tmp_path)
    started = asyncio.Event()
    to_worker, to_serve = asyncio.Queue(), asyncio.Queue()

    class Connection(QueueConnection):
        async def send(self, raw):
            if any(isinstance(item, StopResult) for item in parse_worker_message(raw).payload):
                # Stop proves writer exit; dirty contents are cleaned only by the next INITIAL.
                repo = executor.workspaces.path(session.load().workspace)
                assert (repo / "README.md").read_text() == "cancelled tracked edit"
                assert (repo / "untracked").exists()
                assert store.workers.get_flow_execution(flow.id).released_at is None
            await super().send(raw)

    class Executor(WorkerExecutor):
        async def execute(self, task, secret=None, *, tool_caller=None):
            selected = self.state.assignment
            binding = await self.workspaces.claim(
                task.repo, task.flow_id, selected.workspace, selected.generation,
                base_branch="main", remote="origin", push_token=None,
            )
            self.state.bind_workspace(binding)
            path = self.workspaces.path(binding)
            assert (path / "README.md").read_text() == "base\n"
            assert not (path / "untracked").exists()
            (path / "README.md").write_text("cancelled staged edit")
            _git("add", "README.md", cwd=path)
            (path / "README.md").write_text("cancelled tracked edit")
            (path / "untracked").write_text("cancelled untracked edit")
            (path / ".venv").mkdir(exist_ok=True)
            (path / ".venv/cache").write_text("cancelled ignored cache")
            started.set()
            await asyncio.Event().wait()

    class Coordinator:
        async def prepare_for_assignment(self, job, worker, token, epoch):
            task = InitialTask(
                flow_id=flow.id,
                attempt_id=attempt.id,
                repo=flow.repo,
                issue_number=1,
                issue_title="test",
                issue_body="",
                branch="flow/1",
            )
            return AssignmentArguments(
                job_id=job.id, flow_id=job.flow_id, assignment_token=token, task=task,
                workspace=job.workspace, generation=job.generation,
            )

        def mark_started(self, job, task, worker_id):
            pass

        def worker_connected(self, *args):
            pass

        def worker_disconnected(self, *args):
            pass

    gateway_coordinator = Coordinator()
    handler = server_runtime(
        gateway_coordinator,
        database=store.database,
        peers=Gateway(),
        heartbeat_interval=0.01,
        poll_interval=0.01,
        tools=Mock(spec=FlowToolService),
        results=Mock(spec=ResultApplication),
    )
    pool = RepositoryWorkspaces(root, "worker-1", limit=2 if overflow else 1)
    default = pool.initialize()
    await pool.provision("owner/repo")
    unrelated = None
    if overflow:
        store.workers.worker_connected("worker-1", PROTOCOL_VERSION, "setup",
                                       workspace=default.workspace, repo_workspace_limit=2)
        for item in pool.inventory():
            assert store.workers.report_workspace("worker-1", "setup", item)
        other = store.flows.create_flow("owner/repo", 999, 1)
        other_job = store.jobs.enqueue_job(other.id, JobKind.INITIAL)
        claimed = store.workers.assign_job("worker-1", "unrelated", "setup")
        assert claimed.id == other_job.id
        unrelated = await pool.claim("owner/repo", other.id, claimed.workspace, claimed.generation,
                                     base_branch="main", remote="origin", push_token=None)
        assert store.workers.finish_assignment(other_job.id, "worker-1", "unrelated", JobState.DONE, "setup")
    session = ExecutionState(root, 1)
    executor = Executor(root, RecordingAgent(), worker_id="worker-1", state=session, workspaces=pool)
    client = worker_runtime(
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="unused",
        execution=WorkerExecution(worker_id="worker-1", executor=executor, state=session),
        read_history=session.read,
    )
    client.settings.worker_repo_workspace_limit = pool.limit
    server = asyncio.create_task(
        serve_worker(
            QueueSocket(to_serve, to_worker),
            worker_tokens={"worker-1": "worker-secret"},
            serve=handler.serve_worker_session,
        )
    )
    worker = asyncio.create_task(run_worker(client, Connection(to_serve, to_worker)))
    allocations = []
    try:
        async with asyncio.timeout(10):
            for issue_number in (1, 2):
                flow = store.flows.create_flow("owner/repo", issue_number, 1)
                attempt = store.flows.create_attempt(flow.id)
                flow.current_attempt_id = attempt.id
                store.flows.update_flow(flow)
                session = ExecutionState(root, flow.id)
                executor = Executor(root, RecordingAgent(), worker_id="worker-1", state=session, workspaces=pool)
                client.executions[flow.id] = WorkerExecution(
                    worker_id="worker-1", executor=executor, state=session, on_change=client.notify,
                )
                client._create_execution = dict(client.executions).__getitem__
                store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
                started.clear()
                handler.gateway.wake_all()
                while not started.is_set():
                    if server.done():
                        server.result()
                    if worker.done():
                        worker.result()
                    await asyncio.sleep(0.01)
                binding = session.load().workspace
                path = executor.workspaces.path(binding)
                allocations.append((binding, path.stat().st_ino))
                ControlService(jobs_available=handler.gateway.wake_all, database=store.database).cancel(
                    flow
                )
                while not session.load().acknowledged:
                    if server.done():
                        server.result()
                    if worker.done():
                        worker.result()
                    await asyncio.sleep(0.01)
                assert (path / ".venv/cache").read_text() == "cancelled ignored cache"
                assert store.stops.pending_for_worker("worker-1") == []
        replacement = await pool.claim(
            "owner/repo", flow.id + 1, allocations[-1][0].slot_id,
            allocations[-1][0].generation + 1, base_branch="main", remote="origin", push_token=None,
        )
        assert replacement.slot_id == allocations[0][0].slot_id == allocations[1][0].slot_id
        assert replacement.generation > allocations[1][0].generation > allocations[0][0].generation
        assert (
            executor.workspaces.path(replacement).stat().st_ino
            == allocations[0][1]
            == allocations[1][1]
        )
        assert (root / "repos/owner/repo/README.md").read_text() == "base\n"
        if unrelated is not None:
            assert pool.lookup("owner/repo", other.id) == unrelated
    finally:
        server.cancel()
        worker.cancel()
        await asyncio.gather(server, worker, return_exceptions=True)
        await asyncio.gather(*(execution.shutdown() for execution in client.executions.values()))


@pytest.mark.parametrize("delivery", ["combined", "split", "duplicate_B", "duplicate_RB", "B_only", "effects_pending"])
async def test_stopping_consumes_a_and_requires_real_r_before_b(delivery):
    # Registration cannot bypass a pending stop. In-flight T/A and Flow calls may arrive
    # before R/B, but only the real stopped receipt can release the stopping phase.
    now = utcnow()
    intent = StopIntent(
        request_id="stop",
        flow_id=3,
        job_id=5,
        worker_id="worker-1",
        workspace="a" * 32,
        generation=1,
        assignment_token="token",
        binding_at=now,
        reason="cancel",
        requested_at=now,
        state="requested",
        confirmed_at=None,
        receipt_digest=None,
    )
    receipt = ExecutionStopped(**intent.fence().model_dump(), execution_nonce="actual-executor")
    result = StopResult(
        **result_header(stop_call_id(intent.fence()), stop_receipt_id(receipt)),
        method="worker.stop",
        result=receipt,
    )
    accept = AcceptStop(
        **call_header(accept_stop_id(result.message_id)),
        method="server.accept_stop",
        arguments=AcceptStopArguments(**intent.fence().model_dump(), receipt_id=result.message_id),
    )
    terminal = Failed(job_id=5, assignment_token="token", error="cancelled")
    terminal_result, terminal_accept = terminal_items(terminal)
    confirms = []

    class Stops(NoPendingStops):
        confirmed = False

        def pending_for_worker(self, worker_id):
            return [] if self.confirmed else [intent]

        def receipt_request(self, worker_id, request_id):
            assert (worker_id, request_id) == (intent.worker_id, intent.request_id)
            return intent.model_copy(update={"state": "confirmed"}) if self.confirmed else intent

        def confirm(self, worker_id, evidence, connection_epoch):
            confirms.append((worker_id, evidence, connection_epoch))
            assert evidence == receipt and store.job.result_applied_at is not None
            self.confirmed = delivery != "effects_pending"
            return self.confirmed

    class StoppingStore(RecordingStore):
        @property
        def stops(self):
            return stops

    class StoppingSocket(ScriptedSocket):
        def __init__(self):
            super().__init__()
            self.command = asyncio.Event()

        async def send_text(self, raw):
            await super().send_text(raw)
            if any(isinstance(item, StopExecution) for item in parse_server_message(raw).payload):
                self.command.set()

        async def receive_text(self):
            self._step += 1
            if self._step == 1:
                return register_message(
                    recovery=RecoveryDescriptor(
                        job_id=5,
                        assignment_token="token",
                        state="failed",
                        result_digest=terminal_digest(terminal),
                    ),
                ).model_dump_json()
            await self.command.wait()
            message = next(
                message
                for message in map(parse_server_message, self.sent)
                if any(isinstance(item, StopExecution) for item in message.payload)
            )
            epoch = message.connection_epoch
            assert epoch is not None
            if self._step == 2:
                if delivery == "B_only":
                    return worker_message(epoch, accept).model_dump_json()
                tool = FlowToolRequest(
                    **call_header(),
                    method="flow.open_pr",
                    arguments=FlowToolArguments(
                        job_id=5,
                        flow_id=3,
                        attempt_id=11,
                        assignment_token="token",
                        call_id="call",
                        arguments={"title": "no side effects"},
                        vcs_backend=VcsKind.GIT,
                        branch="flow/1",
                        commit_sha="head",
                        remote_sha="head",
                    ),
                )
                items = [tool, terminal_result, terminal_accept, result]
                if delivery != "split":
                    items.append(accept)
                return Heartbeat(
                    **message_header(epoch),
                    control="heartbeat",
                    job_id=5,
                    assignment_token="token",
                    payload=items,
                ).model_dump_json()
            if self._step == 3 and delivery == "split":
                return worker_message(epoch, accept).model_dump_json()
            if self._step == 3 and delivery in {"duplicate_B", "duplicate_RB"}:
                return worker_message(
                    epoch, *([result, accept] if delivery == "duplicate_RB" else [accept]),
                ).model_dump_json()
            raise ConnectionError("script complete")

    stops = Stops()
    store, socket = StoppingStore(), StoppingSocket()
    store.job.state, store.job.assigned_worker_id, store.job.assignment_token = (
        JobState.RUNNING,
        "worker-1",
        "token",
    )
    handler = handler_for(store)
    await serve_worker(socket, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"})
    messages = list(map(parse_server_message, socket.sent))
    stop_index = next(
        i
        for i, message in enumerate(messages)
        if any(isinstance(item, StopExecution) for item in message.payload)
    )
    assert not any(isinstance(message, Registered) for message in messages[:stop_index])
    assert not any(isinstance(item, AcceptResultResponse) for item in sent_items(socket.sent))
    handler.tools.execute.assert_not_called()
    accepted = [item for item in sent_items(socket.sent) if isinstance(item, StopAccepted)]
    if delivery in {"combined", "split", "duplicate_B", "duplicate_RB"}:
        assert len(confirms) == (4 if delivery == "duplicate_RB" else 2)
        assert len(accepted) == (2 if delivery.startswith("duplicate") else 1)
        assert accepted[-1] == accepted[0]
        assert accepted[0].reply_to == accept.message_id and accepted[0].result == intent.fence()
        assert all(epoch == store.connection_epoch for _, _, epoch in confirms)
    else:
        assert accepted == [] and socket.closed == 1008
        assert len(confirms) == (1 if delivery == "effects_pending" else 0)


async def test_stop_without_ack_reuses_workspace_and_reconnect_ack_preserves_new_writer(
    store, tmp_path, monkeypatch, recording_resources
):
    # Lost stop ACKs retain only the old journal, never occupancy. Replaying that ACK
    # after reconnect must not clean or otherwise modify the new generation's writer.
    root, _ = managed_seed(tmp_path)
    pool = RepositoryWorkspaces(root, "worker-1", limit=1)
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
        return AssignmentArguments(
            job_id=job.id,
            flow_id=job.flow_id,
            assignment_token=token,
            workspace=job.workspace,
            generation=job.generation,
            task=InitialTask(
                flow_id=job.flow_id,
                attempt_id=attempts[job.flow_id].id,
                repo="owner/repo",
                issue_number=job.flow_id,
                issue_title="test",
                issue_body="",
                branch="flow/test",
            ),
        )

    monkeypatch.setattr(subject, "prepare_for_assignment", prepare)

    executed = []

    class Executor(WorkerExecutor):
        async def execute(self, task, secret=None, *, tool_caller=None):
            selected = self.state.assignment
            binding = await self.workspaces.claim(
                task.repo, task.flow_id, selected.workspace, selected.generation,
                base_branch="main", remote="origin", push_token=None,
            )
            self.state.bind_workspace(binding)
            (self.workspaces.path(binding) / "writer").write_text(str(task.flow_id))
            executed.append(task.flow_id)
            started.set()
            await asyncio.Event().wait()

        async def reclaim_workspace(self):
            raise AssertionError("stop must not clean or call the workspace pool")

    handler = server_runtime(
        subject,
        database=store.database,
        peers=Gateway(),
        heartbeat_interval=0.01,
        poll_interval=0.01,
        tools=subject.tools,
        results=subject.results,
    )
    session = ExecutionState(root, flows[0].id)
    executor = Executor(root, RecordingAgent(), worker_id="worker-1", state=session, workspaces=pool)
    client = worker_runtime(
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="unused",
        execution=WorkerExecution(worker_id="worker-1", executor=executor, state=session),
        read_history=session.read,
    )
    client.settings.worker_repo_workspace_limit = 1
    next_state = ExecutionState(root, flows[1].id)
    client.executions[flows[1].id] = WorkerExecution(
        worker_id="worker-1", state=next_state, on_change=client.notify,
        executor=Executor(root, RecordingAgent(), worker_id="worker-1", state=next_state, workspaces=pool),
    )
    client._create_execution = dict(client.executions).__getitem__

    class Socket(QueueSocket):
        async def send_text(self, raw):
            await super().send_text(raw)
            message = parse_server_message(raw)
            if any(isinstance(item, StopAccepted) for item in message.payload):
                await super().send_text(raw)  # Replayed ACKs must be journal-only and idempotent.
            if any(
                isinstance(item, Assignment) and item.arguments.flow_id == flows[1].id
                for item in message.payload
            ):
                successor_dispatched.set()

    class LostAckConnection(QueueConnection):
        async def send(self, raw):
            await super().send(raw)
            message = parse_worker_message(raw)
            for item in message.payload:
                if isinstance(item, StopResult):
                    # A periodic heartbeat may already be queued behind the stop receipt.
                    await super().send(Heartbeat(
                        **message_header(message.connection_epoch), control="heartbeat", payload=[],
                        job_id=item.result.job_id, assignment_token=item.result.assignment_token,
                    ).model_dump_json())

        async def recv(self):
            while True:
                raw = await super().recv()
                if not any(isinstance(item, StopAccepted) for item in parse_server_message(raw).payload):
                    return raw

    def connect(connection_type):
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        return (
            asyncio.create_task(
                serve_worker(
                    Socket(incoming, outgoing),
                    worker_tokens={"worker-1": "worker-secret"},
                    serve=handler.serve_worker_session,
                )
            ),
            asyncio.create_task(run_worker(client, connection_type(incoming, outgoing))),
        )

    server, worker = connect(LostAckConnection)
    try:
        async with asyncio.timeout(5):
            await started.wait()
            ControlService(database=store.database, jobs_available=handler.gateway.wake_all).cancel(
                flows[0]
            )
            while len(executed) < 2:
                if server.done():
                    server.result()
                if worker.done():
                    worker.result()
                await asyncio.sleep(0.01)
        assert not session.load().acknowledged
        assert store.jobs.get_job(jobs[1].id).started_at is not None
        assert successor_dispatched.is_set()
        assert store.stops.pending_for_worker("worker-1") == []
        manifest = pool.manifest_path.read_bytes()
        writer = pool.path(next_state.load().workspace) / "writer"
        assert writer.read_text() == str(flows[1].id)
    finally:
        server.cancel()
        worker.cancel()
        await asyncio.gather(server, worker, return_exceptions=True)
    server, worker = connect(QueueConnection)
    try:
        async with asyncio.timeout(5):
            while not session.load().acknowledged or not client._connected:
                if server.done():
                    server.result()
                if worker.done():
                    worker.result()
                await asyncio.sleep(0.01)
        successor = store.jobs.get_job(jobs[1].id)
        assert successor.started_at is not None and executed == [f.id for f in flows]
        assert session.load().acknowledged and successor_dispatched.is_set()
        assert next_state.load().selected_workspace == session.load().selected_workspace
        assert next_state.load().selected_generation > session.load().selected_generation
        assert pool.manifest_path.read_bytes() == manifest
        assert writer.read_text() == str(flows[1].id)
    finally:
        server.cancel()
        worker.cancel()
        await asyncio.gather(server, worker, return_exceptions=True)
        await asyncio.gather(*(execution.shutdown() for execution in client.executions.values()))


async def test_stop_wakes_assignment_waiting_for_missing_a(monkeypatch):
    waiting, command = asyncio.Event(), asyncio.Event()
    requested = False

    class Stops(NoPendingStops):
        def pending_for_worker(self, worker_id):
            if not requested:
                return []
            now = utcnow()
            return [StopIntent(
                request_id="stop",
                flow_id=3,
                job_id=5,
                worker_id=worker_id,
                workspace="a" * 32,
                generation=1,
                assignment_token=store.job.assignment_token,
                binding_at=now,
                reason="cancel",
                requested_at=now,
                state="requested",
                confirmed_at=None,
                receipt_digest=None,
            )]

    class StoppingStore(RecordingStore):
        @property
        def stops(self):
            return Stops()

    class MissingAcceptSocket(ScriptedSocket):
        async def receive_text(self):
            nonlocal requested
            if self._step < 2:
                return await super().receive_text()
            self._step += 1
            args, epoch = self.assignment()
            if self._step == 3:
                result, _ = terminal_items(
                    Failed(
                        job_id=args.job_id,
                        assignment_token=args.assignment_token,
                        error="terminal without acceptance",
                    )
                )
                return worker_message(epoch, result).model_dump_json()
            await waiting.wait()
            requested = True
            handler.gateway.wake_all()
            await asyncio.wait_for(command.wait(), 1)
            raise ConnectionError("stop delivered while A was absent")

        async def send_text(self, raw):
            await super().send_text(raw)
            if any(isinstance(item, StopExecution) for item in parse_server_message(raw).payload):
                command.set()

    store, socket = StoppingStore(), MissingAcceptSocket()
    handler = handler_for(store)
    receive = Session.next_event

    async def observed(connection, *, until=None):
        if store.job.result_applied_at is not None:
            waiting.set()
        return await receive(connection, until=until)

    monkeypatch.setattr(Session, "next_event", observed)
    await serve_worker(socket, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"})
    assert command.is_set()
    assert not any(isinstance(item, AcceptResultResponse) for item in sent_items(socket.sent))
