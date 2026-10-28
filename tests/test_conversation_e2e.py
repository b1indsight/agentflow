import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest

from app.communication.gateway import Gateway
from app.contracts.protocol import ServerMessage, WorkerMessage
from app.controler.api import create_app
from app.controler.runtime import ServerRuntime
from app.controler.workers.conversation import ConversationService
from app.controler.workers.handler import WorkerHandler
from app.controler.workers.transport import serve_worker
from app.domain.models import Attempt, Flow, FlowExecution, FlowState
from app.worker.execution.amp_process import AmpProcessResult
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.state.execution import ExecutionState
from app.worker.transport.client import WorkerClient
from tests.access import AccessIssuer
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
    def __init__(self, session=SESSION, other_session=OTHER_SESSION):
        super().__init__()
        self.session, self.other_session = session, other_session

    def get_flow(self, flow_id):
        return Flow(id=3, repo="owner/repo", issue_number=8, state=FlowState.ACTIVE) if flow_id == 3 else None

    def get_flow_execution(self, flow_id):
        return FlowExecution(flow_id=flow_id, worker_id="worker-1", repo_identity="repo")

    def list_attempts(self, flow_id):
        return [Attempt(id=7, flow_id=flow_id, codex_session_id=self.session),
                Attempt(id=8, flow_id=flow_id, codex_session_id=self.other_session, session_generation=1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,phase", [
    (backend, phase) for backend in ("codex", "amp")
    for phase in ("worker", "preparation", "tool", "result")
] + [("amp", "slow_export")])
async def test_http_query_while_worker_or_control_busy_and_idle(tmp_path, phase, monkeypatch, backend) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    blocked_phase = "worker" if phase == "slow_export" else phase

    async def block(selected):
        if blocked_phase == selected:
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
    session = ("T-" if backend == "amp" else "") + SESSION
    other_session = ("T-" if backend == "amp" else "") + OTHER_SESSION
    if backend == "amp":
        async def export(args, **kwargs):
            if phase == "slow_export" and args[2] == session:
                # Scale time 1:10: a valid 15s export must outlast the old 10s broker budget.
                await asyncio.sleep(1.5)
            entries = [("user", "PR feedback"), ("assistant", "Fixed")] if args[2] == session else [("assistant", "Follow-up")]
            messages = [{"role": role, "protocolMessageID": f"M-{index}",
                         "content": [{"type": "text", "text": text}]}
                        for index, (role, text) in enumerate(entries)]
            return AmpProcessResult(json.dumps({"id": args[2], "messages": messages}).encode(), b"", 0)

        monkeypatch.setattr("app.worker.state.amp_history.run_amp", export)
    store = HistoryStore(session, other_session)
    gateway_coordinator = Coordinator(store)
    peers = Gateway[WorkerMessage, ServerMessage]()
    broker = ConversationBroker(peers)
    handler = WorkerHandler(
        gateway_coordinator, database=store.database,
        peers=peers,
        poll_interval=0.01,
        tools=gateway_coordinator.tools,
        results=gateway_coordinator.results,
    )
    if phase == "slow_export":
        broker.timeout /= 10
    else:
        broker.timeout = 1
    service = ConversationService(broker, database=store.database, )
    issuer = AccessIssuer()
    runtime = Mock(spec=ServerRuntime, settings=issuer.settings, access=issuer.auth, conversations=service)
    app = create_app(runtime)
    app.state.runtime = runtime
    to_worker: asyncio.Queue[str] = asyncio.Queue()
    to_control: asyncio.Queue[str] = asyncio.Queue()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    state = ExecutionState(tmp_path / "worker")
    worker = WorkerClient(worker_id="worker-1", worker_token="worker-secret", serve_ws_url="unused",
                          execution=WorkerExecution(worker_id="worker-1", executor=Executor(), state=state),
                          read_history=state.read)
    server = asyncio.create_task(serve_worker(QueueSocket(to_control, to_worker),
                                             worker_tokens={"worker-1": "worker-secret"}, handler=handler))
    client = asyncio.create_task(worker.run_connection(QueueConnection(to_control, to_worker)))
    try:
        await asyncio.wait_for(started.wait(), 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                    headers=issuer.headers()) as http:
            # GitHub I/O can hold preparation/tools/result application longer than the
            # query timeout. The query must complete before that operation is released.
            first = await http.get("/api/flows/3/conversation?limit=1")
            assert first.status_code == 200, first.text
            assert first.json()["items"][0]["role"] == "user"
            assert first.json()["sessions"][0]["starts_at_beginning"]
            assert not first.json()["sessions"][0]["reached_end"]
            release.set()
            await asyncio.wait_for(store.done.wait(), 2)
            second = await http.get("/api/flows/3/conversation", params={"cursor": first.json()["next_cursor"]})
            assert second.status_code == 200, second.text
            assert second.json()["items"][0]["content"]["text"] == "Fixed"
            assert second.json()["has_more"]
            # Flow continuation points to another session; this session is complete.
            assert not second.json()["sessions"][0]["starts_at_beginning"]
            assert second.json()["sessions"][0]["reached_end"]
            third = await http.get("/api/flows/3/conversation", params={"cursor": second.json()["next_cursor"]})
            assert third.status_code == 200, third.text
            item = third.json()["items"][0]
            assert (item["attempt_id"], item["session_id"], item["session_generation"]) == (8, other_session, 1)
            assert item["content"]["text"] == "Follow-up"
            assert not third.json()["has_more"]
            assert third.json()["sessions"][0]["starts_at_beginning"]
            assert third.json()["sessions"][0]["reached_end"]
            assert (await http.get("/api/flows/99/conversation")).status_code == 404
            assert broker._inflight == 0
    finally:
        release.set()
        server.cancel()
        client.cancel()
        await asyncio.gather(server, client, return_exceptions=True)
        await issuer.auth.close()
