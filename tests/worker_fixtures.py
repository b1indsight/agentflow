"""Real runtime setup and scripted remote peers for focused protocol tests."""

import asyncio

from pydantic import SecretStr

from app.communication.session import Session
from app.contracts.protocol import Register, message_header
from app.contracts.validation import parse_worker_input
from app.controler.workers.callbacks import ServerCallbacks
from app.controler.workers.transport import control_ack
from app.domain.models import CodingExecutionResult, WorkspaceInventory
from app.worker.config import WorkerSettings
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.runtime import WebSocketTransport, WorkerRuntime
from tests.test_communication_session import Socket


async def naming_start(task, cwd, *, backend="codex", **kwargs):
    """Script the title answer, not registration, dispatch or result handling."""
    return CodingExecutionResult(
        None, '{"title":"Implement task","branch_slug":"feat/implement-task"}', 0,
        backend=backend,
    )


def script_inference(monkeypatch, worker, start=None):
    """Script only answer-workspace calls on the shared configured agent."""
    agent = worker._agents[worker.settings.coding_agent]
    coding_start = agent.start

    async def shared_start(task, cwd, **kwargs):
        if cwd == worker.workspaces.inference_path:
            assert cwd.is_dir()
            assert kwargs["context"] is None
            if start is None:
                return await naming_start(task, cwd, backend=worker.settings.coding_agent, **kwargs)
            return await start(task, cwd, **kwargs)
        return await coding_start(task, cwd, **kwargs)

    monkeypatch.setattr(agent, "start", shared_start)


def inventory() -> list[WorkspaceInventory]:
    """Inventory reported by scripted remote peers, never by a local WorkerRuntime."""
    return [
        WorkspaceInventory(workspace="b" * 32, repository=None, generation=1, flow_id=None),
        WorkspaceInventory(workspace="a" * 32, repository="owner/repo", generation=1, flow_id=None),
    ]


async def worker_runtime(*, execution: WorkerExecution, worker_token="test", read_history=None):
    """Mount an execution paused at a fault boundary on a normally initialized runtime."""
    runtime = WorkerRuntime(WorkerSettings(
        worker_id=execution.worker_id, worker_token=SecretStr(worker_token),
        serve_ws_url="wss://unused", worker_root=execution.state.worker_root,
    ))
    runtime.initialize()
    await runtime.workspaces.provision("owner/repo")
    execution._on_change = runtime.notify
    runtime.executions[execution.state.flow_id] = execution
    if read_history is not None:
        async def history(query):
            return await asyncio.to_thread(read_history, query)

        runtime.read_history = history  # type: ignore[method-assign]
    return runtime


async def run_worker(runtime, connection):
    await runtime._serve_connection(WebSocketTransport(connection))


def server_callbacks(runtime, worker_id, epoch):
    """Construct per-connection callbacks for focused preparation/evidence checks."""
    return ServerCallbacks(
        runtime.coordinator, worker_id=worker_id,
        register=Register(**message_header(None), control="register", payload=[],
                          worker_id=worker_id, coding_agent="codex", workspace="b" * 32, repo_workspace_limit=2,
                          inventory=inventory(), recoveries=[], inventory_complete=True,
                          inference=None),
        session=Session(Socket(), parse=parse_worker_input, make_ack=control_ack, epoch=epoch),
        database=runtime.database, tools=runtime.tools,
        results=runtime.results, notify_jobs=runtime.gateway.wake_all,
    )
