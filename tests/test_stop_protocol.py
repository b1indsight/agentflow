import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

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
    Succeeded,
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
from app.contracts.receipts import stop_receipt_digest
from app.contracts.result_models import ResultPending
from app.controler.workers.transport import serve_worker
from app.controler.workflow.service import ControlService
from app.domain.models import FlowState, JobKind, JobState, VcsKind
from app.github import GitHubError, GitHubErrorKind, Repository
from app.persistence.records import required
from app.worker.config import WorkerSettings
from app.worker.execution.lifecycle import WorkerExecution, WorkerProtocolError
from app.worker.execution.vcs import _run
from app.worker.runtime import WorkerRuntime
from app.worker.state.execution import ExecutionState
from tests.test_remote_e2e import QueueConnection, QueueSocket
from tests.test_vcs import _git
from tests.test_worker_executor import RecordingAgent
from tests.test_worker_handler import (
    ScriptedSocket,
    recovery_registration,
    sent_items,
    terminal_items,
)
from tests.test_worker_handler import queued_initial as queued_initial  # noqa: PLC0414
from tests.test_worker_workspaces import managed_seed
from tests.test_workspace_inventory import merged_business
from tests.wire import worker_message
from tests.worker_fixtures import run_worker, worker_runtime


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

    owner = WorkerExecution(worker_id="w", executor=Executor(), state=journal)
    owner.receive(work)
    owner.start()
    try:
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(0.01)
        client = await worker_runtime(
            worker_token="secret",
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
        restarted = await worker_runtime(
            worker_token="secret",
            execution=WorkerExecution(
                worker_id="w",
                state=restarted_state,
                executor=SimpleNamespace(),
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


async def test_reconnection_during_stop_joins_executor_and_preserves_original_receipt(tmp_path):
    # Disconnect while executor cancellation is suspended; the new connection joins the same stop.
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

    state, executor = ExecutionState(tmp_path, 1), Executor()
    owner = WorkerExecution(worker_id="w", executor=executor, state=state)
    owner.receive(assignment())
    owner.start()
    request = StopFence(job_id=1, flow_id=1, assignment_token="token", request_id="stop")

    client = await worker_runtime(execution=owner, read_history=state.read)

    def connect(request, epoch="epoch"):
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
    client = await worker_runtime(
        worker_token="secret",
        execution=WorkerExecution(worker_id="w", executor=SimpleNamespace(), state=journal),
        read_history=journal.read,
    )
    request = StopFence(job_id=1, flow_id=1, assignment_token="token", request_id="stop")
    with pytest.raises(WorkerProtocolError, match="ownership is unknown"):
        await run_worker(client, AckConnection(request))
    assert journal.load().stopped is None


@pytest.mark.parametrize("overflow", [False, True])
async def test_gateway_and_worker_cancel_over_duplex_protocol(
    store, tmp_path, overflow, recording_resources, server_runtime, monkeypatch,
):
    # Two cancellations used to keep both pool positions occupied, blocking the third Flow.
    root, _ = managed_seed(tmp_path)
    _git("branch", "trunk", cwd=root / "repos/owner/repo")
    _git("push", "origin", "trunk", cwd=root / "repos/owner/repo")
    started = asyncio.Event()
    to_worker, to_serve = asyncio.Queue(), asyncio.Queue()

    class Connection(QueueConnection):
        async def send(self, raw):
            if any(isinstance(item, StopResult) for item in parse_worker_message(raw).payload):
                # Stop proves writer exit; dirty contents are cleaned only by the next INITIAL.
                repo = pool.path(session.load().workspace)
                assert (repo / "README.md").read_text() == "cancelled tracked edit"
                assert (repo / "untracked").exists()
                assert store.workers.get_flow_execution(flow.id).released_at is None
            await super().send(raw)

    class Agent(RecordingAgent):
        async def start(self, task, path, *, output_schema=None, context=None):
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

    handler = server_runtime(settings={"worker_heartbeat_interval": 0.01})
    monkeypatch.setattr("app.worker.runtime.CodexAgent", lambda **kwargs: Agent())
    client = WorkerRuntime(WorkerSettings(
        worker_id="worker-1", worker_token="worker-secret", serve_ws_url="wss://unused",
        worker_root=root, worker_repo_workspace_limit=2 if overflow else 1,
    ))
    client.initialize()
    pool = client.workspaces
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
                path = pool.path(binding)
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
            pool.path(replacement).stat().st_ino
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
async def test_stopping_consumes_a_and_requires_real_r_before_b(
    store, prepared_initial, monkeypatch, delivery, server_runtime,
):
    # Registration cannot bypass a pending stop. In-flight T/A and Flow calls may arrive
    # before R/B, but only the real stopped receipt can release the stopping phase.
    flow, subject, github, request, initial_result = prepared_initial
    job = required(store.jobs.get_job(request.arguments.job_id))
    terminal = Failed(job_id=job.id, assignment_token=job.assignment_token, error="cancelled")
    if delivery == "effects_pending":
        create = Repository.open_pull_request

        async def lost_response(repo, head, base, title, body):
            await create(repo, head, base, title, body)
            raise GitHubError(GitHubErrorKind.TRANSIENT, "open_pull_request")

        monkeypatch.setattr(Repository, "open_pull_request", lost_response)
        terminal = Succeeded(job_id=job.id, assignment_token=job.assignment_token, result=initial_result)
        assert store.workers.record_assignment_result(
            job.id, "worker-a", job.assignment_token, "epoch", terminal,
        ) == "pending"
        with pytest.raises(ResultPending):
            await subject.results.apply(job.id)
        assert github.opened == [flow.initial_branch]
    ControlService(database=store.database).cancel(flow)
    intent, = store.stops.pending_for_worker("worker-a")
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
    terminal_result, terminal_accept = terminal_items(terminal)
    registration = recovery_registration(store, job, RecoveryDescriptor(
        job_id=job.id, assignment_token=job.assignment_token,
        state="succeeded" if delivery == "effects_pending" else "failed",
        result_digest=terminal_digest(terminal),
    ))

    class StoppingSocket(ScriptedSocket):
        def __init__(self):
            super().__init__(worker_id="worker-a", registration=registration)
            self.command = asyncio.Event()

        async def send_text(self, raw):
            await super().send_text(raw)
            if any(isinstance(item, StopExecution) for item in parse_server_message(raw).payload):
                self.command.set()

        async def receive_text(self):
            self._step += 1
            if self._step == 1:
                return registration.model_dump_json()
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
                    return worker_message(accept, epoch=epoch).model_dump_json()
                tool = FlowToolRequest(
                    **call_header(),
                    method="flow.open_pr",
                    arguments=FlowToolArguments(
                        job_id=job.id,
                        flow_id=flow.id,
                        attempt_id=request.arguments.attempt_id,
                        assignment_token=job.assignment_token,
                        call_id="call",
                        arguments={"title": "no side effects"},
                        vcs_backend=VcsKind.GIT,
                        branch=flow.initial_branch,
                        commit_sha="head",
                        remote_sha="head",
                    ),
                )
                items = [tool, terminal_result, terminal_accept, result]
                if delivery == "effects_pending":
                    # The inbox already exists, but its actual GitHub POST lost its
                    # response. Stop evidence cannot acknowledge unresolved effects.
                    items = [tool, result]
                if delivery != "split":
                    items.append(accept)
                return Heartbeat(
                    **message_header(epoch),
                    control="heartbeat",
                    job_id=job.id,
                    assignment_token=job.assignment_token,
                    payload=items,
                ).model_dump_json()
            if self._step == 3 and delivery == "split":
                return worker_message(accept, epoch=epoch).model_dump_json()
            if self._step == 3 and delivery in {"duplicate_B", "duplicate_RB"}:
                return worker_message(
                    *([result, accept] if delivery == "duplicate_RB" else [accept]), epoch=epoch,
                ).model_dump_json()
            raise ConnectionError("script complete")

    socket = StoppingSocket()
    handler = server_runtime(github=github)
    await serve_worker(socket, serve=handler.serve_worker_session, worker_tokens={"worker-a": "worker-secret"})
    messages = list(map(parse_server_message, socket.sent))
    stop_index = next(
        i
        for i, message in enumerate(messages)
        if any(isinstance(item, StopExecution) for item in message.payload)
    )
    assert not any(isinstance(message, Registered) for message in messages[:stop_index])
    assert not any(isinstance(item, AcceptResultResponse) for item in sent_items(socket.sent))
    assert store.deliveries.get_tool_call("call") is None
    accepted = [item for item in sent_items(socket.sent) if isinstance(item, StopAccepted)]
    persisted = required(store.stops.receipt_request("worker-a", intent.request_id))
    if delivery in {"combined", "split", "duplicate_B", "duplicate_RB"}:
        assert persisted.state == "confirmed" and persisted.confirmed_at is not None
        assert persisted.receipt_digest == stop_receipt_digest(receipt)
        assert required(store.jobs.get_job(job.id)).result_applied_at is not None
        assert len(accepted) == (2 if delivery.startswith("duplicate") else 1)
        assert accepted[-1] == accepted[0]
        assert accepted[0].reply_to == accept.message_id and accepted[0].result == intent.fence()
    else:
        assert accepted == [] and socket.closed == 1008
        assert persisted.state == ("stopped" if delivery == "effects_pending" else "pending")
        assert persisted.confirmed_at is None
        assert required(store.jobs.get_job(job.id)).result_applied_at is None
        if delivery == "effects_pending":
            assert persisted.receipt_digest == stop_receipt_digest(receipt)
            assert any(effect.dispatched_at is not None and effect.receipt is None
                       for effect in store.results.list_effects(job.id))


@pytest.mark.parametrize("merged", [False, True])
async def test_stop_without_ack_reuses_workspace_and_reconnect_ack_preserves_new_writer(
    store, tmp_path, recording_resources, merged, server_runtime, monkeypatch,
):
    # Lost stop ACKs retain only the old journal, never occupancy. Replaying that ACK
    # after reconnect must not clean or otherwise modify the new generation's writer.
    root, _ = managed_seed(tmp_path)
    _git("branch", "trunk", cwd=root / "repos/owner/repo")
    _git("push", "origin", "trunk", cwd=root / "repos/owner/repo")
    flows, jobs = [], []
    for issue in (1, 2):
        flow = store.flows.create_flow("owner/repo", issue, 1)
        attempt = store.flows.create_attempt(flow.id)
        flow.current_attempt_id = attempt.id
        store.flows.update_flow(flow)
        flows.append(flow)
        jobs.append(store.jobs.enqueue_job(flow.id, JobKind.INITIAL))
    started, successor_dispatched = asyncio.Event(), asyncio.Event()
    executed = []

    class Agent(RecordingAgent):
        async def start(self, task, cwd, *, output_schema=None, context=None):
            flow_id = next(item.flow_id for item in pool.inventory()
                           if item.repository is not None and item.flow_id is not None
                           and pool.path(pool.lookup("owner/repo", item.flow_id)) == cwd)
            (cwd / "writer").write_text(str(flow_id))
            executed.append(flow_id)
            started.set()
            await asyncio.Event().wait()

    handler = server_runtime(settings={"worker_heartbeat_interval": 0.01})
    monkeypatch.setattr("app.worker.runtime.CodexAgent", lambda **kwargs: Agent())
    client = WorkerRuntime(WorkerSettings(
        worker_id="worker-1", worker_token="worker-secret", serve_ws_url="wss://unused",
        worker_root=root, worker_repo_workspace_limit=1,
    ))
    client.initialize()
    pool = client.workspaces
    session = ExecutionState(root, flows[0].id)
    next_state = ExecutionState(root, flows[1].id)

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
            if merged:
                # The merge business transaction commits before the worker has stopped.
                with store.transaction() as tx:
                    tx.lock_workflow(flows[0].id)
                    tx.stops.request(flows[0].id)
                merged_business(store, flows[0].id)
                handler.gateway.wake_all()
            else:
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
        assert store.flows.get_flow(flows[0].id).state is FlowState.ARCHIVED
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
        assert store.flows.get_flow(flows[0].id).state is FlowState.ARCHIVED
    finally:
        server.cancel()
        worker.cancel()
        await asyncio.gather(server, worker, return_exceptions=True)
        await asyncio.gather(*(execution.shutdown() for execution in client.executions.values()))


async def test_stop_wakes_assignment_waiting_for_missing_a(
    store, queued_initial, monkeypatch, recording_resources, server_runtime,
):
    flow, job = queued_initial
    waiting, command = asyncio.Event(), asyncio.Event()

    class MissingAcceptSocket(ScriptedSocket):
        async def receive_text(self):
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
                return worker_message(result, epoch=epoch).model_dump_json()
            await waiting.wait()
            ControlService(database=store.database, jobs_available=handler.gateway.wake_all).cancel(flow)
            await asyncio.wait_for(command.wait(), 1)
            raise ConnectionError("stop delivered while A was absent")

        async def send_text(self, raw):
            await super().send_text(raw)
            if any(isinstance(item, StopExecution) for item in parse_server_message(raw).payload):
                command.set()

    socket = MissingAcceptSocket()
    handler = server_runtime()
    receive = Session.next_event

    async def observed(connection, *, until=None):
        if required(store.jobs.get_job(job.id)).result_applied_at is not None:
            waiting.set()
        return await receive(connection, until=until)

    monkeypatch.setattr(Session, "next_event", observed)
    await serve_worker(socket, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"})
    assert command.is_set()
    intent, = store.stops.pending_for_worker("worker-1")
    assert intent.job_id == job.id and intent.state == "pending"
    assert required(store.jobs.get_job(job.id)).result_applied_at is not None
    assert not any(isinstance(item, AcceptResultResponse) for item in sent_items(socket.sent))
