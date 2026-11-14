import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocketDisconnect

from app.communication.dispatch import Dispatcher
from app.contracts.conversation import ConversationFailure, HistoryQuery
from app.contracts.protocol import (
    MAX_PAYLOAD_ITEMS,
    AcceptResultArguments,
    AcceptResultRequest,
    AcceptResultResponse,
    Assignment,
    AssignmentArguments,
    ConversationResult,
    ExecuteResult,
    ExecutionIdentity,
    Failed,
    Heartbeat,
    InitialTaskResult,
    ProtocolError,
    RecoveryDescriptor,
    RecoveryRejected,
    Register,
    Registered,
    ResultAcceptance,
    ServerPayload,
    Started,
    Succeeded,
    TerminalPayload,
    WorkerMessage,
    WorkerMessageUnion,
    accept_result_id,
    call_header,
    execution_call_id,
    message_header,
    parse_server_message,
    result_header,
    terminal_digest,
    terminal_id,
)
from app.contracts.result_models import ResultPending
from app.controler.workers.callbacks import ServerCallbacks
from app.controler.workers.conversation import ConversationBroker
from app.controler.workers.transport import serve_worker
from app.controler.workflow.service import ControlService
from app.domain.models import JobState, WorkerStatus, WorkspaceInventory
from app.persistence.records import required
from app.persistence.workers import WorkerStore
from tests.test_communication_session import Socket
from tests.test_conversation_history import SESSION
from tests.test_workflow_transactions import accepted_coding
from tests.wire import worker_message
from tests.worker_fixtures import inventory, server_callbacks
from tests.workspace_fixtures import report_settled

pytestmark = pytest.mark.usefixtures("recording_resources")


@pytest.fixture
def queued_initial(store):
    flow = required(ControlService(database=store.database).create_flow("owner/repo", 8, 42))
    # These wire tests start after naming; the full inference wire is covered in remote_e2e.
    delivery = store.deliveries.get_or_create_delivery(flow.id, "flow/implement-task")
    delivery.base_branch = "trunk"
    store.deliveries.update_delivery(delivery)
    job, = store.jobs.list_jobs(flow.id)
    return flow, job


def register_message(
    worker_id: str = "worker-1", recovery: RecoveryDescriptor | None = None,
    *, inventory_items: list[WorkspaceInventory] | None = None,
) -> Register:
    items = inventory() if inventory_items is None else inventory_items
    return Register(
        **message_header(None), control="register", worker_id=worker_id,
        workspace=next(item.workspace for item in items if item.repository is None),
        repo_workspace_limit=2, inventory=items,
        recoveries=[recovery] if recovery is not None else [],
        inventory_complete=True, inference=None, payload=[],
    )


def recovery_registration(store, job, recovery):
    """Report the same concrete locations and binding selected by the real repository."""
    items = []
    for workspace in store.workers.list_workspaces(job.assigned_worker_id):
        binding = store.workers.get_worker_execution(job.assigned_worker_id, workspace.workspace)
        items.append(WorkspaceInventory(
            workspace=workspace.workspace, repository=workspace.repository,
            generation=workspace.generation, flow_id=binding.flow_id if binding else None,
        ))
    return register_message(job.assigned_worker_id, recovery, inventory_items=items)


def terminal_items(terminal: TerminalPayload) -> tuple[ExecuteResult, AcceptResultRequest]:
    identity = terminal_id(terminal)
    result = ExecuteResult(
        **result_header(execution_call_id(terminal.job_id, terminal.assignment_token), identity),
        method="worker.execute", job_id=terminal.job_id, assignment_token=terminal.assignment_token,
        result=terminal.result if isinstance(terminal, Succeeded) else None,
        error=terminal.error if isinstance(terminal, Failed) else None,
    )
    accept = AcceptResultRequest(
        **call_header(accept_result_id(identity)), method="server.accept_result",
        arguments=AcceptResultArguments(
            job_id=terminal.job_id, assignment_token=terminal.assignment_token, result_id=identity,
        ),
    )
    return result, accept


def sent_items(raws: list[str]) -> list[ServerPayload]:
    return [item for raw in raws for item in parse_server_message(raw).payload]


class ScriptedSocket:
    """Script an external worker while the server owns actual assignment/result transitions."""

    def __init__(self, *, worker_id="worker-1", token="worker-secret", registration=None):
        self.worker_id = worker_id
        self.registration = registration or register_message(worker_id)
        self.headers = {"x-flowtool-worker-id": worker_id, "authorization": f"Bearer {token}"}
        self.accepted = False
        self.closed = None
        self.sent: list[str] = []
        self._step = 0
        self.assignment_sent = asyncio.Event()

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed = code

    async def send_text(self, data):
        self.sent.append(data)
        if any(isinstance(item, Assignment) for item in parse_server_message(data).payload):
            self.assignment_sent.set()

    def assignment(self) -> tuple[AssignmentArguments, str]:
        for raw in self.sent:
            message = parse_server_message(raw)
            for item in message.payload:
                if isinstance(item, Assignment):
                    assert message.connection_epoch is not None
                    return item.arguments, message.connection_epoch
        raise AssertionError("assignment not sent")

    async def receive_text(self):
        self._step += 1
        if self._step == 1:
            return self.registration.model_dump_json()
        await self.assignment_sent.wait()
        assignment, epoch = self.assignment()
        if self._step == 2:
            started = Started(
                **call_header(), method="server.execution_started",
                arguments=ExecutionIdentity(job_id=assignment.job_id,
                                             assignment_token=assignment.assignment_token),
            )
            return worker_message(started, epoch=epoch).model_dump_json()
        if self._step == 3:
            return worker_message(*terminal_items(Succeeded(
                job_id=assignment.job_id, assignment_token=assignment.assignment_token,
                result=InitialTaskResult(session_id="session-1", last_message="failed", exit_code=1),
            )), epoch=epoch).model_dump_json()
        raise WebSocketDisconnect()


class WaitingSocket(ScriptedSocket):
    async def receive_text(self):
        if self._step == 0:
            self._step = 1
            return self.registration.model_dump_json()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_gateway_authorizes_dispatch_after_preparation(store, queued_initial, monkeypatch, server_runtime):
    handler, socket = server_runtime(), ScriptedSocket()
    prepare = handler.coordinator.prepare_for_assignment

    async def reassigned(job, worker, token, epoch):
        assignment = await prepare(job, worker, token, epoch)
        # Another scheduler replaces an unstarted claim while preparation is in flight.
        assert store.workers.requeue_assignment(job.id, worker, token, epoch)
        replacement = required(store.workers.assign_job(worker, "reassigned-token", epoch))
        assert replacement.id == job.id
        return assignment

    monkeypatch.setattr(handler.coordinator, "prepare_for_assignment", reassigned)
    await serve_worker(socket, serve=handler.serve_worker_session,
                       worker_tokens={"worker-1": "worker-secret"})
    assert not any(isinstance(item, Assignment) for item in sent_items(socket.sent))
    persisted = required(store.jobs.get_job(queued_initial[1].id))
    assert persisted.started_at is None and persisted.dispatch_authorized_at is None
    assert persisted.assignment_token == "reassigned-token"


async def test_gateway_never_acknowledges_an_uncommitted_result(store, queued_initial, monkeypatch, server_runtime):
    handler, socket = server_runtime(), ScriptedSocket()

    async def unfinished(*args):
        raise ResultPending("GitHub outcome requires reconciliation")

    monkeypatch.setattr(handler.results, "_apply", unfinished)
    await serve_worker(socket, serve=handler.serve_worker_session,
                       worker_tokens={"worker-1": "worker-secret"})
    persisted = required(store.jobs.get_job(queued_initial[1].id))
    assert persisted.result_payload is not None and persisted.result_received_at is not None
    assert persisted.result_applied_at is None
    assert not any(isinstance(item, AcceptResultResponse) for item in sent_items(socket.sent))
    assert socket.closed == 1013


async def test_gateway_rejects_bad_credentials_before_accepting(monkeypatch):
    socket = ScriptedSocket(token="wrong")
    receive = AsyncMock(side_effect=AssertionError("unauthenticated socket must not be read"))
    monkeypatch.setattr(socket, "receive_text", receive)
    serve = AsyncMock()
    await serve_worker(socket, serve=serve, worker_tokens={"worker-1": "worker-secret"})
    assert not socket.accepted and socket.closed == 1008
    receive.assert_not_awaited()
    serve.assert_not_awaited()


async def test_disconnected_predecessor_does_not_reject_replacement(store, server_runtime):
    # The peer may already be gone when replacement closes its still-registered Session.
    class ObservedSocket(WaitingSocket):
        def __init__(self):
            super().__init__()
            self.registered = asyncio.Event()

        async def send_text(self, data):
            await super().send_text(data)
            if isinstance(parse_server_message(data), Registered):
                self.registered.set()

        async def close(self, code=1000):
            await super().close(code)
            if self is first:
                raise WebSocketDisconnect(1006)

    handler = server_runtime()
    peers = handler.gateway
    first, second = ObservedSocket(), ObservedSocket()
    first_session = asyncio.create_task(serve_worker(
        first, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"},
    ))
    tasks = [first_session]
    try:
        await asyncio.wait_for(first.registered.wait(), 2)
        previous = peers.get("worker-1")
        assert previous is not None
        tasks.append(asyncio.create_task(serve_worker(
            second, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"},
        )))
        await asyncio.wait_for(first_session, 2)
        await asyncio.wait_for(second.registered.wait(), 2)
        current = peers.get("worker-1")
        assert current is not None and current is not previous
        assert current.session.epoch != previous.session.epoch
        worker, = store.workers.list_workers()
        assert worker.connection_epoch == current.session.epoch and worker.status is WorkerStatus.ONLINE
        assert previous.session.closed and first.closed == 1012
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_overlapping_replacements_do_not_republish_old_route(store, server_runtime):
    # B waits for A's close while C supersedes B. Finishing A must not publish B again.
    closing, release = asyncio.Event(), asyncio.Event()

    class ClosingSocket(Socket):
        def __init__(self, slow=False):
            super().__init__()
            self.slow = slow
            self.headers = {"x-flowtool-worker-id": "worker-1",
                            "authorization": "Bearer worker-secret"}
            self.incoming.put_nowait(register_message().model_dump_json())

        async def accept(self):
            pass

        async def close(self, code=1000):
            if self.slow:
                closing.set()
                await release.wait()

    handler = server_runtime()
    gateway = handler.gateway
    broker = ConversationBroker(gateway)
    tokens = {"worker-1": "worker-secret"}
    first, second, third = ClosingSocket(True), ClosingSocket(), ClosingSocket()
    tasks = [asyncio.create_task(serve_worker(first, serve=handler.serve_worker_session, worker_tokens=tokens))]

    async def next_business(socket):
        while True:
            message = parse_server_message(await asyncio.wait_for(socket.sent.get(), 2))
            if message.control is not None or message.payload:
                return message

    def only_receipt_acks(socket):
        while not socket.sent.empty():
            message = parse_server_message(socket.sent.get_nowait())
            assert message.control is None and message.payload == [] and message.ack

    try:
        assert isinstance(await next_business(first), Registered)
        query = asyncio.create_task(broker.query("worker-1", HistoryQuery(session_id=SESSION)))
        await next_business(first)
        tasks.append(asyncio.create_task(serve_worker(second, serve=handler.serve_worker_session, worker_tokens=tokens)))
        await asyncio.wait_for(closing.wait(), 2)
        with pytest.raises(ConversationFailure, match="worker_unavailable"):
            await asyncio.wait_for(query, 2)
        only_receipt_acks(second)
        tasks.append(asyncio.create_task(serve_worker(third, serve=handler.serve_worker_session, worker_tokens=tokens)))
        registered = await next_business(third)
        assert isinstance(registered, Registered)
        newest = gateway.get("worker-1")
        assert newest is not None
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks[:2]), 2)
        assert gateway.get("worker-1") is newest
        assert newest.session.epoch == registered.connection_epoch
        only_receipt_acks(second)
        worker, = store.workers.list_workers()
        assert worker.connection_epoch == newest.session.epoch and worker.status is WorkerStatus.ONLINE
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("disconnect_after_start", [False, True])
async def test_gateway_disconnect_policy(store, queued_initial, disconnect_after_start, server_runtime):
    class DisconnectingSocket(ScriptedSocket):
        async def receive_text(self):
            if self._step == (2 if disconnect_after_start else 1):
                raise WebSocketDisconnect()
            return await super().receive_text()

    await serve_worker(DisconnectingSocket(), serve=server_runtime().serve_worker_session,
                       worker_tokens={"worker-1": "worker-secret"})
    job = required(store.jobs.get_job(queued_initial[1].id))
    assert job.state is JobState.RUNNING and job.result_received_at is None
    assert (job.started_at is not None) == disconnect_after_start
    binding = required(store.workers.get_flow_execution(job.flow_id))
    assert binding.released_at is None
    assert next(w for w in store.workers.list_workspaces("worker-1")
                if w.workspace == job.workspace).current_job_id == job.id


async def test_gateway_resumes_started_assignment_under_a_new_epoch(store, server_runtime):
    job, _ = accepted_coding(store, receive=False)
    registration = recovery_registration(store, job, RecoveryDescriptor(
        job_id=job.id, assignment_token=job.assignment_token, state="started", executor_active=True,
    ))

    class RecoveringSocket(ScriptedSocket):
        def __init__(self):
            super().__init__(worker_id="worker-a", registration=registration)
            self.resumed = asyncio.Event()

        async def send_text(self, raw):
            await super().send_text(raw)
            if isinstance(parse_server_message(raw), Registered):
                self.resumed.set()

        async def receive_text(self):
            self._step += 1
            if self._step == 1:
                return registration.model_dump_json()
            if self._step == 2:
                await self.resumed.wait()
                resumed = next(m for m in map(parse_server_message, self.sent) if isinstance(m, Registered))
                return Heartbeat(
                    **message_header(resumed.connection_epoch), control="heartbeat", payload=[],
                    job_id=job.id, assignment_token=job.assignment_token,
                ).model_dump_json()
            raise WebSocketDisconnect()

    socket = RecoveringSocket()
    await serve_worker(socket, serve=server_runtime().serve_worker_session,
                       worker_tokens={"worker-a": "worker-secret"})
    resumed = next(m for m in map(parse_server_message, socket.sent) if isinstance(m, Registered))
    assert len(resumed.recoveries) == 1 and resumed.recoveries[0].action == "resume"
    worker, = store.workers.list_workers()
    assert worker.connection_epoch == resumed.connection_epoch != "epoch-a"
    persisted = required(store.jobs.get_job(job.id))
    assert persisted.state is JobState.RUNNING and persisted.heartbeat_at is not None
    assert store.workers.assignment_active(job.id, "worker-a", job.assignment_token, worker.connection_epoch)
    assert not store.workers.assignment_active(job.id, "worker-a", job.assignment_token, "epoch-a")


@pytest.mark.parametrize("first,code", [
    (worker_message(ConversationResult(
        **result_header("1" * 32), method="worker.history", error="query_busy",
    ), epoch="old"), "protocol_violation"),
    (register_message(worker_id="other-worker"), "protocol_violation"),
    (register_message().model_copy(update={"protocol_version": 99}), "invalid_message"),
])
async def test_transport_checks_actual_first_message_before_session_dispatch(first, code):
    class BadFirstSocket(ScriptedSocket):
        async def receive_text(self):
            return first.model_dump_json()

    socket, serve = BadFirstSocket(), AsyncMock()
    await serve_worker(socket, worker_tokens={"worker-1": "worker-secret"}, serve=serve)
    serve.assert_not_called()
    assert socket.accepted and socket.closed == 1008 and len(socket.sent) == 1
    error = parse_server_message(socket.sent[0])
    assert isinstance(error, ProtocolError) and error.code == code


@pytest.mark.parametrize("failure", ["disconnected", "runtime", "database", "cancel", "sealed"])
async def test_handler_cleanup_joins_communication_before_presence_and_propagates_errors(
    store, monkeypatch, failure, server_runtime,
):
    closing, release, joined, registered, disconnect = (asyncio.Event() for _ in range(5))

    class ClosingSocket(WaitingSocket):
        async def receive_text(self):
            if self._step == 0:
                return await super().receive_text()
            await disconnect.wait()
            raise WebSocketDisconnect()

        async def send_text(self, raw):
            await super().send_text(raw)
            if isinstance(parse_server_message(raw), Registered):
                registered.set()

        async def close(self, code=1000):
            closing.set()
            await release.wait()
            joined.set()
            if failure == "disconnected":
                raise WebSocketDisconnect(1006)
            if failure == "runtime":
                raise RuntimeError("invalid socket close state")
            await super().close(code)

    disconnected = WorkerStore.worker_disconnected

    def record_disconnect(repository, worker_id, reason=None, connection_epoch=""):
        assert joined.is_set()
        disconnected(repository, worker_id, reason, connection_epoch)
        if failure == "database":
            raise ConnectionError("database disconnect failed")

    monkeypatch.setattr(WorkerStore, "worker_disconnected", record_disconnect)
    handler, socket = server_runtime(), ClosingSocket()
    peers = handler.gateway
    if failure == "sealed":
        peers.seal()
    running = asyncio.create_task(serve_worker(
        socket, worker_tokens={"worker-1": "worker-secret"}, serve=handler.serve_worker_session,
    ))
    try:
        if failure != "sealed":
            await asyncio.wait_for(registered.wait(), 2)
            assert peers.get("worker-1") is not None
            disconnect.set()
        await asyncio.wait_for(closing.wait(), 2)
        assert all(worker.status is WorkerStatus.ONLINE for worker in store.workers.list_workers())
        if failure == "cancel":
            for _ in range(2):
                running.cancel()
                await asyncio.sleep(0)
                assert not running.done()
        release.set()
        if failure in {"runtime", "database", "cancel"}:
            error = {"cancel": asyncio.CancelledError, "runtime": RuntimeError,
                     "database": ConnectionError}[failure]
            with pytest.raises(error):
                await asyncio.wait_for(running, 2)
        else:
            await asyncio.wait_for(running, 2)
        workers = store.workers.list_workers()
        assert not workers if failure == "sealed" else workers[0].status is WorkerStatus.OFFLINE
        assert peers.get("worker-1") is None
        assert all(isinstance(message, Registered) or (message.ack and not message.payload)
                   for message in map(parse_server_message, socket.sent))
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, peers.close(), return_exceptions=True)


@pytest.mark.parametrize("batch", ["started", "heartbeat", "wrong_epoch", "ack", "reversed"])
async def test_control_and_payload_keep_order_and_trailing_acceptance(store, queued_initial, batch, server_runtime):
    # Started/T/A and heartbeat/history/T/A must not lose trailing lifecycle entries.
    class BatchedSocket(ScriptedSocket):
        async def receive_text(self):
            separate_started = batch in {"heartbeat", "wrong_epoch", "ack"}
            if self._step < 1 or (self._step == 1 and separate_started):
                return await super().receive_text()
            if self._step > (2 if separate_started else 1):
                raise WebSocketDisconnect()
            self._step += 1
            await self.assignment_sent.wait()
            args, epoch = self.assignment()
            started = Started(
                **call_header(), method="server.execution_started",
                arguments=ExecutionIdentity(job_id=args.job_id, assignment_token=args.assignment_token),
            )
            terminal, accept = terminal_items(Failed(
                job_id=args.job_id, assignment_token=args.assignment_token, error="executor exited",
            ))
            if batch in {"heartbeat", "wrong_epoch"}:
                history = ConversationResult(
                    **result_header("1" * 32), method="worker.history", error="query_busy",
                )
                return Heartbeat(
                    **message_header("old" if batch == "wrong_epoch" else epoch), control="heartbeat",
                    job_id=args.job_id, assignment_token=args.assignment_token,
                    payload=[history, terminal, accept],
                ).model_dump_json()
            if batch == "ack":
                return WorkerMessage(
                    **(message_header(epoch) | {"ack": True, "ack_id": "1" * 32}),
                    control=None, payload=[terminal, accept],
                ).model_dump_json()
            items = [terminal, started, accept] if batch == "reversed" else [started, terminal, accept]
            return worker_message(*items, epoch=epoch).model_dump_json()

    socket = BatchedSocket()
    await serve_worker(socket, serve=server_runtime().serve_worker_session,
                       worker_tokens={"worker-1": "worker-secret"})
    responses = [item for item in sent_items(socket.sent) if isinstance(item, AcceptResultResponse)]
    job = required(store.jobs.get_job(queued_initial[1].id))
    if batch in {"reversed", "wrong_epoch"}:
        assert (job.started_at is not None) == (batch == "wrong_epoch")
        assert job.result_payload is None and responses == [] and socket.closed == 1008
    else:
        assert job.started_at is not None and job.state is JobState.FAILED
        assert job.result_applied_at is not None
        assert len(responses) == 1 and responses[0].result.state == "failed" and socket.closed == 1000


@pytest.mark.parametrize("acceptance", ["later", "heartbeat", "absent", "write_failed"])
async def test_terminal_waits_for_actual_acceptance_write_before_retiring_assignment(
    store, queued_initial, acceptance, monkeypatch, server_runtime,
):
    applied = asyncio.Event()
    callbacks = []

    class ObservedCallbacks(ServerCallbacks):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            callbacks.append(self)

    monkeypatch.setattr("app.controler.runtime.ServerCallbacks", ObservedCallbacks)

    class SplitSocket(ScriptedSocket):
        async def receive_text(self):
            if self._step < 2:
                return await super().receive_text()
            self._step += 1
            args, epoch = self.assignment()
            terminal, accept = terminal_items(Failed(
                job_id=args.job_id, assignment_token=args.assignment_token, error="executor exited",
            ))
            if self._step == 3:
                return worker_message(terminal, epoch=epoch).model_dump_json()
            if self._step == 4:
                await applied.wait()
                assert set(callbacks[0].current) == {args.job_id}
                assert required(store.jobs.get_job(args.job_id)).result_applied_at is not None
                assert not any(isinstance(item, AcceptResultResponse) for item in sent_items(self.sent))
                if acceptance == "heartbeat":
                    return Heartbeat(
                        **message_header(epoch), control="heartbeat", job_id=args.job_id,
                        assignment_token=args.assignment_token, payload=[],
                    ).model_dump_json()
                if acceptance != "absent":
                    return worker_message(accept, epoch=epoch).model_dump_json()
            if self._step == 5 and acceptance == "heartbeat":
                return worker_message(accept, epoch=epoch).model_dump_json()
            raise WebSocketDisconnect()

        async def send_text(self, raw):
            if acceptance == "write_failed" and any(
                isinstance(item, AcceptResultResponse) for item in parse_server_message(raw).payload
            ):
                raise ConnectionError("response lost")
            await super().send_text(raw)

    handler, socket = server_runtime(), SplitSocket()
    apply = handler.results._apply

    async def commit(job_id):
        state = await apply(job_id)
        applied.set()
        return state

    monkeypatch.setattr(handler.results, "_apply", commit)
    await serve_worker(socket, serve=handler.serve_worker_session,
                       worker_tokens={"worker-1": "worker-secret"})
    job = required(store.jobs.get_job(queued_initial[1].id))
    assert job.result_applied_at is not None
    assert (job.id not in callbacks[0].current) == (acceptance in {"later", "heartbeat"})


@pytest.mark.parametrize("defect", ["execution_id", "terminal_id", "accept_id", "result_id", "missing"])
async def test_result_identities_and_durable_inbox_are_required(store, queued_initial, defect, server_runtime):
    class InvalidResultSocket(ScriptedSocket):
        async def receive_text(self):
            if self._step < 2:
                return await super().receive_text()
            if self._step > 2:
                raise WebSocketDisconnect()
            self._step += 1
            args, epoch = self.assignment()
            terminal, accept = terminal_items(Failed(
                job_id=args.job_id, assignment_token=args.assignment_token, error="failed",
            ))
            if defect == "execution_id":
                terminal.reply_to = "1" * 32
            elif defect == "terminal_id":
                terminal.message_id = "1" * 32
            elif defect == "accept_id":
                accept.message_id = "1" * 32
            elif defect == "result_id":
                accept.arguments.result_id = "1" * 32
            return worker_message(*([accept] if defect == "missing" else [terminal, accept]),
                                  epoch=epoch).model_dump_json()

    socket = InvalidResultSocket()
    await serve_worker(socket, serve=server_runtime().serve_worker_session,
                       worker_tokens={"worker-1": "worker-secret"})
    assert socket.closed == 1008
    assert not any(isinstance(item, AcceptResultResponse) for item in sent_items(socket.sent))
    job = required(store.jobs.get_job(queued_initial[1].id))
    assert (job.result_payload is not None) == (defect in {"accept_id", "result_id"})


@pytest.mark.parametrize("committed,with_accept", [(True, True), (True, False), (False, True)])
async def test_registration_only_answers_actual_committed_acceptance(
    store, prepared_initial, monkeypatch, committed, with_accept, server_runtime,
):
    _flow, subject, _, request, _ = prepared_initial
    job = required(store.jobs.get_job(request.arguments.job_id))
    terminal = Failed(job_id=job.id, assignment_token=job.assignment_token, error="failed offline")
    assert store.workers.record_assignment_result(job.id, "worker-a", job.assignment_token,
                                                  "epoch", terminal) == "pending"
    if committed:
        await subject.results.apply(job.id)
    _, accept = terminal_items(terminal)
    initial = recovery_registration(store, job, RecoveryDescriptor(
        job_id=job.id, assignment_token=job.assignment_token, state="failed",
        result_digest=terminal_digest(terminal),
    ))

    class RegistrationSocket(WaitingSocket):
        def __init__(self):
            super().__init__(worker_id="worker-a", registration=initial)
            self.registered = asyncio.Event()

        async def receive_text(self):
            if self._step == 0:
                return await super().receive_text()
            await self.registered.wait()
            if self._step == 1 and with_accept:
                self._step += 1
                assert not any(isinstance(item, AcceptResultResponse) for item in sent_items(self.sent))
                registered = next(m for m in map(parse_server_message, self.sent) if isinstance(m, Registered))
                return worker_message(accept, epoch=registered.connection_epoch).model_dump_json()
            raise WebSocketDisconnect()

        async def send_text(self, raw):
            await super().send_text(raw)
            if isinstance(parse_server_message(raw), Registered):
                assert (required(store.jobs.get_job(job.id)).result_applied_at is not None) == committed
                self.registered.set()

    async def pending(job_id):
        raise ResultPending("result application is not committed")

    handler, socket = server_runtime(), RegistrationSocket()
    monkeypatch.setattr(handler.results, "_apply", pending)
    await serve_worker(socket, serve=handler.serve_worker_session,
                       worker_tokens={"worker-a": "worker-secret"})
    responses = [item for item in sent_items(socket.sent) if isinstance(item, AcceptResultResponse)]
    assert len(responses) == int(committed and with_accept)
    if responses:
        assert responses[0].reply_to == accept.message_id
    assert (required(store.jobs.get_job(job.id)).result_applied_at is not None) == committed
    registered = next(m for m in map(parse_server_message, socket.sent) if isinstance(m, Registered))
    assert len(registered.recoveries) == 1
    assert registered.recoveries[0].action == "replay" and registered.ready
    assert socket.closed == (1013 if with_accept and not committed else 1000)


async def test_duplicate_prior_job_result_does_not_retire_current_assignment(store, prepared_initial, server_runtime):
    flow, subject, _, request, _ = prepared_initial
    previous = required(store.jobs.get_job(request.arguments.job_id))
    old_terminal = Failed(job_id=previous.id, assignment_token=previous.assignment_token, error="old failure")
    old_t, old_a = terminal_items(old_terminal)
    assert store.workers.record_assignment_result(previous.id, "worker-a", previous.assignment_token,
                                                  "epoch", old_terminal) == "pending"
    await subject.results.apply(previous.id)
    report_settled(store, previous.id, epoch="epoch")
    assert ControlService(database=store.database).retry(flow)
    following = next(job for job in store.jobs.list_jobs(flow.id) if job.state is JobState.QUEUED)
    registration = recovery_registration(store, previous, None)

    class DuplicateSocket(ScriptedSocket):
        async def receive_text(self):
            if self._step == 2:
                self._step += 1
                args, epoch = self.assignment()
                new_t, new_a = terminal_items(Failed(
                    job_id=args.job_id, assignment_token=args.assignment_token, error="new failure",
                ))
                return worker_message(old_t, old_a, new_t, new_a, epoch=epoch).model_dump_json()
            return await super().receive_text()

    socket = DuplicateSocket(worker_id="worker-a", registration=registration)
    await serve_worker(socket, serve=server_runtime().serve_worker_session,
                       worker_tokens={"worker-a": "worker-secret"})
    responses = [item for item in sent_items(socket.sent) if isinstance(item, AcceptResultResponse)]
    assert [item.result.job_id for item in responses] == [previous.id, following.id]
    assert required(store.jobs.get_job(following.id)).result_applied_at is not None
    assert socket.closed == 1000


async def test_split_result_acceptance_orders_same_flow_successor_after_ack(store, queued_initial, server_runtime):
    # Completion releases the DB claim before A. A prepared successor must wait for
    # the ACK that makes this Flow's worker journal replaceable on the ordered writer.
    callbacks = server_callbacks(server_runtime(), "worker-1", "epoch")
    callbacks.session.send = AsyncMock(return_value=True)
    routes = Dispatcher[WorkerMessageUnion]()
    callbacks.bind(routes)
    routes.freeze()
    callbacks.on_published()
    await callbacks.on_opened()
    job = required(store.workers.assign_job("worker-1", "token", "epoch"))
    arguments = await callbacks.coordinator.prepare_for_assignment(job, "worker-1", "token", "epoch")
    await callbacks._on_prepared(job, "token", arguments)
    started = Started(**call_header(), method="server.execution_started",
                      arguments=ExecutionIdentity(job_id=job.id, assignment_token="token"))
    await callbacks.on_started(started, worker_message(started, epoch="epoch"))
    terminal, accept = terminal_items(Failed(job_id=job.id, assignment_token="token", error="failed"))
    await callbacks.on_result(terminal, worker_message(terminal, epoch="epoch"))
    assert required(store.jobs.get_job(job.id)).result_applied_at is not None
    assert ControlService(database=store.database).retry(queued_initial[0])
    report_settled(store, job.id, epoch="epoch")
    following = required(store.workers.assign_job("worker-1", "next", "epoch"))
    preparation = asyncio.create_task(callbacks.coordinator.prepare_for_assignment(
        following, "worker-1", "next", "epoch",
    ))
    await preparation
    callbacks.preparing[following.id] = following, "next", preparation
    try:
        await callbacks.poll()
        assert following.id in callbacks.preparing and following.id not in callbacks.current
        assert required(store.jobs.get_job(following.id)).dispatch_authorized_at is None
        await callbacks.on_accept_result(accept, worker_message(accept, epoch="epoch"))
        await callbacks.poll()
        sent = [item for call in callbacks.session.send.call_args_list for item in call.args[0].payload]
        assert [type(item) for item in sent] == [Assignment, AcceptResultResponse, Assignment]
        assert set(callbacks.current) == {following.id} and not callbacks.preparing
        assert required(store.jobs.get_job(following.id)).dispatch_authorized_at is not None
    finally:
        await callbacks.aclose()


async def test_result_response_cache_is_bounded_and_idempotent_without_inventory_ack(store, server_runtime):
    callbacks = server_callbacks(server_runtime(), "worker-1", "epoch")
    callbacks.session.send = AsyncMock(return_value=True)
    for job_id in range(1, MAX_PAYLOAD_ITEMS * 3):
        reply = AcceptResultResponse(
            **result_header(call_header()["message_id"]), method="server.accept_result",
            result=ResultAcceptance(job_id=job_id, assignment_token="token", state="done"),
        )
        await callbacks._send(reply)
        await callbacks._send(reply.model_copy(update={"message_id": call_header()["message_id"]}))
        last = callbacks.session.send.call_args_list[-2:]
        assert last[0].args[0].payload == last[1].args[0].payload
    assert len(callbacks._sent) == MAX_PAYLOAD_ITEMS * 2


async def test_recovery_rejection_is_correlated_to_register_before_registered(store, completed_open_initial, server_runtime):
    _, _, _, request, _ = completed_open_initial
    job = required(store.jobs.get_job(request.arguments.job_id))
    initial = recovery_registration(store, job, RecoveryDescriptor(
        job_id=job.id, assignment_token="stale", state="started", executor_active=True,
    ))

    class RejectedSocket(WaitingSocket):
        async def receive_text(self):
            if self._step == 0:
                return await super().receive_text()
            raise WebSocketDisconnect()

    socket = RejectedSocket(worker_id="worker-a", registration=initial)
    await serve_worker(socket, serve=server_runtime().serve_worker_session,
                       worker_tokens={"worker-a": "worker-secret"})
    business = [message for message in map(parse_server_message, socket.sent)
                if message.control or message.payload]
    rejected = business[0].payload[0]
    assert isinstance(rejected, RecoveryRejected)
    assert rejected.arguments.register_message_id == initial.message_id
    assert isinstance(business[1], Registered)
    assert required(store.jobs.get_job(job.id)).state is JobState.DONE
