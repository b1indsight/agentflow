import asyncio
from collections.abc import Mapping
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import WebSocketDisconnect
from pydantic import SecretStr

from app.communication.gateway import Gateway
from app.contracts.conversation import ConversationFailure, HistoryQuery
from app.contracts.protocol import (
    Assignment,
    AssignmentSecret,
    ConversationResult,
    ExecutionTask,
    FlowToolRequest,
    FlowToolResponse,
    Heartbeat,
    InitialTask,
    InitialTaskResult,
    ProtocolError,
    PushSecret,
    RecoveryDescriptor,
    Register,
    Registered,
    ResultAccepted,
    ResumeAssignment,
    ServerMessage,
    Started,
    Succeeded,
    WorkerMessage,
    parse_server_message,
    terminal_digest,
)
from app.contracts.result_models import ResultPending
from app.controler.workers.conversation import ConversationBroker
from app.controler.workers.handler import WorkerHandler
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
from tests.worker_fakes import NoPendingStops, connected_worker, result_application


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
        payload: Mapping[str, object],
        digest: str,
    ) -> str:
        if not self.assignment_active(job_id, worker_id, token):
            return "rejected"
        self.job.result_payload = dict(payload)
        self.job.result_digest = digest
        self.job.result_received_at = self.job.updated_at
        return "pending"

    def unapplied_result_ceiling(self) -> int:
        return 0

    def list_unapplied_results(
        self, *, after_id: int = 0, through_id: int | None = None, limit: int = 64,
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

    def assignment_active(self, job_id: int, worker_id: str, token: str, connection_epoch: str = "") -> bool:
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
        return Assignment(job_id=job.id, flow_id=job.flow_id, assignment_token=token,
                          connection_epoch=epoch, task=task,
                          secret=AssignmentSecret(push=PushSecret(token=SecretStr('push-token'),
                              repository=task.repo, expires_at=utcnow() + timedelta(hours=1))))

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

    async def _tool_response(self, request: FlowToolRequest, *, worker_id: str) -> FlowToolResponse:
        assert worker_id == "worker-1"
        return FlowToolResponse(
            call_id=request.call_id,
            result=PreparedDelivery(head_sha="abc", delivery_version=1),
        )

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None:
        assert isinstance(task, InitialTask)
        self.started = True

    async def _commit_result(self, job_id: int) -> JobState:
        job = self.jobs.get_job(job_id)
        assert job is not None and job.result_payload is not None
        result = Succeeded.model_validate(job.result_payload).result
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
        if isinstance(parse_server_message(data), Assignment):
            self.assignment_sent.set()
        if isinstance(parse_server_message(data), FlowToolResponse):
            self.tool_responded.set()

    async def receive_text(self) -> str:
        self._step += 1
        if self._step == 1:
            return Register(worker_id=self.worker_id).model_dump_json()
        await self.assignment_sent.wait()
        assignment = next(
            message
            for raw in self.sent
            if isinstance((message := parse_server_message(raw)), Assignment)
        )
        if self._step == 2:
            return Started(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch,
            ).model_dump_json()
        if self._step == 3:
            return Succeeded(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch,
                result=InitialTaskResult(
                    session_id="session-1", last_message="execution failed", exit_code=1
                ),
            ).model_dump_json()
        raise WebSocketDisconnect()


class ToolSocket(ScriptedSocket):
    async def receive_text(self) -> str:
        self._step += 1
        if self._step == 1:
            return Register(worker_id=self.worker_id).model_dump_json()
        await self.assignment_sent.wait()
        assignment = next(
            message
            for raw in self.sent
            if isinstance((message := parse_server_message(raw)), Assignment)
        )
        assert isinstance(assignment.task, InitialTask)
        if self._step == 2:
            return Started(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch,
            ).model_dump_json()
        if self._step == 3:
            return FlowToolRequest(
                job_id=assignment.job_id,
                flow_id=assignment.flow_id,
                attempt_id=assignment.task.attempt_id,
                assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch,
                call_id="call-1",
                name="flow.open_pr",
                arguments={"title": "Implement", "body": "Done"},
                vcs_backend=VcsKind.GIT,
                branch=assignment.task.branch,
                commit_sha="abc",
                remote_sha="abc",
            ).model_dump_json()
        if self._step == 4:
            await self.tool_responded.wait()
            return Succeeded(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch,
                result=InitialTaskResult(
                    session_id="session-1", last_message=InitialCodingOutput(
                        summary="implemented", vcs_backend_used=VcsKind.GIT,
                    ).model_dump_json(), exit_code=0,
                    vcs_backend=VcsKind.GIT, branch=assignment.task.branch,
                    commit_sha="abc", remote_sha="abc", pushed=True, open_pr_call_id="call-1",
                    delivery_receipt=PreparedDelivery(head_sha="abc", delivery_version=1),
                ),
            ).model_dump_json()
        raise WebSocketDisconnect()


@pytest.mark.asyncio
async def test_gateway_runs_one_remote_assignment_to_ack() -> None:
    store = RecordingStore()
    coordinator = RecordingCoordinator(store)
    socket = ScriptedSocket()
    handler = WorkerHandler(
        coordinator, database=store.database,
        peers=Gateway(),
        heartbeat_interval=10,
        heartbeat_timeout=30,
        poll_interval=0.01,
        tools=coordinator.tools,
        results=coordinator.results,
    )

    await serve_worker(socket, handler=handler, worker_tokens={"worker-1": "worker-secret"})

    messages = [parse_server_message(raw) for raw in socket.sent]
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

    monkeypatch.setattr(NoPendingStops, "authorize_dispatch",
                        lambda self, job_id, worker_id, token, epoch:
                        store.workers.assignment_active(job_id, worker_id, token))

    socket = ScriptedSocket()
    gateway_coordinator = ChangedCoordinator(store)
    handler = WorkerHandler(
        gateway_coordinator, database=store.database,
        peers=Gateway(),
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
    )
    await serve_worker(socket, handler=handler, worker_tokens={"worker-1": "worker-secret"})
    assert not any(isinstance(parse_server_message(raw), Assignment) for raw in socket.sent)
    assert not store.started


@pytest.mark.asyncio
async def test_gateway_multiplexes_flow_tool_calls_during_execution() -> None:
    socket = ToolSocket()
    store = RecordingStore()
    gateway_coordinator = RecordingCoordinator(store)
    handler = WorkerHandler(
        gateway_coordinator, database=store.database,
        peers=Gateway(),
        poll_interval=0.01,
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
    )

    await serve_worker(socket, handler=handler, worker_tokens={"worker-1": "worker-secret"})

    responses = [parse_server_message(raw) for raw in socket.sent]
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
    handler = WorkerHandler(
        coordinator, database=store.database,
        peers=Gateway(),
        tools=coordinator.tools,
        results=coordinator.results,
    )
    await serve_worker(socket, handler=handler, worker_tokens={"worker-1": "worker-secret"})
    assert store.job.result_payload is not None
    assert store.job.result_applied_at is None
    assert not any(isinstance(parse_server_message(raw), ResultAccepted) for raw in socket.sent)
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
                    connection_epoch=f"epoch-{job.id}",
                    result=InitialTaskResult(session_id="session", last_message="execution failed", exit_code=1),
                )
                job.result_payload, job.result_digest = result.model_dump(mode="json"), terminal_digest(result)

        def unapplied_result_ceiling(self):
            return max(job.id for job in self.job_records)

        def list_unapplied_results(self, *, after_id=0, through_id=None, limit=64):
            return [job for job in self.job_records if job.id > after_id
                    and (through_id is None or job.id <= through_id)][:limit]

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
async def test_gateway_rejects_bad_credentials_before_accepting() -> None:
    socket = ScriptedSocket(token="wrong")
    store = RecordingStore()
    gateway_coordinator = RecordingCoordinator(store)
    handler = WorkerHandler(
        gateway_coordinator, database=store.database,
        peers=Gateway(),
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
    )

    await serve_worker(socket, handler=handler, worker_tokens={"worker-1": "worker-secret"})

    assert socket.accepted is False
    assert socket.closed == 1008


class InvalidRegisterSocket(ScriptedSocket):
    async def receive_text(self) -> str:
        return '{"type":"register","protocol_version":99,"worker_id":"worker-1"}'


@pytest.mark.asyncio
async def test_gateway_closes_messages_with_an_incompatible_protocol_version() -> None:
    socket = InvalidRegisterSocket()
    store = RecordingStore()
    gateway_coordinator = RecordingCoordinator(store)
    handler = WorkerHandler(
        gateway_coordinator, database=store.database,
        peers=Gateway(),
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
    )

    await serve_worker(socket, handler=handler, worker_tokens={"worker-1": "worker-secret"})

    assert socket.accepted is True
    assert socket.closed == 1008
    assert any(message.type == "error" for message in map(parse_server_message, socket.sent))


class WaitingSocket(ScriptedSocket):
    def __init__(self, *, worker_id: str = "worker-1", token: str = "worker-secret") -> None:
        super().__init__(worker_id=worker_id, token=token)
        self.accepted_event = asyncio.Event()

    async def accept(self) -> None:
        await super().accept()
        self.accepted_event.set()

    async def receive_text(self) -> str:
        if self._step == 0:
            self._step = 1
            return Register(worker_id=self.worker_id).model_dump_json()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
@pytest.mark.parametrize("predecessor_disconnected", [False, True])
async def test_gateway_new_epoch_takes_over_a_duplicate_worker_connection(
    predecessor_disconnected: bool,
) -> None:
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
            if self is first and predecessor_disconnected:
                raise WebSocketDisconnect(1006)

    store = RecordingStore()
    store.job.state = JobState.DONE
    store.job.result_applied_at = store.job.updated_at
    gateway_coordinator = RecordingCoordinator(store)
    peers = Gateway[WorkerMessage, ServerMessage]()
    handler = WorkerHandler(
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
            handler=handler,
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
                    handler=handler,
                    worker_tokens={"worker-1": "worker-secret"},
                )
            )
        )
        await asyncio.wait_for(first_session, 1)
        await asyncio.wait_for(second.registered.wait(), 1)
        current = peers.get("worker-1")
        assert current is not None and current is not previous
        assert current.epoch != previous.epoch
        assert store.connection_epoch == current.epoch
        assert previous.closed
        assert first.closed == 1012
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_gateway_accepts_different_worker_identities_concurrently() -> None:
    store = RecordingStore()
    store.job.state = JobState.DONE
    store.job.result_applied_at = store.job.updated_at
    gateway_coordinator = RecordingCoordinator(store)
    handler = WorkerHandler(
        gateway_coordinator, database=store.database,
        peers=Gateway(),
        poll_interval=60,
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
    )
    tokens = {"worker-1": "secret-1", "worker-2": "secret-2"}
    first = WaitingSocket(worker_id="worker-1", token="secret-1")
    second = WaitingSocket(worker_id="worker-2", token="secret-2")
    sessions = [
        asyncio.create_task(serve_worker(first, handler=handler, worker_tokens=tokens)),
        asyncio.create_task(serve_worker(second, handler=handler, worker_tokens=tokens)),
    ]
    await asyncio.gather(first.accepted_event.wait(), second.accepted_event.wait())
    try:
        assert first.accepted is True
        assert second.accepted is True
        assert store.presence == {"worker-1": "online", "worker-2": "online"}
    finally:
        for session in sessions:
            session.cancel()
        await asyncio.gather(*sessions, return_exceptions=True)


async def test_overlapping_replacements_do_not_republish_old_route():
    # B waits for A's close while C supersedes B. Finishing A must not publish B again.
    closing, release = asyncio.Event(), asyncio.Event()

    class ClosingSocket(Socket):
        def __init__(self, slow=False):
            super().__init__()
            self.slow = slow
            self.headers = {"x-flowtool-worker-id": "worker-1", "authorization": "Bearer worker-secret"}
            self.incoming.put_nowait(Register(worker_id="worker-1").model_dump_json())

        async def accept(self):
            pass

        async def close(self, code=1000):
            if self.slow:
                closing.set()
                await release.wait()

    store = RecordingStore()
    store.job.state = JobState.DONE
    connected = Mock(wraps=store.worker_connected)
    store.worker_connected = connected
    coordinator = RecordingCoordinator(store)
    gateway = Gateway()
    broker = ConversationBroker(gateway)
    handler = WorkerHandler(coordinator, database=store.database, peers=gateway,
                            tools=coordinator.tools, results=coordinator.results)
    tokens = {"worker-1": "worker-secret"}
    first, second, third = ClosingSocket(True), ClosingSocket(), ClosingSocket()
    tasks = [asyncio.create_task(serve_worker(first, handler=handler, worker_tokens=tokens))]
    try:
        assert isinstance(parse_server_message(await asyncio.wait_for(first.sent.get(), 1)), Registered)
        query = asyncio.create_task(broker.query("worker-1", HistoryQuery(session_id=SESSION)))
        await first.sent.get()
        tasks.append(asyncio.create_task(serve_worker(second, handler=handler, worker_tokens=tokens)))
        await asyncio.wait_for(closing.wait(), 1)
        with pytest.raises(ConversationFailure, match="worker_unavailable"):
            await asyncio.wait_for(query, 1)
        assert second.sent.empty()  # No new registration/route before the old close joins.
        tasks.append(asyncio.create_task(serve_worker(third, handler=handler, worker_tokens=tokens)))
        registered = parse_server_message(await asyncio.wait_for(third.sent.get(), 1))
        assert isinstance(registered, Registered)
        newest = gateway.get("worker-1")
        assert newest is not None
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks[:2]), 1)
        assert gateway.get("worker-1") is newest
        assert newest.epoch == registered.connection_epoch
        assert second.sent.empty()
        assert connected.call_count == 2
        assert connected.call_args.args[2] == newest.epoch
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
    handler = WorkerHandler(
        coordinator, database=store.database,
        peers=Gateway(),
        poll_interval=0.01,
        tools=coordinator.tools,
        results=coordinator.results,
    )

    await serve_worker(DisconnectingSocket(disconnect_after_start=disconnect_after_start),
                       handler=handler, worker_tokens={"worker-1": "worker-secret"})

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
        if isinstance(parse_server_message(raw), ResumeAssignment):
            self.resumed.set()

    async def receive_text(self) -> str:
        self._step += 1
        if self._step == 1:
            return Register(
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
                if isinstance((message := parse_server_message(raw)), ResumeAssignment)
            )
            return Heartbeat(
                job_id=resumed.job_id,
                assignment_token=resumed.assignment_token,
                connection_epoch=resumed.connection_epoch,
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
    handler = WorkerHandler(
        gateway_coordinator, database=store.database,
        peers=Gateway(),
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
    )

    await serve_worker(socket, handler=handler, worker_tokens={"worker-1": "worker-secret"})

    messages = [parse_server_message(raw) for raw in socket.sent]
    resumed = next(message for message in messages if isinstance(message, ResumeAssignment))
    assert store.resumed is True
    assert resumed.connection_epoch == store.connection_epoch
    assert store.job.state is JobState.RUNNING


@pytest.mark.parametrize("first", [
    ConversationResult(request_id="unsolicited", connection_epoch="old", error="query_busy"),
    Register(worker_id="other-worker"),
])
async def test_transport_checks_actual_first_frame_before_session_dispatch(first):
    # Session's reply dispatcher would hide an unsolicited result before a later Register.
    class BadFirstSocket(ScriptedSocket):
        async def receive_text(self):
            if self._step == 0:
                self._step += 1
                return first.model_dump_json()
            return Register(worker_id=self.worker_id).model_dump_json()

    socket = BadFirstSocket()
    handler = Mock(spec=WorkerHandler)
    await serve_worker(socket, worker_tokens={"worker-1": "worker-secret"}, handler=handler)
    handler.handle.assert_not_called()
    assert socket.closed == 1008
    assert len(socket.sent) == 1
    assert isinstance(parse_server_message(socket.sent[0]), ProtocolError)


@pytest.mark.parametrize(
    "failure", ["none", "disconnected", "close", "runtime", "database", "cancel", "sealed"]
)
async def test_handler_cleanup_joins_communication_before_presence_and_propagates_errors(failure):
    # Cleanup errors must not re-enter handshake error mapping or strand DB presence.
    closing, release, joined, registered, disconnect = (asyncio.Event() for _ in range(5))

    class ClosingSocket(WaitingSocket):
        close_calls = 0

        async def receive_text(self):
            if self._step == 0:
                return await super().receive_text()
            await disconnect.wait()
            raise WebSocketDisconnect()

        async def send_text(self, raw):
            await super().send_text(raw)
            registered.set()

        async def close(self, code=1000):
            self.close_calls += 1
            closing.set()
            await release.wait()
            joined.set()
            if failure == "disconnected":
                raise WebSocketDisconnect(1006)
            if failure == "close":
                raise OSError("transport close failed")
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
    handler = WorkerHandler(
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
        serve_worker(socket, worker_tokens={"worker-1": "worker-secret"}, handler=handler)
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
        if failure in {"close", "runtime", "database", "cancel"}:
            error = {"cancel": asyncio.CancelledError, "runtime": RuntimeError}.get(
                failure, OSError
            )
            with pytest.raises(error):
                await asyncio.wait_for(running, 1)
        else:
            await asyncio.wait_for(running, 1)
        assert store.disconnected == ([] if failure == "sealed" else [store.connection_epoch])
        assert peers.get("worker-1") is None
        assert not handler._wakes
        assert socket.close_calls == 1
        assert all(isinstance(parse_server_message(raw), Registered) for raw in socket.sent)
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, peers.close(), return_exceptions=True)
