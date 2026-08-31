import asyncio

import pytest
from fastapi import WebSocketDisconnect

from app.control.gateway import SingleWorkerGateway
from app.models import Job, JobKind, JobState
from app.protocol import (
    Assignment,
    ExecutionResult,
    ExecutionTask,
    InitialResult,
    InitialTask,
    Register,
    ResultAccepted,
    Started,
    Succeeded,
    parse_server_message,
)


class RecordingStore:
    def __init__(self) -> None:
        self.job = Job(id=5, flow_id=3, kind=JobKind.INITIAL, state=JobState.QUEUED)
        self.started = False
        self.finished: JobState | None = None
        self.requeued = False

    def assign_job(self, worker_id: str, token: str) -> Job | None:
        if self.job.state is not JobState.QUEUED:
            return None
        self.job.state = JobState.RUNNING
        self.job.assigned_worker_id = worker_id
        self.job.assignment_token = token
        return self.job

    def start_assignment(self, job_id: int, worker_id: str, token: str) -> bool:
        self.started = (job_id, worker_id, token) == (5, "worker-1", self.job.assignment_token)
        return self.started

    def heartbeat_assignment(self, job_id: int, worker_id: str, token: str) -> bool:
        return (job_id, worker_id, token) == (5, "worker-1", self.job.assignment_token)

    def finish_assignment(
        self, job_id: int, worker_id: str, token: str, state: JobState
    ) -> bool:
        if (job_id, worker_id, token) != (5, "worker-1", self.job.assignment_token):
            return False
        self.finished = state
        self.job.state = state
        return True

    def assignment_active(self, job_id: int, worker_id: str, token: str) -> bool:
        return (
            self.job.state is JobState.RUNNING
            and (job_id, worker_id, token) == (5, "worker-1", self.job.assignment_token)
        )

    def requeue_assignment(self, job_id: int, worker_id: str, token: str) -> bool:
        self.requeued = True
        return True

    def fail_assignment(self, job_id: int, worker_id: str, token: str, reason: str) -> bool:
        self.finished = JobState.FAILED
        self.job.state = JobState.FAILED
        return True


class RecordingCoordinator:
    def __init__(self) -> None:
        self.started = False
        self.results: list[InitialResult] = []
        self.failures: list[str] = []

    async def prepare(self, job: Job) -> ExecutionTask | JobState:
        return InitialTask(
            attempt_id=11,
            repo="owner/repo",
            issue_number=8,
            issue_title="Issue",
            issue_body="Body",
            branch="flow/issue-8",
        )

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None:
        assert isinstance(task, InitialTask)
        self.started = True

    async def apply_result(self, job: Job, result: ExecutionResult) -> JobState:
        assert isinstance(result, InitialResult)
        self.results.append(result)
        return JobState.DONE

    async def fail(self, job: Job, reason: str) -> None:
        self.failures.append(reason)


class ScriptedSocket:
    def __init__(self, *, token: str = "worker-secret") -> None:
        self.headers = {
            "x-flowtool-worker-id": "worker-1",
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
            return Register(worker_id="worker-1").model_dump_json()
        assignment = next(
            message
            for raw in self.sent
            if isinstance((message := parse_server_message(raw)), Assignment)
        )
        if self._step == 2:
            return Started(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
            ).model_dump_json()
        if self._step == 3:
            return Succeeded(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                result=InitialResult(
                    session_id="session-1", last_message="implemented", exit_code=0
                ),
            ).model_dump_json()
        raise WebSocketDisconnect()


@pytest.mark.asyncio
async def test_gateway_runs_one_remote_assignment_to_ack() -> None:
    store = RecordingStore()
    coordinator = RecordingCoordinator()
    socket = ScriptedSocket()
    gateway = SingleWorkerGateway(
        store,
        coordinator,
        worker_id="worker-1",
        worker_token="worker-secret",
        heartbeat_interval=10,
        heartbeat_timeout=30,
        poll_interval=0.01,
    )

    await gateway.handle(socket)

    messages = [parse_server_message(raw) for raw in socket.sent]
    assert any(isinstance(message, Assignment) for message in messages)
    assert any(isinstance(message, ResultAccepted) for message in messages)
    assert store.started is True
    assert store.finished is JobState.DONE
    assert coordinator.started is True
    assert coordinator.results[0].session_id == "session-1"


@pytest.mark.asyncio
async def test_gateway_rejects_bad_credentials_before_accepting() -> None:
    socket = ScriptedSocket(token="wrong")
    gateway = SingleWorkerGateway(
        RecordingStore(),
        RecordingCoordinator(),
        worker_id="worker-1",
        worker_token="worker-secret",
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
    gateway = SingleWorkerGateway(
        RecordingStore(),
        RecordingCoordinator(),
        worker_id="worker-1",
        worker_token="worker-secret",
    )

    await gateway.handle(socket)

    assert socket.accepted is True
    assert socket.closed == 1008
    assert any(message.type == "error" for message in map(parse_server_message, socket.sent))


class WaitingSocket(ScriptedSocket):
    def __init__(self) -> None:
        super().__init__()
        self.accepted_event = asyncio.Event()

    async def accept(self) -> None:
        await super().accept()
        self.accepted_event.set()

    async def receive_text(self) -> str:
        if self._step == 0:
            self._step = 1
            return Register(worker_id="worker-1").model_dump_json()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_gateway_rejects_a_second_legal_worker_connection() -> None:
    store = RecordingStore()
    store.job.state = JobState.DONE
    gateway = SingleWorkerGateway(
        store,
        RecordingCoordinator(),
        worker_id="worker-1",
        worker_token="worker-secret",
        poll_interval=60,
    )
    first = WaitingSocket()
    first_session = asyncio.create_task(gateway.handle(first))
    await first.accepted_event.wait()
    second = ScriptedSocket()
    try:
        await gateway.handle(second)
    finally:
        first_session.cancel()
        await asyncio.gather(first_session, return_exceptions=True)

    assert second.accepted is False
    assert second.closed == 1008


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
    coordinator = RecordingCoordinator()
    gateway = SingleWorkerGateway(
        store,
        coordinator,
        worker_id="worker-1",
        worker_token="worker-secret",
        poll_interval=0.01,
    )

    await gateway.handle(DisconnectingSocket(disconnect_after_start=disconnect_after_start))

    assert store.requeued is (not disconnect_after_start)
    assert (store.finished is JobState.FAILED) is disconnect_after_start
    assert bool(coordinator.failures) is disconnect_after_start
