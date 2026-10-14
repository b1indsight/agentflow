import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr

from app.controler.api import create_app
from app.controler.runtime import ServerRuntime
from app.controler.workers.conversation import ConversationService
from app.controler.workers.gateway import WorkerGateway
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

OTHER_SESSION = "019eac3b-a60c-7a93-a442-3718f660fc8d"

from app.controler.workers.conversation import ConversationBroker


class HistoryStore(RemoteStore):
    def get_flow(self, flow_id):
        return Flow(id=3, repo="owner/repo", issue_number=8, state=FlowState.ACTIVE) if flow_id == 3 else None

    def get_flow_execution(self, flow_id):
        return FlowExecution(flow_id=flow_id, worker_id="worker-1", repo_identity="repo")

    def list_attempts(self, flow_id):
        return [Attempt(id=7, flow_id=flow_id, codex_session_id=SESSION),
                Attempt(id=8, flow_id=flow_id, codex_session_id=OTHER_SESSION, session_generation=1)]


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

        async def _tool_response(self, request, *, worker_id):
            await block("tool")
            return await super()._tool_response(request, worker_id=worker_id)

        async def _commit_result(self, job_id):
            await block("result")
            return await super()._commit_result(job_id)

    class Executor(FakeExecutor):
        async def execute(self, task, secret=None, *, tool_caller):
            await block("worker")
            return await super().execute(task, secret, tool_caller=tool_caller)

    path = rollout(tmp_path / "codex", [message("user", "PR feedback"), message("assistant", "Fixed")])
    path.with_name(path.name.replace(SESSION, OTHER_SESSION)).write_text(
        json.dumps({"type": "session_meta", "payload": {"id": OTHER_SESSION}}) + "\n"
        + json.dumps(message("assistant", "Follow-up")) + "\n"
    )
    store = HistoryStore()
    gateway_coordinator = Coordinator(store)
    gateway = WorkerGateway(
        gateway_coordinator,
        worker_tokens={"worker-1": "worker-secret"},
        poll_interval=0.01,
        deliveries=store.deliveries,
        jobs=store.jobs,
        stops=store.stops,
        workers=store.workers,
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
        conversations=ConversationBroker(),
    )
    gateway.conversations.timeout = 1
    service = ConversationService(gateway.conversations, flows=store.flows, workers=store.workers)
    runtime = Mock(spec=ServerRuntime, settings=SimpleNamespace(control_api_token=SecretStr("reader")), conversations=service)
    app = create_app(runtime)
    app.state.runtime = runtime
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
            assert second.json()["has_more"]
            third = await http.get("/api/flows/3/conversation", params={"cursor": second.json()["next_cursor"]})
            assert third.status_code == 200, third.text
            item = third.json()["items"][0]
            assert (item["attempt_id"], item["session_id"], item["session_generation"]) == (8, OTHER_SESSION, 1)
            assert item["content"]["text"] == "Follow-up"
            assert not third.json()["has_more"]
            assert (await http.get("/api/flows/99/conversation")).status_code == 404
            assert not gateway.conversations.pending
    finally:
        release.set()
        server.cancel()
        client.cancel()
        await asyncio.gather(server, client, return_exceptions=True)
