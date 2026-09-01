import asyncio
from collections.abc import Mapping

import pytest

from app.control.gateway import WorkerGateway
from app.models import Job, JobKind, JobState
from app.protocol import (
    ExecutionResult,
    ExecutionTask,
    InitialResult,
    InitialTask,
)
from app.worker.client import WorkerClient


class QueueSocket:
    def __init__(self, worker_to_serve: asyncio.Queue[str], serve_to_worker: asyncio.Queue[str]):
        self._incoming = worker_to_serve
        self._outgoing = serve_to_worker
        self._headers = {
            "x-flowtool-worker-id": "worker-1",
            "authorization": "Bearer worker-secret",
        }

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers

    async def accept(self) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        pass

    async def send_text(self, data: str) -> None:
        await self._outgoing.put(data)

    async def receive_text(self) -> str:
        return await self._incoming.get()


class QueueConnection:
    def __init__(self, worker_to_serve: asyncio.Queue[str], serve_to_worker: asyncio.Queue[str]):
        self._outgoing = worker_to_serve
        self._incoming = serve_to_worker

    async def send(self, data: str) -> None:
        await self._outgoing.put(data)

    async def recv(self) -> str:
        return await self._incoming.get()


class RemoteStore:
    def __init__(self) -> None:
        self.job = Job(id=5, flow_id=3, kind=JobKind.INITIAL, state=JobState.QUEUED)
        self.done = asyncio.Event()

    def worker_connected(self, worker_id: str, protocol_version: int):
        pass

    def worker_disconnected(self, worker_id: str, reason: str | None = None) -> None:
        pass

    def assign_job(self, worker_id: str, token: str) -> Job | None:
        if self.job.state is not JobState.QUEUED:
            return None
        self.job.state = JobState.RUNNING
        self.job.assigned_worker_id = worker_id
        self.job.assignment_token = token
        return self.job

    def start_assignment(
        self, job_id: int, attempt_id: int, worker_id: str, token: str
    ) -> bool:
        return self.assignment_active(job_id, worker_id, token)

    def heartbeat_assignment(self, job_id: int, worker_id: str, token: str) -> bool:
        return self.assignment_active(job_id, worker_id, token)

    def assignment_active(self, job_id: int, worker_id: str, token: str) -> bool:
        return (
            self.job.state is JobState.RUNNING
            and self.job.id == job_id
            and self.job.assigned_worker_id == worker_id
            and self.job.assignment_token == token
        )

    def finish_assignment(
        self, job_id: int, worker_id: str, token: str, state: JobState
    ) -> bool:
        if not self.assignment_active(job_id, worker_id, token):
            return False
        self.job.state = state
        self.done.set()
        return True

    def requeue_assignment(self, job_id: int, worker_id: str, token: str) -> bool:
        self.job.state = JobState.QUEUED
        return True

    def fail_assignment(self, job_id: int, worker_id: str, token: str, reason: str) -> bool:
        self.job.state = JobState.FAILED
        self.done.set()
        return True


class RemoteCoordinator:
    def __init__(self) -> None:
        self.result: InitialResult | None = None

    async def prepare(self, job: Job) -> ExecutionTask | JobState:
        return InitialTask(
            attempt_id=7,
            repo="owner/repo",
            issue_number=8,
            issue_title="Issue",
            issue_body="Body",
            branch="flow/issue-8",
        )

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None:
        pass

    async def apply_result(self, job: Job, result: ExecutionResult) -> JobState:
        assert isinstance(result, InitialResult)
        self.result = result
        return JobState.DONE

    async def fail(self, job: Job, reason: str) -> None:
        pass


class FakeExecutor:
    async def execute(self, task: ExecutionTask) -> ExecutionResult:
        assert isinstance(task, InitialTask)
        return InitialResult(session_id="session-1", last_message="done", exit_code=0)


@pytest.mark.asyncio
async def test_control_gateway_and_worker_client_complete_one_remote_job() -> None:
    worker_to_serve: asyncio.Queue[str] = asyncio.Queue()
    serve_to_worker: asyncio.Queue[str] = asyncio.Queue()
    store = RemoteStore()
    coordinator = RemoteCoordinator()
    gateway = WorkerGateway(
        store,
        coordinator,
        worker_tokens={"worker-1": "worker-secret"},
        poll_interval=0.01,
    )
    client = WorkerClient(
        worker_id="worker-1",
        worker_token="worker-secret",
        serve_ws_url="ws://unused",
        executor=FakeExecutor(),
    )

    tasks = [
        asyncio.create_task(gateway.handle(QueueSocket(worker_to_serve, serve_to_worker))),
        asyncio.create_task(
            client.run_connection(QueueConnection(worker_to_serve, serve_to_worker))
        ),
    ]
    await asyncio.wait_for(store.done.wait(), timeout=1)
    await asyncio.sleep(0)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    assert store.job.state is JobState.DONE
    assert coordinator.result is not None
    assert coordinator.result.session_id == "session-1"
