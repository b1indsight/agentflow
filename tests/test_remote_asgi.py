from threading import Event
from unittest.mock import AsyncMock, Mock

from pydantic import SecretStr
from starlette.testclient import TestClient

from app.communication.gateway import Gateway
from app.contracts.protocol import (
    AcceptResultArguments,
    AcceptResultRequest,
    AcceptResultResponse,
    Assignment,
    AssignmentArguments,
    ExecuteResult,
    ExecutionIdentity,
    ExecutionTask,
    FlowToolErrorKind,
    FlowToolRequest,
    FlowToolResponse,
    InitialTask,
    InitialTaskResult,
    Register,
    Registered,
    Started,
    Succeeded,
    accept_result_id,
    call_header,
    execution_call_id,
    message_header,
    parse_server_message,
    result_header,
    terminal_id,
)
from app.controler.api import create_app
from app.controler.config import ServerSettings
from app.controler.runtime import ServerRuntime
from app.controler.workers.conversation import ConversationBroker, ConversationService
from app.controler.workers.inference import InferencePool
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.tools import FlowToolService
from app.domain.models import Job, JobKind, JobState
from app.persistence.store import Store
from app.telemetry.metrics import Metrics
from tests.wire import worker_message
from tests.worker_fakes import result_application


class AsgiCoordinator(JobCoordinator):
    async def prepare_for_assignment(self, job, worker, token, epoch):
        task = await self.prepare(job)
        if isinstance(task, JobState):
            return task
        return AssignmentArguments(
            job_id=job.id, flow_id=job.flow_id, assignment_token=token, task=task
        )

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

    async def _tool_response(
        self, request: FlowToolRequest, *, worker_id: str, connection_epoch: str
    ) -> FlowToolResponse:
        return FlowToolResponse(
            **result_header(request.message_id),
            method=request.method,
            call_id=request.arguments.call_id,
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
        self.settings = ServerSettings.model_construct(
            database_url=store.database_url,
            worker_tokens={"worker-1": SecretStr("worker-secret")},
            github_webhook_secret="unused",
            webhook_max_body_bytes=1024,
            metrics_token=None,
            host="test",
            port=0,
        )
        self.metrics = Metrics()
        gateway_coordinator = AsgiCoordinator(attempt_id, store)
        self.gateway = Gateway()
        self.worker_tokens = {"worker-1": "worker-secret"}
        self.coordinator = gateway_coordinator
        self.database = store.database
        self.inference_pool = InferencePool(self.gateway, database=store.database)
        self.tools = gateway_coordinator.tools
        self.results = gateway_coordinator.results
        self.settings.worker_poll_interval = 0.01
        self.conversations = ConversationService(
            ConversationBroker(self.gateway),
            database=store.database,
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
    serve = ServerRuntime.serve_worker_session

    async def handle_until_disconnected(self, worker_id, register, connection):
        await serve(self, worker_id, register, connection)
        disconnected.set()

    monkeypatch.setattr(ServerRuntime, "serve_worker_session", handle_until_disconnected)
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
        socket.send_text(
            Register(
                **message_header(None),
                control="register",
                payload=[],
                worker_id="worker-1",
                recovery=None,
                inference=None,
            ).model_dump_json()
        )
        ready = False
        assignment = None
        epoch = None
        while assignment is None:
            message = parse_server_message(socket.receive_text())
            if isinstance(message, Registered):
                ready = True
            for item in message.payload:
                assert isinstance(item, Assignment) and ready
                assignment, epoch = item, message.connection_epoch
        assert epoch is not None
        work = assignment.arguments
        assert assignment.message_id == execution_call_id(work.job_id, work.assignment_token)
        started = Started(
            **call_header(),
            method="server.execution_started",
            arguments=ExecutionIdentity(job_id=work.job_id, assignment_token=work.assignment_token),
        )
        socket.send_text(worker_message(started, epoch=epoch).model_dump_json())
        terminal = Succeeded(
            job_id=work.job_id,
            assignment_token=work.assignment_token,
            result=InitialTaskResult(session_id="session-1", last_message="done", exit_code=0),
        )
        outcome = ExecuteResult(
            **result_header(assignment.message_id, terminal_id(terminal)),
            method="worker.execute",
            job_id=work.job_id,
            assignment_token=work.assignment_token,
            result=terminal.result,
        )
        acceptance = AcceptResultRequest(
            **call_header(accept_result_id(outcome.message_id)),
            method="server.accept_result",
            arguments=AcceptResultArguments(
                job_id=work.job_id,
                assignment_token=work.assignment_token,
                result_id=outcome.message_id,
            ),
        )
        socket.send_text(worker_message(outcome, acceptance, epoch=epoch).model_dump_json())
        while True:
            message = parse_server_message(socket.receive_text())
            if message.payload:
                assert len(message.payload) == 1 and message.connection_epoch == epoch
                accepted = message.payload[0]
                assert isinstance(accepted, AcceptResultResponse)
                assert accepted.reply_to == acceptance.message_id
                assert accepted.result.state == "done"
                assert (accepted.result.job_id, accepted.result.assignment_token) == (
                    work.job_id,
                    work.assignment_token,
                )
                break

        # Wait for the peer to finish disconnecting before TestClient cancels its ASGI task.
        socket.close()
        assert disconnected.wait(timeout=2)

    completed = store.jobs.get_job(job.id)
    assert completed is not None
    assert completed.state is JobState.DONE
