from threading import Event

from pydantic import SecretStr
from starlette.testclient import TestClient

from app.communication.gateway import Gateway
from app.contracts.protocol import (
    AcceptResultArguments,
    AcceptResultRequest,
    AcceptResultResponse,
    Assignment,
    ExecuteResult,
    ExecutionIdentity,
    FlowToolArguments,
    FlowToolRequest,
    FlowToolResponse,
    InitialTask,
    InitialTaskResult,
    Inventory,
    Register,
    Registered,
    RepositoryDemand,
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
from app.domain.models import DeliveryState, FlowState, JobState, VcsKind
from app.persistence.records import required
from app.persistence.store import Store
from app.telemetry.metrics import Metrics
from tests.github import RecordingGitHub
from tests.test_coordinator import CoordinatorFixture, coordinator
from tests.test_worker_handler import queued_initial as queued_initial  # noqa: PLC0414
from tests.wire import worker_message
from tests.worker_fakes import inventory


class AsgiRuntime(ServerRuntime):
    def __init__(self, store: Store, subject: CoordinatorFixture) -> None:
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
        self.gateway = Gateway()
        self.worker_tokens = {"worker-1": "worker-secret"}
        self.coordinator = subject
        self.database = store.database
        self.tools = subject.tools
        self.results = subject.results
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


def test_real_asgi_websocket_completes_a_postgresql_job(
    store: Store, queued_initial, recording_resources, monkeypatch,
) -> None:
    disconnected = Event()
    serve = ServerRuntime.serve_worker_session

    async def handle_until_disconnected(self, worker_id, register, connection):
        await serve(self, worker_id, register, connection)
        disconnected.set()

    monkeypatch.setattr(ServerRuntime, "serve_worker_session", handle_until_disconnected)
    flow, job = queued_initial
    github = RecordingGitHub()
    application = create_app(AsgiRuntime(store, coordinator(store, github)))

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
                workspace="b" * 32,
                repo_workspace_limit=2,
                inventory=inventory(),
                recoveries=[],
                inventory_complete=True,
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
                if isinstance(item, RepositoryDemand):
                    assert ready and item.repository == "owner/repo"
                    socket.send_text(Inventory(
                        **message_header(message.connection_epoch), control="inventory", payload=[],
                        inventory=inventory(), recoveries=[], inventory_complete=True,
                    ).model_dump_json())
                    continue
                assert isinstance(item, Assignment) and ready
                assignment, epoch = item, message.connection_epoch
        assert epoch is not None
        work = assignment.arguments
        assert isinstance(work.task, InitialTask)
        assert assignment.message_id == execution_call_id(work.job_id, work.assignment_token)
        started = Started(
            **call_header(),
            method="server.execution_started",
            arguments=ExecutionIdentity(job_id=work.job_id, assignment_token=work.assignment_token),
        )
        socket.send_text(worker_message(started, epoch=epoch).model_dump_json())
        tool = FlowToolRequest(
            **call_header(), method="flow.open_pr",
            arguments=FlowToolArguments(
                job_id=work.job_id, flow_id=flow.id, attempt_id=work.task.attempt_id,
                assignment_token=work.assignment_token, call_id="asgi-open",
                arguments={"title": "Title", "body": "Body"},
                vcs_backend=VcsKind.GIT, branch=work.task.branch,
                commit_sha="abc123", remote_sha="abc123",
            ),
        )
        socket.send_text(worker_message(tool, epoch=epoch).model_dump_json())
        while True:
            message = parse_server_message(socket.receive_text())
            if message.payload:
                receipt, = message.payload
                assert isinstance(receipt, FlowToolResponse) and receipt.reply_to == tool.message_id
                assert receipt.result is not None
                break
        terminal = Succeeded(
            job_id=work.job_id,
            assignment_token=work.assignment_token,
            result=InitialTaskResult(
                session_id="session-1", last_message="done", exit_code=0,
                vcs_backend=VcsKind.GIT, branch=work.task.branch,
                commit_sha="abc123", remote_sha="abc123", pushed=True,
                open_pr_call_id=tool.arguments.call_id, delivery_receipt=receipt.result,
            ),
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
    assert required(store.flows.get_flow(flow.id)).state is FlowState.REVIEW
    assert required(store.deliveries.get_delivery(flow.id)).state is DeliveryState.OPEN
    assert github.opened == [flow.initial_branch]
