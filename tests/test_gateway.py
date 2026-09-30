import asyncio
from collections.abc import Mapping

import pytest
from fastapi import WebSocketDisconnect

from app.contracts.protocol import (
    PROTOCOL_VERSION,
    Assignment,
    ExecutionTask,
    FlowToolRequest,
    FlowToolResponse,
    Heartbeat,
    InitialResult,
    InitialTask,
    RecoveryDescriptor,
    Register,
    ResultAccepted,
    ResumeAssignment,
    Started,
    Succeeded,
    parse_server_message,
)
from app.contracts.result_models import ResultPending
from app.control.gateway import WorkerGateway
from app.domain.delivery_models import OpenedDelivery
from app.domain.models import DeliveryState, FlowDelivery, Job, JobKind, JobState, VcsKind
from tests.worker_fakes import NoPendingStops, connected_worker


class RecordingStore:
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

    def worker_connected(self, worker_id: str, protocol_version: int, connection_epoch: str):
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

    def assign_job(self, worker_id: str, token: str, connection_epoch: str) -> Job | None:
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
        connection_epoch: str,
    ) -> bool:
        self.started = (job_id, worker_id, token) == (5, "worker-1", self.job.assignment_token)
        return self.started

    def heartbeat_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str
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
        connection_epoch: str,
    ) -> bool:
        if (job_id, worker_id, token) != (5, "worker-1", self.job.assignment_token):
            return False
        self.finished = state
        self.job.state = state
        return True

    def assignment_active(self, job_id: int, worker_id: str, token: str) -> bool:
        return self.job.state is JobState.RUNNING and (job_id, worker_id, token) == (
            5,
            "worker-1",
            self.job.assignment_token,
        )

    def requeue_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str
    ) -> bool:
        self.requeued = True
        return True


class RecordingCoordinator:
    async def prepare_for_assignment(self, job, worker, token, epoch):
        return await self.prepare(job)

    def __init__(self, store: RecordingStore) -> None:
        self.store = store
        self.started = False
        self.results: list[InitialResult] = []
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

    async def assignment_secret(self, job: Job, task: ExecutionTask):
        return None

    async def execute_tool(self, request: FlowToolRequest, *, worker_id: str) -> FlowToolResponse:
        assert worker_id == "worker-1"
        return FlowToolResponse(
            call_id=request.call_id,
            result=OpenedDelivery(pr_number=17, head_sha="abc", url="https://github.com/owner/repo/pull/17", delivery_version=1),
        )

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None:
        assert isinstance(task, InitialTask)
        self.started = True

    async def apply_result(self, job_id: int) -> JobState:
        job = self.store.get_job(job_id)
        assert job is not None and job.result_payload is not None
        result = Succeeded.model_validate(job.result_payload).result
        assert isinstance(result, InitialResult)
        self.results.append(result)
        # This transport fixture shares its stored Job object with the coordinator.
        job.state = JobState.DONE
        job.result_applied_at = job.updated_at
        return JobState.DONE

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

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed = code

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        self._step += 1
        if self._step == 1:
            return Register(worker_id=self.worker_id).model_dump_json()
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
                result=InitialResult(
                    session_id="session-1", last_message="implemented", exit_code=0
                ),
            ).model_dump_json()
        raise WebSocketDisconnect()


class ToolSocket(ScriptedSocket):
    async def receive_text(self) -> str:
        self._step += 1
        if self._step == 1:
            return Register(worker_id=self.worker_id).model_dump_json()
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
                branch="flow/3",
                commit_sha="abc",
                remote_sha="abc",
            ).model_dump_json()
        if self._step == 4:
            assert any(isinstance(parse_server_message(raw), FlowToolResponse) for raw in self.sent)
            return Succeeded(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch,
                result=InitialResult(
                    session_id="session-1", last_message="implemented", exit_code=0
                ),
            ).model_dump_json()
        raise WebSocketDisconnect()


@pytest.mark.asyncio
async def test_gateway_runs_one_remote_assignment_to_ack() -> None:
    store = RecordingStore()
    coordinator = RecordingCoordinator(store)
    socket = ScriptedSocket()
    gateway = WorkerGateway(
        store,
        coordinator, execution_control=NoPendingStops(),
        worker_tokens={"worker-1": "worker-secret"},
        heartbeat_interval=10,
        heartbeat_timeout=30,
        poll_interval=0.01,
    )

    await gateway.handle(socket)

    messages = [parse_server_message(raw) for raw in socket.sent]
    assert any(isinstance(message, Assignment) for message in messages)
    assert any(isinstance(message, ResultAccepted) for message in messages)
    assert store.started is True
    assert store.job.state is JobState.DONE
    assert store.job.result_applied_at is not None
    assert coordinator.started is True
    assert coordinator.results[0].session_id == "session-1"


async def test_gateway_checks_merge_after_capability_preparation(monkeypatch):
    # A merge may commit after prepare() while control is awaiting a push token.
    store = RecordingStore()
    coordinator = RecordingCoordinator(store)
    delivery = FlowDelivery(flow_id=store.job.flow_id, branch="flow/3", state=DeliveryState.OPEN)
    monkeypatch.setattr(store, "get_delivery", lambda _: delivery)

    async def merged_secret(*args):
        delivery.state = DeliveryState.MERGED

    monkeypatch.setattr(coordinator, "assignment_secret", merged_secret)
    gateway = WorkerGateway(store, coordinator, execution_control=NoPendingStops(), worker_tokens={"worker-1": "secret"})
    store.worker_connected("worker-1", PROTOCOL_VERSION, "epoch")
    job = store.assign_job("worker-1", "token", "epoch")
    prepared, secret = await gateway._prepare_assignment(job, "worker-1", "token", "epoch")
    assert prepared is JobState.FAILED and secret is None
    assert coordinator.failures == ["PR merged before task dispatch"]


async def test_gateway_does_not_dispatch_after_capability_fetch_loses_ownership():
    store = RecordingStore()

    class ChangedCoordinator(RecordingCoordinator):
        async def assignment_secret(self, job, task):
            store.job.assignment_token = "reassigned-token"

    socket = ScriptedSocket()
    gateway = WorkerGateway(store, ChangedCoordinator(store), execution_control=NoPendingStops(), worker_tokens={"worker-1": "worker-secret"})
    await gateway.handle(socket)
    assert not any(isinstance(parse_server_message(raw), Assignment) for raw in socket.sent)
    assert not store.started


@pytest.mark.asyncio
async def test_gateway_multiplexes_flow_tool_calls_during_execution() -> None:
    socket = ToolSocket()
    store = RecordingStore()
    gateway = WorkerGateway(
        store,
        RecordingCoordinator(store), execution_control=NoPendingStops(),
        worker_tokens={"worker-1": "worker-secret"},
        poll_interval=0.01,
    )

    await gateway.handle(socket)

    responses = [parse_server_message(raw) for raw in socket.sent]
    assert any(isinstance(message, FlowToolResponse) for message in responses)
    assert any(isinstance(message, ResultAccepted) for message in responses)


@pytest.mark.parametrize("pending", [True, False])
async def test_gateway_never_acknowledges_an_uncommitted_result(pending, monkeypatch):
    store = RecordingStore()
    coordinator = RecordingCoordinator(store)
    socket = ScriptedSocket()

    async def unfinished(*args):
        if pending:
            raise ResultPending("GitHub outcome requires reconciliation")
        return JobState.DONE

    monkeypatch.setattr(coordinator, "apply_result", unfinished)
    gateway = WorkerGateway(store, coordinator, execution_control=NoPendingStops(), worker_tokens={"worker-1": "worker-secret"})
    await gateway.handle(socket)
    assert store.job.result_payload is not None
    assert store.job.result_applied_at is None
    assert not any(isinstance(parse_server_message(raw), ResultAccepted) for raw in socket.sent)
    assert socket.closed == (1013 if pending else 1008)


async def test_recovery_continues_after_one_broken_result():
    class RecoveryStore(RecordingStore):
        def __init__(self):
            super().__init__()
            self.jobs = [self.job, self.job.model_copy(update={"id": 6})]
            for job in self.jobs:
                job.result_payload = Succeeded(
                    job_id=job.id,
                    assignment_token="token",
                    connection_epoch="epoch",
                    result=InitialResult(session_id="session", last_message="done", exit_code=0),
                ).model_dump(mode="json")

        def unapplied_result_ceiling(self):
            return max(job.id for job in self.jobs)

        def list_unapplied_results(self, *, after_id=0, through_id=None, limit=64):
            return [job for job in self.jobs if job.id > after_id
                    and (through_id is None or job.id <= through_id)][:limit]

        def get_job(self, job_id):
            return next(job for job in self.jobs if job.id == job_id)

    class RecoveryCoordinator(RecordingCoordinator):
        async def apply_result(self, job_id):
            if job_id == 5:
                raise RuntimeError("broken local result")
            return await super().apply_result(job_id)

    store = RecoveryStore()
    gateway = WorkerGateway(store, RecoveryCoordinator(store), execution_control=NoPendingStops(), worker_tokens={"worker-1": "secret"})
    await gateway.recover_pending_results()
    assert store.jobs[0].result_applied_at is None
    assert store.jobs[1].result_applied_at is not None


@pytest.mark.asyncio
async def test_gateway_rejects_bad_credentials_before_accepting() -> None:
    socket = ScriptedSocket(token="wrong")
    store = RecordingStore()
    gateway = WorkerGateway(
        store,
        RecordingCoordinator(store), execution_control=NoPendingStops(),
        worker_tokens={"worker-1": "worker-secret"},
    )

    await gateway.handle(socket)

    assert socket.accepted is False
    assert socket.closed == 1008


class InvalidRegisterSocket(ScriptedSocket):
    async def receive_text(self) -> str:
        return '{"type":"register","protocol_version":99,"worker_id":"worker-1"}'


@pytest.mark.asyncio
async def test_gateway_closes_messages_with_an_incompatible_protocol_version() -> None:
    socket = InvalidRegisterSocket()
    store = RecordingStore()
    gateway = WorkerGateway(
        store,
        RecordingCoordinator(store), execution_control=NoPendingStops(),
        worker_tokens={"worker-1": "worker-secret"},
    )

    await gateway.handle(socket)

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
async def test_gateway_new_epoch_takes_over_a_duplicate_worker_connection() -> None:
    store = RecordingStore()
    store.job.state = JobState.DONE
    gateway = WorkerGateway(
        store,
        RecordingCoordinator(store), execution_control=NoPendingStops(),
        worker_tokens={"worker-1": "worker-secret"},
        poll_interval=60,
    )
    first = WaitingSocket()
    first_session = asyncio.create_task(gateway.handle(first))
    await first.accepted_event.wait()
    second = WaitingSocket()
    try:
        second_session = asyncio.create_task(gateway.handle(second))
        await second.accepted_event.wait()
        await asyncio.sleep(0)
    finally:
        first_session.cancel()
        second_session.cancel()
        await asyncio.gather(first_session, second_session, return_exceptions=True)

    assert second.accepted is True
    assert first.closed == 1012


@pytest.mark.asyncio
async def test_gateway_accepts_different_worker_identities_concurrently() -> None:
    store = RecordingStore()
    store.job.state = JobState.DONE
    gateway = WorkerGateway(
        store,
        RecordingCoordinator(store), execution_control=NoPendingStops(),
        worker_tokens={"worker-1": "secret-1", "worker-2": "secret-2"},
        poll_interval=60,
    )
    first = WaitingSocket(worker_id="worker-1", token="secret-1")
    second = WaitingSocket(worker_id="worker-2", token="secret-2")
    sessions = [
        asyncio.create_task(gateway.handle(first)),
        asyncio.create_task(gateway.handle(second)),
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
    gateway = WorkerGateway(
        store,
        coordinator, execution_control=NoPendingStops(),
        worker_tokens={"worker-1": "worker-secret"},
        poll_interval=0.01,
    )

    await gateway.handle(DisconnectingSocket(disconnect_after_start=disconnect_after_start))

    assert store.requeued is False
    assert store.finished is None
    assert coordinator.failures == []
    assert store.job.state is JobState.RUNNING


class RecoveringSocket(ScriptedSocket):
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
    gateway = WorkerGateway(
        store,
        RecordingCoordinator(store), execution_control=NoPendingStops(),
        worker_tokens={"worker-1": "worker-secret"},
    )

    await gateway.handle(socket)

    messages = [parse_server_message(raw) for raw in socket.sent]
    resumed = next(message for message in messages if isinstance(message, ResumeAssignment))
    assert store.resumed is True
    assert resumed.connection_epoch == store.connection_epoch
    assert store.job.state is JobState.RUNNING
