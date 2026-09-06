import asyncio
from collections.abc import Mapping
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from app.control.gateway import WorkerGateway
from app.models import Job, JobKind, JobState, VcsKind
from app.protocol import (
    Assignment,
    ExecutionResult,
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

    def worker_connected(
        self, worker_id: str, protocol_version: int, connection_epoch: str
    ):
        self.presence[worker_id] = "online"
        self.connection_epoch = connection_epoch
        return SimpleNamespace(current_job_id=None)

    def worker_disconnected(
        self,
        worker_id: str,
        reason: str | None = None,
        connection_epoch: str = "",
    ) -> None:
        self.presence[worker_id] = "offline"

    def assign_job(
        self, worker_id: str, token: str, connection_epoch: str
    ) -> Job | None:
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
        return "pending" if self.assignment_active(job_id, worker_id, token) else "rejected"

    def list_unapplied_results(self) -> list[Job]:
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
        return (
            self.job.state is JobState.RUNNING
            and (job_id, worker_id, token) == (5, "worker-1", self.job.assignment_token)
        )

    def requeue_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str
    ) -> bool:
        self.requeued = True
        return True

    def fail_assignment(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        reason: str,
        connection_epoch: str,
    ) -> bool:
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

    async def execute_tool(
        self, request: FlowToolRequest, *, worker_id: str
    ) -> FlowToolResponse:
        assert worker_id == "worker-1"
        return FlowToolResponse(
            call_id=request.call_id,
            result={"pr_number": 17, "head_sha": "abc"},
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
            assert any(
                isinstance(parse_server_message(raw), FlowToolResponse) for raw in self.sent
            )
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
    coordinator = RecordingCoordinator()
    socket = ScriptedSocket()
    gateway = WorkerGateway(
        store,
        coordinator,
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
    assert store.finished is JobState.DONE
    assert coordinator.started is True
    assert coordinator.results[0].session_id == "session-1"


@pytest.mark.asyncio
async def test_gateway_multiplexes_flow_tool_calls_during_execution() -> None:
    socket = ToolSocket()
    gateway = WorkerGateway(
        RecordingStore(),
        RecordingCoordinator(),
        worker_tokens={"worker-1": "worker-secret"},
        poll_interval=0.01,
    )

    await gateway.handle(socket)

    responses = [parse_server_message(raw) for raw in socket.sent]
    assert any(isinstance(message, FlowToolResponse) for message in responses)
    assert any(isinstance(message, ResultAccepted) for message in responses)


@pytest.mark.asyncio
async def test_gateway_rejects_bad_credentials_before_accepting() -> None:
    socket = ScriptedSocket(token="wrong")
    gateway = WorkerGateway(
        RecordingStore(),
        RecordingCoordinator(),
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
    gateway = WorkerGateway(
        RecordingStore(),
        RecordingCoordinator(),
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
        RecordingCoordinator(),
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
        RecordingCoordinator(),
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
    coordinator = RecordingCoordinator()
    gateway = WorkerGateway(
        store,
        coordinator,
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
        RecordingCoordinator(),
        worker_tokens={"worker-1": "worker-secret"},
    )

    await gateway.handle(socket)

    messages = [parse_server_message(raw) for raw in socket.sent]
    resumed = next(message for message in messages if isinstance(message, ResumeAssignment))
    assert store.resumed is True
    assert resumed.connection_epoch == store.connection_epoch
    assert store.job.state is JobState.RUNNING
