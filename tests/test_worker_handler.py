import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import WebSocketDisconnect
from pydantic import SecretStr

from app.communication.gateway import Gateway
from app.contracts.conversation import ConversationFailure, HistoryQuery
from app.contracts.protocol import (
    AcceptResult,
    AcceptResultArguments,
    Assignment,
    AssignmentArguments,
    AssignmentSecret,
    ConversationResult,
    ExecuteResult,
    ExecutionIdentity,
    ExecutionTask,
    Failed,
    FlowToolArguments,
    FlowToolRequest,
    FlowToolResponse,
    Heartbeat,
    InitialTask,
    InitialTaskResult,
    ProtocolError,
    PushSecret,
    RecoveryDescriptor,
    RecoveryRejected,
    Register,
    Registered,
    ResultAccepted,
    ServerMessageUnion,
    ServerPayload,
    Started,
    Succeeded,
    TerminalPayload,
    WorkerMessage,
    WorkerMessageUnion,
    WorkerPayload,
    accept_result_id,
    call_header,
    execution_call_id,
    message_header,
    parse_server_message,
    result_header,
    terminal_digest,
    terminal_id,
)
from app.contracts.receipts import ResultInbox, decode_result_inbox
from app.contracts.result_models import ResultPending
from app.controler.runtime import ServerRuntime
from app.controler.workers.conversation import ConversationBroker
from app.controler.workers.transport import serve_worker
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.tools import FlowToolService
from app.domain.delivery_models import PreparedDelivery
from app.domain.models import (
    FlowDelivery,
    InitialCodingOutput,
    Job,
    JobKind,
    JobState,
    VcsKind,
    utcnow,
)
from app.persistence.stops import StopStore
from app.persistence.store import Store
from tests.persistence_fakes import RepositoryViews
from tests.test_communication_session import Socket
from tests.test_conversation_history import SESSION
from tests.worker_fakes import NoPendingStops, connected_worker, result_application, server_runtime


def register_message(
    worker_id: str = "worker-1",
    recovery: RecoveryDescriptor | None = None,
    payload: list[WorkerPayload] | None = None,
) -> Register:
    return Register(
        **message_header(None),
        control="register",
        worker_id=worker_id,
        recovery=recovery,
        payload=payload or [],
    )


def worker_message(epoch: str, *items: WorkerPayload) -> WorkerMessage:
    return WorkerMessage(**message_header(epoch), control=None, payload=list(items))


def terminal_items(terminal: TerminalPayload) -> tuple[ExecuteResult, AcceptResult]:
    identity = terminal_id(terminal)
    result = ExecuteResult(
        **result_header(execution_call_id(terminal.job_id, terminal.assignment_token), identity),
        method="worker.execute",
        job_id=terminal.job_id,
        assignment_token=terminal.assignment_token,
        result=terminal.result if isinstance(terminal, Succeeded) else None,
        error=terminal.error if isinstance(terminal, Failed) else None,
    )
    accept = AcceptResult(
        **call_header(accept_result_id(identity)),
        method="server.accept_result",
        arguments=AcceptResultArguments(
            job_id=terminal.job_id, assignment_token=terminal.assignment_token, result_id=identity
        ),
    )
    return result, accept


def sent_items(raws: list[str]) -> list[ServerPayload]:
    return [item for raw in raws for item in parse_server_message(raw).payload]


class RecordingStore(RepositoryViews, Store):
    @property
    def stops(self) -> StopStore:
        return NoPendingStops()

    def __init__(self) -> None:
        self.job = Job(id=5, flow_id=3, kind=JobKind.INITIAL, state=JobState.QUEUED)
        self.started = False
        self.finished: JobState | None = None
        self.requeued = False
        self.presence: dict[str, str] = {}
        self.connection_epoch = ""
        self.resumed = False

    def get_job(self, job_id: int) -> Job | None:
        return self.job if self.job.id == job_id else None

    def get_delivery(self, flow_id: int) -> FlowDelivery | None:
        return None

    def get_worker_execution(self, worker_id: str):
        return None

    def worker_connected(self, worker_id: str, protocol_version: int, connection_epoch: str = ""):
        self.presence[worker_id] = "online"
        self.connection_epoch = connection_epoch
        return connected_worker(worker_id, protocol_version, connection_epoch)

    def worker_disconnected(
        self,
        worker_id: str,
        reason: str | None = None,
        connection_epoch: str = "",
    ) -> None:
        self.presence[worker_id] = "offline"

    def assign_job(self, worker_id: str, token: str, connection_epoch: str = "") -> Job | None:
        if self.job.state is not JobState.QUEUED:
            return None
        self.job.state = JobState.RUNNING
        self.job.assigned_worker_id = worker_id
        self.job.assignment_token = token
        return self.job

    def start_assignment(
        self,
        job_id: int,
        attempt_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str = "",
    ) -> bool:
        self.started = (job_id, worker_id, token) == (5, "worker-1", self.job.assignment_token)
        return self.started

    def heartbeat_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str = ""
    ) -> bool:
        return (job_id, worker_id, token) == (5, "worker-1", self.job.assignment_token)

    def resume_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str
    ) -> bool:
        self.resumed = self.assignment_active(job_id, worker_id, token)
        return self.resumed

    def record_assignment_result(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str,
        terminal: TerminalPayload,
    ) -> str:
        if (job_id, worker_id, token, connection_epoch) != (
            self.job.id,
            self.job.assigned_worker_id,
            self.job.assignment_token,
            self.connection_epoch,
        ):
            return "rejected"
        digest = terminal_digest(terminal)
        if self.job.result_digest is not None:
            if digest != self.job.result_digest:
                return "conflict"
            return "applied" if self.job.result_applied_at is not None else "pending"
        self.job.result_payload = ResultInbox(source="worker", terminal=terminal).model_dump(
            mode="json"
        )
        self.job.result_digest = digest
        self.job.result_received_at = self.job.updated_at
        return "pending"

    def unapplied_result_ceiling(self) -> int:
        return 0

    def list_unapplied_results(
        self,
        *,
        after_id: int = 0,
        through_id: int | None = None,
        limit: int = 64,
    ) -> list[Job]:
        return []

    def complete_persisted_result(self, job_id: int, state: JobState) -> bool:
        self.job.state = state
        return True

    def finish_assignment(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        state: JobState,
        connection_epoch: str = "",
    ) -> bool:
        if (job_id, worker_id, token) != (5, "worker-1", self.job.assignment_token):
            return False
        self.finished = state
        self.job.state = state
        return True

    def assignment_active(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str = ""
    ) -> bool:
        return self.job.state is JobState.RUNNING and (job_id, worker_id, token) == (
            5,
            "worker-1",
            self.job.assignment_token,
        )

    def requeue_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str = ""
    ) -> bool:
        self.requeued = True
        return True


class RecordingCoordinator(JobCoordinator):
    async def prepare_for_assignment(self, job, worker, token, epoch):
        task = await self.prepare(job)
        if isinstance(task, JobState):
            return task
        task = task.model_copy(update={"repo_identity": f"{worker}:{task.repo}"})
        return AssignmentArguments(
            job_id=job.id,
            flow_id=job.flow_id,
            assignment_token=token,
            task=task,
            secret=AssignmentSecret(
                push=PushSecret(
                    token=SecretStr("push-token"),
                    repository=task.repo,
                    expires_at=utcnow() + timedelta(hours=1),
                )
            ),
        )

    def __init__(self, store: RecordingStore) -> None:
        self.jobs = store.jobs
        self.started = False
        self.received_results: list[InitialTaskResult] = []
        self.tools = Mock(spec=FlowToolService, execute=AsyncMock(side_effect=self._tool_response))
        self.results = result_application(store, self._commit_result)
        self.failures: list[str] = []

    async def prepare(self, job: Job) -> ExecutionTask | JobState:
        return InitialTask(
            flow_id=job.flow_id,
            attempt_id=11,
            repo="owner/repo",
            issue_number=8,
            issue_title="Issue",
            issue_body="Body",
            branch="flow/issue-8",
        )

    async def _tool_response(
        self,
        request: FlowToolRequest,
        *,
        worker_id: str,
        connection_epoch: str,
    ) -> FlowToolResponse:
        assert worker_id == "worker-1"
        return FlowToolResponse(
            **result_header(request.message_id),
            method=request.method,
            call_id=request.arguments.call_id,
            result=PreparedDelivery(head_sha="abc", delivery_version=1),
        )

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None:
        assert isinstance(task, InitialTask)
        self.started = True

    async def _commit_result(self, job_id: int) -> JobState:
        job = self.jobs.get_job(job_id)
        assert job is not None and job.result_payload is not None and job.result_digest is not None
        terminal = decode_result_inbox(
            job.result_payload,
            job_id=job.id,
            assignment_token=job.assignment_token,
            result_digest=job.result_digest,
        ).terminal
        if isinstance(terminal, Failed):
            job.state, job.result_applied_at = JobState.FAILED, job.updated_at
            return job.state
        result = terminal.result
        assert isinstance(result, InitialTaskResult)
        self.received_results.append(result)
        # This transport fixture shares its stored Job object with the coordinator.
        job.state = JobState.DONE if result.exit_code == 0 else JobState.FAILED
        job.result_applied_at = job.updated_at
        return job.state

    async def fail(self, job: Job, reason: str) -> None:
        self.failures.append(reason)
        job.state = JobState.FAILED
        job.result_applied_at = job.updated_at


class ScriptedSocket:
    def __init__(self, *, worker_id: str = "worker-1", token: str = "worker-secret") -> None:
        self.worker_id = worker_id
        self.headers = {
            "x-flowtool-worker-id": worker_id,
            "authorization": f"Bearer {token}",
        }
        self.accepted = False
        self.closed: int | None = None
        self.sent: list[str] = []
        self._step = 0
        self.assignment_sent = asyncio.Event()
        self.tool_responded = asyncio.Event()

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed = code

    async def send_text(self, data: str) -> None:
        self.sent.append(data)
        for item in parse_server_message(data).payload:
            if isinstance(item, Assignment):
                self.assignment_sent.set()
            if isinstance(item, FlowToolResponse):
                self.tool_responded.set()

    def assignment(self) -> tuple[AssignmentArguments, str]:
        for raw in self.sent:
            message = parse_server_message(raw)
            for item in message.payload:
                if isinstance(item, Assignment):
                    assert message.connection_epoch is not None
                    return item.arguments, message.connection_epoch
        raise AssertionError("assignment not sent")

    async def receive_text(self) -> str:
        self._step += 1
        if self._step == 1:
            return register_message(self.worker_id).model_dump_json()
        await self.assignment_sent.wait()
        assignment, epoch = self.assignment()
        if self._step == 2:
            return worker_message(
                epoch,
                Started(
                    **call_header(),
                    method="server.execution_started",
                    arguments=ExecutionIdentity(
                        job_id=assignment.job_id, assignment_token=assignment.assignment_token
                    ),
                ),
            ).model_dump_json()
        if self._step == 3:
            return worker_message(
                epoch,
                *terminal_items(
                    Succeeded(
                        job_id=assignment.job_id,
                        assignment_token=assignment.assignment_token,
                        result=InitialTaskResult(
                            session_id="session-1", last_message="execution failed", exit_code=1
                        ),
                    )
                ),
            ).model_dump_json()
        raise WebSocketDisconnect()


class ToolSocket(ScriptedSocket):
    async def receive_text(self) -> str:
        self._step += 1
        if self._step == 1:
            return register_message(self.worker_id).model_dump_json()
        await self.assignment_sent.wait()
        assignment, epoch = self.assignment()
        assert isinstance(assignment.task, InitialTask)
        if self._step == 2:
            return worker_message(
                epoch,
                Started(
                    **call_header(),
                    method="server.execution_started",
                    arguments=ExecutionIdentity(
                        job_id=assignment.job_id, assignment_token=assignment.assignment_token
                    ),
                ),
            ).model_dump_json()
        if self._step == 3:
            return worker_message(
                epoch,
                FlowToolRequest(
                    **call_header(),
                    method="flow.open_pr",
                    arguments=FlowToolArguments(
                        job_id=assignment.job_id,
                        flow_id=assignment.flow_id,
                        attempt_id=assignment.task.attempt_id,
                        assignment_token=assignment.assignment_token,
                        call_id="call-1",
                        arguments={"title": "Implement", "body": "Done"},
                        vcs_backend=VcsKind.GIT,
                        branch=assignment.task.branch,
                        commit_sha="abc",
                        remote_sha="abc",
                    ),
                ),
            ).model_dump_json()
        if self._step == 4:
            await self.tool_responded.wait()
            return worker_message(
                epoch,
                *terminal_items(
                    Succeeded(
                        job_id=assignment.job_id,
                        assignment_token=assignment.assignment_token,
                        result=InitialTaskResult(
                            session_id="session-1",
                            last_message=InitialCodingOutput(
                                summary="implemented",
                                vcs_backend_used=VcsKind.GIT,
                            ).model_dump_json(),
                            exit_code=0,
                            vcs_backend=VcsKind.GIT,
                            branch=assignment.task.branch,
                            commit_sha="abc",
                            remote_sha="abc",
                            pushed=True,
                            open_pr_call_id="call-1",
                            delivery_receipt=PreparedDelivery(head_sha="abc", delivery_version=1),
                        ),
                    )
                ),
            ).model_dump_json()
        raise WebSocketDisconnect()


@pytest.mark.asyncio
async def test_gateway_runs_one_remote_assignment_to_ack() -> None:
    store = RecordingStore()
    coordinator = RecordingCoordinator(store)
    socket = ScriptedSocket()
    handler = server_runtime(
        coordinator,
        database=store.database,
        peers=Gateway(),
        heartbeat_interval=10,
        heartbeat_timeout=30,
        poll_interval=0.01,
        tools=coordinator.tools,
        results=coordinator.results,
    )

    await serve_worker(socket, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"})

    messages = sent_items(socket.sent)
    assert any(isinstance(message, Assignment) for message in messages)
    assert any(isinstance(message, ResultAccepted) for message in messages)
    assert store.started is True
    assert store.job.state is JobState.FAILED
    assert store.job.result_applied_at is not None
    assert coordinator.started is True
    assert coordinator.received_results[0].session_id == "session-1"


async def test_gateway_authorizes_dispatch_after_preparation(monkeypatch):
    store = RecordingStore()

    class ChangedCoordinator(RecordingCoordinator):
        async def prepare_for_assignment(self, job, worker, token, epoch):
            assignment = await super().prepare_for_assignment(job, worker, token, epoch)
            store.job.assignment_token = "reassigned-token"
            return assignment

    monkeypatch.setattr(
        NoPendingStops,
        "authorize_dispatch",
        lambda self, job_id, worker_id, token, epoch: store.workers.assignment_active(
            job_id, worker_id, token
        ),
    )

    socket = ScriptedSocket()
    gateway_coordinator = ChangedCoordinator(store)
    handler = server_runtime(
        gateway_coordinator,
        database=store.database,
        peers=Gateway(),
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
    )
    await serve_worker(socket, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"})
    assert not any(isinstance(item, Assignment) for item in sent_items(socket.sent))
    assert not store.started


@pytest.mark.asyncio
async def test_gateway_multiplexes_flow_tool_calls_during_execution() -> None:
    socket = ToolSocket()
    store = RecordingStore()
    gateway_coordinator = RecordingCoordinator(store)
    handler = server_runtime(
        gateway_coordinator,
        database=store.database,
        peers=Gateway(),
        poll_interval=0.01,
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
    )

    await serve_worker(socket, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"})

    responses = sent_items(socket.sent)
    assert any(isinstance(message, FlowToolResponse) for message in responses)
    assert any(isinstance(message, ResultAccepted) for message in responses)
    assert store.job.state is JobState.DONE


async def test_gateway_never_acknowledges_an_uncommitted_result(monkeypatch):
    store = RecordingStore()
    coordinator = RecordingCoordinator(store)
    socket = ScriptedSocket()

    async def unfinished(*args):
        raise ResultPending("GitHub outcome requires reconciliation")

    monkeypatch.setattr(coordinator.results, "_apply", unfinished)
    handler = server_runtime(
        coordinator,
        database=store.database,
        peers=Gateway(),
        tools=coordinator.tools,
        results=coordinator.results,
    )
    await serve_worker(socket, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"})
    assert store.job.result_payload is not None
    assert store.job.result_applied_at is None
    assert not any(isinstance(item, ResultAccepted) for item in sent_items(socket.sent))
    assert socket.closed == 1013


async def test_recovery_continues_after_one_broken_result():
    class RecoveryStore(RecordingStore):
        def __init__(self):
            super().__init__()
            self.job_records = [self.job, self.job.model_copy(update={"id": 6, "flow_id": 4})]
            for job in self.job_records:
                job.state, job.assigned_worker_id = JobState.RUNNING, f"worker-{job.id}"
                job.assignment_token = f"token-{job.id}"
                job.started_at, job.result_received_at = job.created_at, job.updated_at
                result = Succeeded(
                    job_id=job.id,
                    assignment_token=job.assignment_token,
                    result=InitialTaskResult(
                        session_id="session", last_message="execution failed", exit_code=1
                    ),
                )
                job.result_payload = ResultInbox(source="worker", terminal=result).model_dump(
                    mode="json"
                )
                job.result_digest = terminal_digest(result)

        def unapplied_result_ceiling(self):
            return max(job.id for job in self.job_records)

        def list_unapplied_results(self, *, after_id=0, through_id=None, limit=64):
            return [
                job
                for job in self.job_records
                if job.id > after_id and (through_id is None or job.id <= through_id)
            ][:limit]

        def get_job(self, job_id):
            return next(job for job in self.job_records if job.id == job_id)

    class RecoveryCoordinator(RecordingCoordinator):
        async def _commit_result(self, job_id):
            if job_id == 5:
                raise RuntimeError("broken local result")
            return await super()._commit_result(job_id)

    store = RecoveryStore()
    processor = result_application(store, RecoveryCoordinator(store)._commit_result)
    await processor.recover_pending_results()
    assert store.job_records[0].result_applied_at is None
    assert store.job_records[1].result_applied_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("with_accept", [False, True])
async def test_gateway_rejects_bad_credentials_before_accepting(
    with_accept: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = ScriptedSocket(token="wrong")
    terminal = Failed(job_id=5, assignment_token="token", error="offline")
    _, accept = terminal_items(terminal)
    initial = register_message(
        recovery=RecoveryDescriptor(
            job_id=5,
            assignment_token="token",
            state="failed",
            result_digest=terminal_digest(terminal),
        )
        if with_accept
        else None,
        payload=[accept] if with_accept else [],
    )
    receive = AsyncMock(return_value=initial.model_dump_json())
    monkeypatch.setattr(socket, "receive_text", receive)
    store = RecordingStore()
    gateway_coordinator = RecordingCoordinator(store)
    handler = server_runtime(
        gateway_coordinator,
        database=store.database,
        peers=Gateway(),
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
    )

    await serve_worker(socket, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"})

    assert socket.accepted is False
    assert socket.closed == 1008
    receive.assert_not_awaited()


class InvalidRegisterSocket(ScriptedSocket):
    async def receive_text(self) -> str:
        return '{"type":"register","protocol_version":99,"worker_id":"worker-1"}'


@pytest.mark.asyncio
async def test_gateway_closes_messages_with_an_incompatible_protocol_version() -> None:
    socket = InvalidRegisterSocket()
    store = RecordingStore()
    gateway_coordinator = RecordingCoordinator(store)
    handler = server_runtime(
        gateway_coordinator,
        database=store.database,
        peers=Gateway(),
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
    )

    await serve_worker(socket, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"})

    assert socket.accepted is True
    assert socket.closed == 1008
    assert any(message.control == "error" for message in map(parse_server_message, socket.sent))


class WaitingSocket(ScriptedSocket):
    async def receive_text(self) -> str:
        if self._step == 0:
            self._step = 1
            return register_message(self.worker_id).model_dump_json()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_disconnected_predecessor_does_not_reject_replacement() -> None:
    # The peer may already be gone when replacement closes its still-registered Session.
    class ObservedSocket(WaitingSocket):
        def __init__(self) -> None:
            super().__init__()
            self.registered = asyncio.Event()

        async def send_text(self, data: str) -> None:
            await super().send_text(data)
            if isinstance(parse_server_message(data), Registered):
                self.registered.set()

        async def close(self, code: int = 1000) -> None:
            await super().close(code)
            if self is first:
                raise WebSocketDisconnect(1006)

    store = RecordingStore()
    store.job.state = JobState.DONE
    store.job.result_applied_at = store.job.updated_at
    gateway_coordinator = RecordingCoordinator(store)
    peers = Gateway[WorkerMessageUnion, ServerMessageUnion]()
    handler = server_runtime(
        gateway_coordinator,
        database=store.database,
        peers=peers,
        poll_interval=60,
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
    )
    first, second = ObservedSocket(), ObservedSocket()
    first_session = asyncio.create_task(
        serve_worker(
            first,
            serve=handler.serve_worker_session,
            worker_tokens={"worker-1": "worker-secret"},
        )
    )
    tasks = [first_session]
    try:
        await asyncio.wait_for(first.registered.wait(), 1)
        previous = peers.get("worker-1")
        assert previous is not None
        tasks.append(
            asyncio.create_task(
                serve_worker(
                    second,
                    serve=handler.serve_worker_session,
                    worker_tokens={"worker-1": "worker-secret"},
                )
            )
        )
        await asyncio.wait_for(first_session, 1)
        await asyncio.wait_for(second.registered.wait(), 1)
        current = peers.get("worker-1")
        assert current is not None and current is not previous
        assert current.session.epoch != previous.session.epoch
        assert store.connection_epoch == current.session.epoch
        assert previous.session.closed
        assert first.closed == 1012
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_overlapping_replacements_do_not_republish_old_route():
    # B waits for A's close while C supersedes B. Finishing A must not publish B again.
    closing, release = asyncio.Event(), asyncio.Event()

    class ClosingSocket(Socket):
        def __init__(self, slow=False):
            super().__init__()
            self.slow = slow
            self.headers = {
                "x-flowtool-worker-id": "worker-1",
                "authorization": "Bearer worker-secret",
            }
            self.incoming.put_nowait(register_message().model_dump_json())

        async def accept(self):
            pass

        async def close(self, code=1000):
            if self.slow:
                closing.set()
                await release.wait()

    store = RecordingStore()
    store.job.state = JobState.DONE
    coordinator = RecordingCoordinator(store)
    gateway = Gateway()
    broker = ConversationBroker(gateway)
    handler = server_runtime(
        coordinator,
        database=store.database,
        peers=gateway,
        tools=coordinator.tools,
        results=coordinator.results,
    )
    tokens = {"worker-1": "worker-secret"}
    first, second, third = ClosingSocket(True), ClosingSocket(), ClosingSocket()
    tasks = [asyncio.create_task(serve_worker(first, serve=handler.serve_worker_session, worker_tokens=tokens))]

    async def next_business(socket):
        while True:
            message = parse_server_message(await asyncio.wait_for(socket.sent.get(), 1))
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
        tasks.append(
            asyncio.create_task(serve_worker(second, serve=handler.serve_worker_session, worker_tokens=tokens))
        )
        await asyncio.wait_for(closing.wait(), 1)
        with pytest.raises(ConversationFailure, match="worker_unavailable"):
            await asyncio.wait_for(query, 1)
        only_receipt_acks(second)  # Receipt ACK does not publish a business route.
        tasks.append(
            asyncio.create_task(serve_worker(third, serve=handler.serve_worker_session, worker_tokens=tokens))
        )
        registered = await next_business(third)
        assert isinstance(registered, Registered)
        newest = gateway.get("worker-1")
        assert newest is not None
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks[:2]), 1)
        assert gateway.get("worker-1") is newest
        assert newest.session.epoch == registered.connection_epoch
        only_receipt_acks(second)
        assert store.connection_epoch == newest.session.epoch
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class DisconnectingSocket(ScriptedSocket):
    def __init__(self, *, disconnect_after_start: bool) -> None:
        super().__init__()
        self.disconnect_after_start = disconnect_after_start

    async def receive_text(self) -> str:
        if self._step == 1 and not self.disconnect_after_start:
            raise WebSocketDisconnect()
        if self._step == 2 and self.disconnect_after_start:
            raise WebSocketDisconnect()
        return await super().receive_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect_after_start", [False, True])
async def test_gateway_disconnect_policy(disconnect_after_start: bool) -> None:
    store = RecordingStore()
    coordinator = RecordingCoordinator(store)
    handler = server_runtime(
        coordinator,
        database=store.database,
        peers=Gateway(),
        poll_interval=0.01,
        tools=coordinator.tools,
        results=coordinator.results,
    )

    await serve_worker(
        DisconnectingSocket(disconnect_after_start=disconnect_after_start),
        serve=handler.serve_worker_session,
        worker_tokens={"worker-1": "worker-secret"},
    )

    assert store.requeued is False
    assert store.finished is None
    assert coordinator.failures == []
    assert store.job.state is JobState.RUNNING


class RecoveringSocket(ScriptedSocket):
    def __init__(self):
        super().__init__()
        self.resumed = asyncio.Event()

    async def send_text(self, raw):
        await super().send_text(raw)
        message = parse_server_message(raw)
        if isinstance(message, Registered) and message.recovery is not None:
            self.resumed.set()

    async def receive_text(self) -> str:
        self._step += 1
        if self._step == 1:
            return register_message(
                worker_id=self.worker_id,
                recovery=RecoveryDescriptor(
                    job_id=5,
                    assignment_token="assignment-1",
                    state="started",
                    executor_active=True,
                ),
            ).model_dump_json()
        if self._step == 2:
            await self.resumed.wait()
            resumed = next(
                message
                for raw in self.sent
                if isinstance((message := parse_server_message(raw)), Registered)
                and message.recovery is not None
            )
            assert resumed.recovery is not None
            return Heartbeat(
                **message_header(resumed.connection_epoch),
                control="heartbeat",
                payload=[],
                job_id=resumed.recovery.job_id,
                assignment_token=resumed.recovery.assignment_token,
            ).model_dump_json()
        raise WebSocketDisconnect()


@pytest.mark.asyncio
async def test_gateway_resumes_started_assignment_under_a_new_epoch() -> None:
    store = RecordingStore()
    store.job.state = JobState.RUNNING
    store.job.assigned_worker_id = "worker-1"
    store.job.assignment_token = "assignment-1"
    store.job.started_at = store.job.created_at
    socket = RecoveringSocket()
    gateway_coordinator = RecordingCoordinator(store)
    handler = server_runtime(
        gateway_coordinator,
        database=store.database,
        peers=Gateway(),
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
    )

    await serve_worker(socket, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"})

    messages = [parse_server_message(raw) for raw in socket.sent]
    resumed = next(
        message
        for message in messages
        if isinstance(message, Registered) and message.recovery is not None
    )
    assert resumed.recovery is not None and resumed.recovery.action == "resume"
    assert store.resumed is True
    assert resumed.connection_epoch == store.connection_epoch
    assert store.job.state is JobState.RUNNING


@pytest.mark.parametrize(
    "first",
    [
        worker_message(
            "old",
            ConversationResult(
                **result_header("1" * 32), method="worker.history", error="query_busy"
            ),
        ),
        register_message(worker_id="other-worker"),
    ],
)
async def test_transport_checks_actual_first_message_before_session_dispatch(first):
    # Session's reply dispatcher would hide an unsolicited result before a later Register.
    class BadFirstSocket(ScriptedSocket):
        async def receive_text(self):
            if self._step == 0:
                self._step += 1
                return first.model_dump_json()
            return register_message(self.worker_id).model_dump_json()

    socket = BadFirstSocket()
    serve = AsyncMock()
    await serve_worker(socket, worker_tokens={"worker-1": "worker-secret"}, serve=serve)
    serve.assert_not_called()
    assert socket.closed == 1008
    assert len(socket.sent) == 1
    assert isinstance(parse_server_message(socket.sent[0]), ProtocolError)


@pytest.mark.parametrize("failure", ["disconnected", "runtime", "database", "cancel", "sealed"])
async def test_handler_cleanup_joins_communication_before_presence_and_propagates_errors(failure):
    # Cleanup errors must not re-enter handshake error mapping or strand DB presence.
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

    class PresenceStore(RecordingStore):
        def __init__(self):
            super().__init__()
            self.disconnected = []

        def worker_disconnected(self, worker_id, reason=None, connection_epoch=""):
            assert joined.is_set()
            self.disconnected.append(connection_epoch)
            super().worker_disconnected(worker_id, reason, connection_epoch)
            if failure == "database":
                raise ConnectionError("database disconnect failed")

    store = PresenceStore()
    store.job.state = JobState.DONE
    coordinator = RecordingCoordinator(store)
    peers = Gateway()
    handler = server_runtime(
        coordinator,
        database=store.database,
        peers=peers,
        tools=coordinator.tools,
        results=coordinator.results,
    )
    socket = ClosingSocket()
    if failure == "sealed":
        peers.seal()
    running = asyncio.create_task(
        serve_worker(socket, worker_tokens={"worker-1": "worker-secret"}, serve=handler.serve_worker_session)
    )
    try:
        if failure != "sealed":
            await asyncio.wait_for(registered.wait(), 1)
            assert peers.get("worker-1") is not None
            disconnect.set()
        await asyncio.wait_for(closing.wait(), 1)
        assert not store.disconnected
        if failure == "cancel":
            for _ in range(2):
                running.cancel()
                await asyncio.sleep(0)
                assert not running.done()
        release.set()
        if failure in {"runtime", "database", "cancel"}:
            error = {
                "cancel": asyncio.CancelledError,
                "runtime": RuntimeError,
                "database": ConnectionError,
            }[failure]
            with pytest.raises(error):
                await asyncio.wait_for(running, 1)
        else:
            await asyncio.wait_for(running, 1)
        assert store.disconnected == ([] if failure == "sealed" else [store.connection_epoch])
        assert peers.get("worker-1") is None
        assert all(
            isinstance(message, Registered) or (message.ack and not message.payload)
            for message in map(parse_server_message, socket.sent)
        )
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, peers.close(), return_exceptions=True)


def handler_for(store: RecordingStore) -> ServerRuntime:
    coordinator = RecordingCoordinator(store)
    return server_runtime(
        coordinator,
        database=store.database,
        peers=Gateway(),
        tools=coordinator.tools,
        results=coordinator.results,
        poll_interval=0.01,
    )


@pytest.mark.parametrize("batch", ["started", "heartbeat", "wrong_epoch", "ack", "reversed"])
async def test_control_and_payload_keep_order_and_trailing_acceptance(batch):
    # One read can contain Started/T/A, or a heartbeat/history/T/A. Neither stage changes
    # nor transient history dispatch may discard the remaining lifecycle entries.
    class BatchedSocket(ScriptedSocket):
        async def receive_text(self):
            if self._step < 1 or (self._step == 1 and batch in {"heartbeat", "wrong_epoch", "ack"}):
                return await super().receive_text()
            if self._step > (2 if batch in {"heartbeat", "wrong_epoch", "ack"} else 1):
                raise WebSocketDisconnect()
            self._step += 1
            await self.assignment_sent.wait()
            args, epoch = self.assignment()
            started = Started(
                **call_header(),
                method="server.execution_started",
                arguments=ExecutionIdentity(
                    job_id=args.job_id, assignment_token=args.assignment_token
                ),
            )
            terminal, accept = terminal_items(
                Failed(
                    job_id=args.job_id,
                    assignment_token=args.assignment_token,
                    error="executor exited",
                )
            )
            if batch in {"heartbeat", "wrong_epoch"}:
                history = ConversationResult(
                    **result_header("1" * 32), method="worker.history", error="query_busy"
                )
                return Heartbeat(
                    **message_header("old" if batch == "wrong_epoch" else epoch),
                    control="heartbeat",
                    job_id=args.job_id,
                    assignment_token=args.assignment_token,
                    payload=[history, terminal, accept],
                ).model_dump_json()
            if batch == "ack":
                header = message_header(epoch)
                header["ack"], header["ack_id"] = True, "1" * 32
                return WorkerMessage(
                    **header, control=None, payload=[terminal, accept]
                ).model_dump_json()
            items = (
                [terminal, started, accept] if batch == "reversed" else [started, terminal, accept]
            )
            return worker_message(epoch, *items).model_dump_json()

    store, socket = RecordingStore(), BatchedSocket()
    await serve_worker(
        socket, serve=handler_for(store).serve_worker_session, worker_tokens={"worker-1": "worker-secret"}
    )
    responses = [item for item in sent_items(socket.sent) if isinstance(item, ResultAccepted)]
    if batch in {"reversed", "wrong_epoch"}:
        assert store.started == (batch == "wrong_epoch") and store.job.result_payload is None
        assert responses == [] and socket.closed == 1008
    else:
        assert store.started and store.job.state is JobState.FAILED
        assert len(responses) == 1 and responses[0].result.state == "failed"
        assert socket.closed == 1000


@pytest.mark.parametrize("acceptance", ["later", "heartbeat", "absent", "write_failed"])
async def test_terminal_waits_for_actual_acceptance_write_before_next_assignment(acceptance):
    applied = asyncio.Event()

    class ObservedStore(RecordingStore):
        assignments = 0

        def assign_job(self, worker_id, token, connection_epoch=""):
            self.assignments += 1
            return super().assign_job(worker_id, token, connection_epoch)

        def heartbeat_assignment(self, job_id, worker_id, token, connection_epoch=""):
            # The real repository only extends leases while the job is RUNNING.
            return self.job.state is JobState.RUNNING and super().heartbeat_assignment(
                job_id, worker_id, token, connection_epoch
            )

    class SplitSocket(ScriptedSocket):
        async def receive_text(self):
            if self._step < 2:
                return await super().receive_text()
            self._step += 1
            args, epoch = self.assignment()
            terminal, accept = terminal_items(
                Failed(
                    job_id=args.job_id,
                    assignment_token=args.assignment_token,
                    error="executor exited",
                )
            )
            if self._step == 3:
                return worker_message(epoch, terminal).model_dump_json()
            if self._step == 4:
                await applied.wait()
                assert store.assignments == 1
                assert not any(isinstance(item, ResultAccepted) for item in sent_items(self.sent))
                if acceptance == "heartbeat":
                    # A heartbeat already in flight can arrive after commit but before A.
                    return Heartbeat(
                        **message_header(epoch),
                        control="heartbeat",
                        job_id=args.job_id,
                        assignment_token=args.assignment_token,
                        payload=[],
                    ).model_dump_json()
                if acceptance != "absent":
                    return worker_message(epoch, accept).model_dump_json()
            if self._step == 5 and acceptance == "heartbeat":
                return worker_message(epoch, accept).model_dump_json()
            raise WebSocketDisconnect()

        async def send_text(self, raw):
            if acceptance == "write_failed" and any(
                isinstance(item, ResultAccepted) for item in parse_server_message(raw).payload
            ):
                raise ConnectionError("response lost")
            await super().send_text(raw)

    store, socket = ObservedStore(), SplitSocket()
    handler = handler_for(store)
    apply = handler.results._apply

    async def commit(job_id):
        state = await apply(job_id)
        applied.set()
        return state

    handler.results._apply = commit
    await serve_worker(socket, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"})
    assert store.job.result_applied_at is not None
    assert (store.assignments > 1) == (acceptance in {"later", "heartbeat"})


@pytest.mark.parametrize(
    "defect", ["execution_id", "terminal_id", "accept_id", "result_id", "missing"]
)
async def test_result_identities_and_durable_inbox_are_required(defect):
    class InvalidResultSocket(ScriptedSocket):
        async def receive_text(self):
            if self._step < 2:
                return await super().receive_text()
            if self._step > 2:
                raise WebSocketDisconnect()
            self._step += 1
            args, epoch = self.assignment()
            terminal, accept = terminal_items(
                Failed(job_id=args.job_id, assignment_token=args.assignment_token, error="failed")
            )
            if defect == "execution_id":
                terminal.reply_to = "1" * 32
            elif defect == "terminal_id":
                terminal.message_id = "1" * 32
            elif defect == "accept_id":
                accept.message_id = "1" * 32
            elif defect == "result_id":
                accept.arguments.result_id = "1" * 32
            return worker_message(
                epoch, *([accept] if defect == "missing" else [terminal, accept])
            ).model_dump_json()

    store, socket = RecordingStore(), InvalidResultSocket()
    await serve_worker(
        socket, serve=handler_for(store).serve_worker_session, worker_tokens={"worker-1": "worker-secret"}
    )
    assert socket.closed == 1008
    assert not any(isinstance(item, ResultAccepted) for item in sent_items(socket.sent))
    assert (store.job.result_payload is not None) == (defect in {"accept_id", "result_id"})


@pytest.mark.parametrize("committed,with_accept", [(True, True), (True, False), (False, True)])
async def test_registration_only_answers_actual_committed_acceptance(committed, with_accept):
    store = RecordingStore()
    store.job.state = JobState.FAILED if committed else JobState.RUNNING
    store.job.assigned_worker_id, store.job.assignment_token = "worker-1", "token"
    terminal = Failed(job_id=store.job.id, assignment_token="token", error="failed offline")
    store.job.result_payload = ResultInbox(source="worker", terminal=terminal).model_dump(
        mode="json"
    )
    store.job.result_digest = terminal_digest(terminal)
    store.job.result_received_at = store.job.updated_at
    if committed:
        store.job.result_applied_at = store.job.updated_at
    _, accept = terminal_items(terminal)
    initial = register_message(
        recovery=RecoveryDescriptor(
            job_id=store.job.id,
            assignment_token="token",
            state="failed",
            result_digest=terminal_digest(terminal),
        ),
        payload=[accept] if with_accept else [],
    )

    class RegistrationSocket(WaitingSocket):
        def __init__(self):
            super().__init__()
            self.registered = asyncio.Event()

        async def receive_text(self):
            if self._step == 0:
                self._step += 1
                return initial.model_dump_json()
            await self.registered.wait()
            raise WebSocketDisconnect()

        async def send_text(self, raw):
            await super().send_text(raw)
            if isinstance(parse_server_message(raw), Registered):
                self.registered.set()

    socket, handler = RegistrationSocket(), handler_for(store)
    apply = AsyncMock(side_effect=AssertionError("registration must not apply a pending inbox"))
    handler.results._apply = apply
    await serve_worker(socket, serve=handler.serve_worker_session, worker_tokens={"worker-1": "worker-secret"})
    apply.assert_not_awaited()
    responses = [item for item in sent_items(socket.sent) if isinstance(item, ResultAccepted)]
    assert len(responses) == int(committed and with_accept)
    if responses:
        assert responses[0].reply_to == accept.message_id
    registered = next(
        message for message in map(parse_server_message, socket.sent) if isinstance(message, Registered)
    )
    assert (registered.recovery is None) == committed
    if registered.recovery is not None:
        assert registered.recovery.action == "replay"


async def test_duplicate_prior_job_result_does_not_retire_current_assignment():
    # A lost response may be replayed after the next job has already started on this connection.
    old_terminal = Failed(job_id=4, assignment_token="previous", error="old failure")
    old_t, old_a = terminal_items(old_terminal)

    class PriorStore(RecordingStore):
        def __init__(self):
            super().__init__()
            self.previous = self.job.model_copy(
                update={
                    "id": 4,
                    "state": JobState.FAILED,
                    "assigned_worker_id": "worker-1",
                    "assignment_token": "previous",
                    "result_applied_at": self.job.updated_at,
                    "result_digest": terminal_digest(old_terminal),
                    "result_payload": ResultInbox(
                        source="worker", terminal=old_terminal
                    ).model_dump(mode="json"),
                }
            )

        def get_job(self, job_id):
            return self.previous if job_id == 4 else super().get_job(job_id)

        def record_assignment_result(self, job_id, worker_id, token, connection_epoch, terminal):
            if job_id == 4:
                assert (worker_id, token, connection_epoch, terminal) == (
                    "worker-1",
                    "previous",
                    self.connection_epoch,
                    old_terminal,
                )
                return "applied"
            return super().record_assignment_result(
                job_id, worker_id, token, connection_epoch, terminal
            )

    class DuplicateSocket(ScriptedSocket):
        async def receive_text(self):
            if self._step == 2:
                self._step += 1
                args, epoch = self.assignment()
                new_t, new_a = terminal_items(
                    Failed(
                        job_id=args.job_id,
                        assignment_token=args.assignment_token,
                        error="new failure",
                    )
                )
                return worker_message(epoch, old_t, old_a, new_t, new_a).model_dump_json()
            return await super().receive_text()

    store, socket = PriorStore(), DuplicateSocket()
    await serve_worker(
        socket, serve=handler_for(store).serve_worker_session, worker_tokens={"worker-1": "worker-secret"}
    )
    responses = [item for item in sent_items(socket.sent) if isinstance(item, ResultAccepted)]
    assert [item.result.job_id for item in responses] == [4, 5]
    assert store.job.result_applied_at is not None and socket.closed == 1000


async def test_recovery_rejection_is_correlated_to_register_before_registered():
    initial = register_message(
        recovery=RecoveryDescriptor(
            job_id=5, assignment_token="stale", state="started", executor_active=True
        )
    )

    class RejectedSocket(WaitingSocket):
        async def receive_text(self):
            if self._step == 0:
                self._step += 1
                return initial.model_dump_json()
            raise WebSocketDisconnect()

    store, socket = RecordingStore(), RejectedSocket()
    store.job.state = JobState.DONE
    await serve_worker(
        socket, serve=handler_for(store).serve_worker_session, worker_tokens={"worker-1": "worker-secret"}
    )
    business = [
        message for message in map(parse_server_message, socket.sent) if message.control or message.payload
    ]
    rejected = business[0].payload[0]
    assert isinstance(rejected, RecoveryRejected)
    assert rejected.arguments.register_message_id == initial.message_id
    assert isinstance(business[1], Registered)
