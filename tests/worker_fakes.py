"""Transport peers and external execution dependencies for focused protocol tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from pydantic import SecretStr

from app.communication.gateway import Gateway
from app.contracts.protocol import Register, message_header
from app.controler.config import ServerSettings
from app.controler.runtime import ServerRuntime
from app.controler.workers.callbacks import ServerCallbacks
from app.domain.models import WorkspaceInventory
from app.worker.config import WorkerSettings
from app.worker.execution.executor import WorkerExecutor
from app.worker.runtime import WebSocketTransport, WorkerRuntime


def inventory() -> list[WorkspaceInventory]:
    """Concrete locations used by transport-only peers without filesystem execution."""
    return [
        WorkspaceInventory(workspace="b" * 32, repository=None, generation=1, flow_id=None),
        WorkspaceInventory(workspace="a" * 32, repository="owner/repo", generation=1, flow_id=None),
    ]


class SimulatedWorkspaces:
    """Report concrete inventory without creating repositories on disk."""

    def __init__(self):
        self.items = inventory()

    def inventory(self):
        return list(self.items)

    async def provision(self, repository):
        assert repository == "owner/repo"


def worker_runtime(*, worker_id, worker_token, serve_ws_url, execution, read_history):
    """Use the real Runtime connection path with test-owned execution and history IO."""
    runtime = WorkerRuntime.__new__(WorkerRuntime)
    runtime.settings = WorkerSettings.model_construct(
        worker_id=worker_id,
        worker_token=SecretStr(worker_token),
        serve_ws_url=serve_ws_url,
    )
    runtime._changed = asyncio.Event()
    runtime._initialized = True
    runtime.read_history = read_history
    if isinstance(execution, Mock):
        execution.state = Mock(flow_id=3, assignment=None,
                               load=Mock(return_value=SimpleNamespace(job_id=5, acknowledged=True)))
        runtime.workspaces = SimulatedWorkspaces()
    else:
        runtime.settings.worker_root = execution.state.worker_root
        execution._on_change = runtime.notify
        if isinstance(execution.executor, WorkerExecutor):
            runtime.workspaces = execution.executor.workspaces
            runtime.workspaces.initialize()
        else:
            runtime.workspaces = SimulatedWorkspaces()
    runtime.executions = {execution.state.flow_id: execution}
    runtime._create_execution = {execution.state.flow_id: execution}.__getitem__
    runtime.inference = Mock(capability=Mock(return_value=None))
    runtime.gateway = Gateway()
    runtime._connected = False
    runtime._shutdown_requested = asyncio.Event()
    runtime._connection_task = None
    return runtime


async def run_worker(runtime, connection):
    await runtime._serve_connection(WebSocketTransport(connection))


def server_runtime(coordinator, *, database, peers, tools, results,
                   heartbeat_interval=15, heartbeat_timeout=45, poll_interval=2, metrics=None):
    """Exercise ServerRuntime.serve_worker_session without unrelated background services."""
    runtime = ServerRuntime.__new__(ServerRuntime)
    runtime.settings = ServerSettings.model_construct(
        worker_heartbeat_interval=heartbeat_interval, worker_heartbeat_timeout=heartbeat_timeout,
        worker_poll_interval=poll_interval,
    )
    runtime.database, runtime.gateway = database, peers
    runtime.coordinator, runtime.tools, runtime.results = coordinator, tools, results
    runtime.metrics = metrics
    return runtime


def server_callbacks(runtime, worker_id, epoch):
    """Construct per-connection callbacks for focused preparation/evidence checks."""
    return ServerCallbacks(
        runtime.coordinator, worker_id=worker_id,
        register=Register(**message_header(None), control="register", payload=[],
                          worker_id=worker_id, workspace="b" * 32, repo_workspace_limit=2,
                          inventory=inventory(), recoveries=[], inventory_complete=True,
                          inference=None),
        session=Mock(epoch=epoch), database=runtime.database, tools=runtime.tools,
        results=runtime.results, notify_jobs=runtime.gateway.wake_all,
    )
