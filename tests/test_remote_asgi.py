from types import SimpleNamespace

from starlette.testclient import TestClient

from app.control.api import create_app
from app.control.gateway import WorkerGateway
from app.models import Job, JobKind, JobState
from app.observability import Metrics
from app.protocol import (
    Assignment,
    AssignmentSecret,
    ExecutionResult,
    ExecutionTask,
    FlowToolErrorKind,
    FlowToolRequest,
    FlowToolResponse,
    InitialResult,
    InitialTask,
    Register,
    Registered,
    ResultAccepted,
    Started,
    Succeeded,
    parse_server_message,
)
from app.stop_store import StopStore
from app.store import Store


class AsgiCoordinator:
    async def prepare_for_assignment(self, job, worker, token, epoch):
        return await self.prepare(job)

    def __init__(self, attempt_id: int, store: Store) -> None:
        self.attempt_id = attempt_id
        self.store = store

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

    async def assignment_secret(self, job: Job, task: ExecutionTask) -> AssignmentSecret | None:
        return None

    async def execute_tool(self, request: FlowToolRequest, *, worker_id: str) -> FlowToolResponse:
        return FlowToolResponse(
            call_id=request.call_id,
            error="unused",
            error_kind=FlowToolErrorKind.INVALID_REQUEST,
        )

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None:
        pass

    async def apply_result(self, job: Job, result: ExecutionResult) -> JobState:
        assert isinstance(result, InitialResult)
        self.store.complete_persisted_result(job.id, JobState.DONE)
        return JobState.DONE

    async def fail(self, job: Job, reason: str) -> None:
        pass


class AsgiContext:
    def __init__(self, store: Store, attempt_id: int) -> None:
        self.settings = SimpleNamespace(github_webhook_secret="unused", webhook_max_body_bytes=1024,
                                        metrics_token=None, host="test", port=0)
        self.metrics = Metrics()
        self.gateway = WorkerGateway(
            store,
            AsgiCoordinator(attempt_id, store), stops=StopStore(store),
            worker_tokens={"worker-1": "worker-secret"},
            poll_interval=0.01,
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
    from threading import Event

    disconnected = Event()
    handle = WorkerGateway.handle

    async def handle_until_disconnected(self, socket):
        await handle(self, socket)
        disconnected.set()

    monkeypatch.setattr(WorkerGateway, "handle", handle_until_disconnected)
    flow = store.create_flow("owner/repo", 8, installation_id=42)
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    job = store.enqueue_job(flow.id, JobKind.INITIAL)
    application = create_app(lambda: AsgiContext(store, attempt.id))  # type: ignore[arg-type]

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
                result=InitialResult(session_id="session-1", last_message="done", exit_code=0),
            ).model_dump_json()
        )
        accepted = parse_server_message(socket.receive_text())
        assert isinstance(accepted, ResultAccepted)
        assert accepted.state is JobState.DONE

        # Wait for the peer to finish disconnecting before TestClient cancels its ASGI task.
        socket.close()
        assert disconnected.wait(timeout=2)

    completed = store.get_job(job.id)
    assert completed is not None
    assert completed.state is JobState.DONE
