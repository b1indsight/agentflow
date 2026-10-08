import asyncio
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.control.api import create_app
from app.control.conversation import ConversationService
from app.control.gateway import WorkerGateway
from app.domain.models import Attempt, Flow, FlowExecution, FlowState
from app.worker.state.session import WorkerSession
from app.worker.transport.client import WorkerClient
from tests.test_conversation_history import SESSION, message, rollout
from tests.test_remote_e2e import (
    FakeExecutor,
    QueueConnection,
    QueueSocket,
    RemoteCoordinator,
    RemoteStore,
)
from tests.worker_fakes import NoPendingStops


class HistoryStore(RemoteStore):
    def get_flow(self, flow_id):
        return Flow(id=3, repo="owner/repo", issue_number=8, state=FlowState.ACTIVE) if flow_id == 3 else None

    def get_flow_execution(self, flow_id):
        return FlowExecution(flow_id=flow_id, worker_id="worker-1", repo_identity="repo")

    def list_attempts(self, flow_id):
        return [Attempt(id=7, flow_id=flow_id, codex_session_id=SESSION)]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["worker", "preparation", "tool", "result"])
async def test_http_query_while_worker_or_control_busy_and_idle(tmp_path, phase, monkeypatch) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def block(selected):
        if phase == selected:
            started.set()
            await release.wait()

    class Coordinator(RemoteCoordinator):
        async def prepare_for_assignment(self, job, worker, token, epoch):
            await block("preparation")
            return await super().prepare_for_assignment(job, worker, token, epoch)

        async def execute_tool(self, request, *, worker_id):
            await block("tool")
            return await super().execute_tool(request, worker_id=worker_id)

        async def apply_result(self, job_id):
            await block("result")
            return await super().apply_result(job_id)

    class Executor(FakeExecutor):
        async def execute(self, task, secret=None, *, tool_caller):
            await block("worker")
            return await super().execute(task, secret, tool_caller=tool_caller)

    rollout(tmp_path / "codex", [message("user", "PR feedback"), message("assistant", "Fixed")])
    store = HistoryStore()
    gateway = WorkerGateway(store, Coordinator(store), execution_control=NoPendingStops(),
                            worker_tokens={"worker-1": "worker-secret"}, poll_interval=0.01)
    gateway.conversations.timeout = 1
    service = ConversationService(store, gateway.conversations)
    context = SimpleNamespace(settings=SimpleNamespace(control_api_token=SecretStr("reader")), conversations=service)
    app = create_app(lambda: context)
    app.state.ctx = context
    to_worker: asyncio.Queue[str] = asyncio.Queue()
    to_control: asyncio.Queue[str] = asyncio.Queue()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    worker = WorkerClient(worker_id="worker-1", worker_token="worker-secret", serve_ws_url="unused",
                          executor=Executor(), session=WorkerSession(tmp_path / "worker"))
    server = asyncio.create_task(gateway.handle(QueueSocket(to_control, to_worker)))
    client = asyncio.create_task(worker.run_connection(QueueConnection(to_control, to_worker)))
    try:
        await asyncio.wait_for(started.wait(), 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                    headers={"Authorization": "Bearer reader"}) as http:
            # GitHub I/O can hold preparation/tools/result application longer than the
            # query timeout. The query must complete before that operation is released.
            first = await http.get("/api/flows/3/conversation?limit=1")
            assert first.status_code == 200, first.text
            assert first.json()["items"][0]["role"] == "user"
            release.set()
            await asyncio.wait_for(store.done.wait(), 2)
            second = await http.get("/api/flows/3/conversation", params={"cursor": first.json()["next_cursor"]})
            assert second.status_code == 200, second.text
            assert second.json()["items"][0]["content"]["text"] == "Fixed"
            assert (await http.get("/api/flows/99/conversation")).status_code == 404
            assert not gateway.conversations.pending
    finally:
        release.set()
        server.cancel()
        client.cancel()
        await asyncio.gather(server, client, return_exceptions=True)
