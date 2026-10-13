from threading import Event
from unittest.mock import AsyncMock, Mock

from pydantic import SecretStr
from starlette.testclient import TestClient

from app.contracts.protocol import (
    Assignment,
    ExecutionTask,
    FlowToolErrorKind,
    FlowToolRequest,
    FlowToolResponse,
    InitialTask,
    InitialTaskResult,
    Register,
    Registered,
    ResultAccepted,
    Started,
    Succeeded,
    parse_server_message,
)
from app.controler.api import create_app
from app.controler.config import ServerSettings
from app.controler.runtime import ServerRuntime
from app.controler.workers.conversation import ConversationService
from app.controler.workers.gateway import WorkerGateway
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.tools import FlowToolService
from app.domain.models import Job, JobKind, JobState
from app.persistence.store import Store
from app.telemetry.metrics import Metrics
from tests.worker_fakes import result_application


class AsgiCoordinator(JobCoordinator):
    async def prepare_for_assignment(self, job, worker, token, epoch):
        task = await self.prepare(job)
        if isinstance(task, JobState):
            return task
        return Assignment(job_id=job.id, flow_id=job.flow_id, assignment_token=token,
                          connection_epoch=epoch, task=task)

    def __init__(self, attempt_id: int, store: Store) -> None:
        self.attempt_id = attempt_id
        self.jobs = store.jobs
        self.tools = Mock(spec=FlowToolService, execute=AsyncMock(side_effect=self._tool_response))
        self.results = result_application(store, self._commit_result)

    async def prepare(self, job: Job) -> ExecutionTask | JobState:
        return InitialTask(
            flow_id=job.flow_id,
            attempt_id=self.attempt_id,
            repo="owner/repo",
            issue_number=8,
            issue_title="Issue",
            issue_body="Body",
            branch="flow/issue-8",
        )

    async def _tool_response(self, request: FlowToolRequest, *, worker_id: str) -> FlowToolResponse:
        return FlowToolResponse(
            call_id=request.call_id,
            error="unused",
            error_kind=FlowToolErrorKind.INVALID_REQUEST,
        )

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None:
        pass

    async def _commit_result(self, job_id: int) -> JobState:
        self.jobs.complete_persisted_result(job_id, JobState.DONE)
        return JobState.DONE

    async def fail(self, job: Job, reason: str) -> None:
        pass


class AsgiRuntime(ServerRuntime):
    def __init__(self, store: Store, attempt_id: int) -> None:
        self.settings = ServerSettings.model_construct(database_url=store.database_url,
                                      worker_tokens={"worker-1": SecretStr("worker-secret")},
                                      github_webhook_secret="unused", webhook_max_body_bytes=1024,
                                      metrics_token=None, host="test", port=0)
        self.metrics = Metrics()
        self.gateway = WorkerGateway(
            AsgiCoordinator(attempt_id, store),
            worker_tokens={"worker-1": "worker-secret"},
            poll_interval=0.01,
            deliveries=store.deliveries,
            jobs=store.jobs,
            stops=store.stops,
            workers=store.workers,
        )
        self.conversations = ConversationService(
            self.gateway.conversations, flows=store.flows, workers=store.workers
        )

    async def ready(self) -> bool:
        return True

    def startup(self) -> None:
        pass

    async def receive_webhook(self, event_type, delivery_id, payload, digest) -> None:
        raise AssertionError("webhook receipt is not used in this test")

    async def shutdown(self) -> None:
        pass


def test_real_asgi_websocket_completes_a_postgresql_job(store: Store, monkeypatch) -> None:
    disconnected = Event()
    handle = WorkerGateway.handle

    async def handle_until_disconnected(self, socket):
        await handle(self, socket)
        disconnected.set()

    monkeypatch.setattr(WorkerGateway, "handle", handle_until_disconnected)
    flow = store.flows.create_flow("owner/repo", 8, installation_id=42)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    application = create_app(AsgiRuntime(store, attempt.id))

    with (
        TestClient(application) as client,
        client.websocket_connect(
            "/internal/workers/connect",
            headers={
                "Authorization": "Bearer worker-secret",
                "X-FlowTool-Worker-ID": "worker-1",
            },
        ) as socket,
    ):
        socket.send_text(Register(worker_id="worker-1").model_dump_json())
        assert isinstance(parse_server_message(socket.receive_text()), Registered)
        assignment = parse_server_message(socket.receive_text())
        assert isinstance(assignment, Assignment)

        socket.send_text(
            Started(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch,
            ).model_dump_json()
        )
        socket.send_text(
            Succeeded(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch,
                result=InitialTaskResult(session_id="session-1", last_message="done", exit_code=0),
            ).model_dump_json()
        )
        accepted = parse_server_message(socket.receive_text())
        assert isinstance(accepted, ResultAccepted)
        assert accepted.state is JobState.DONE

        # Wait for the peer to finish disconnecting before TestClient cancels its ASGI task.
        socket.close()
        assert disconnected.wait(timeout=2)

    completed = store.jobs.get_job(job.id)
    assert completed is not None
    assert completed.state is JobState.DONE
