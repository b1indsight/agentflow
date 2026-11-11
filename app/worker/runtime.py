"""Composition root and process-lifetime root lock for a remote worker."""

from __future__ import annotations

import asyncio
import fcntl
import os
import random
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Protocol

from loguru import logger
from websockets.asyncio.client import connect

from app.communication.gateway import Gateway
from app.communication.session import Session, Transport
from app.contracts.conversation import HistoryPage, HistoryQuery
from app.contracts.protocol import (
    ArchiveTask,
    AssignmentArguments,
    ServerMessageUnion,
    WorkerMessageUnion,
)
from app.contracts.validation import parse_server_input
from app.domain.inference_models import MAX_OUTPUT_BYTES
from app.inference.transport import InferenceProtocolError, WorkerInferenceCallbacks
from app.telemetry.logging import configure_logging
from app.worker.config import WorkerSettings
from app.worker.execution.amp_agent import AmpCodingAgent
from app.worker.execution.codex import CodexAgent
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.inference import NativeAgentInference
from app.worker.execution.lifecycle import WorkerExecution, WorkerProtocolError
from app.worker.execution.workspaces import RepositoryWorkspaces
from app.worker.health import HealthFile
from app.worker.state.amp_history import AmpHistory
from app.worker.state.execution import ExecutionState
from app.worker.state.history import CodexHistory
from app.worker.transport.business import WorkerBusinessCallbacks
from app.worker.transport.state import WorkerStateCallbacks, control_ack

_CONTROL_ENVIRONMENT_KEYS = frozenset(
    {
        "FLOWTOOL_DATABASE_URL",
        "FLOWTOOL_GITHUB_APP_ID",
        "FLOWTOOL_GITHUB_PRIVATE_KEY_PATH",
        "FLOWTOOL_GITHUB_WEBHOOK_SECRET",
        "FLOWTOOL_WORKER_TOKENS",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    }
)


class Connection(Protocol):
    async def send(self, data: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self, code: int = 1000) -> None: ...


class WebSocketTransport:
    """Map outbound WebSocket IO to the transport shared with the server."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    async def send_text(self, data: str) -> None:
        await self.connection.send(data)

    async def receive_text(self) -> str:
        raw = await self.connection.recv()
        return raw.decode() if isinstance(raw, bytes) else raw

    async def close(self, code: int = 1000) -> None:
        await self.connection.close(code)


def validate_worker_environment(
    settings: WorkerSettings, source: Mapping[str, str] | None = None
) -> None:
    """Fail closed when a full-access coding agent shares control-plane process secrets."""

    if not settings.trusted_host_execution:
        return
    values = os.environ if source is None else source
    exposed = sorted(key for key in _CONTROL_ENVIRONMENT_KEYS if values.get(key))
    if exposed:
        raise RuntimeError(
            "trusted-host CodingAgent cannot start with control-plane secrets in the worker "
            f"environment: {', '.join(exposed)}"
        )


class WorkerRootLock:
    """Prevent two worker processes from mutating the same workspace root."""

    def __init__(self, worker_root: Path, worker_id: str | None = None) -> None:
        self.worker_root = worker_root
        self.worker_id = worker_id
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.worker_root.mkdir(parents=True, exist_ok=True)
        handle = (self.worker_root / ".worker.lock").open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(f"worker root already in use: {self.worker_root}") from exc
        if self.worker_id is not None:
            identity_path = self.worker_root / ".worker-id"
            if identity_path.exists():
                recorded = identity_path.read_text().strip()
                if recorded != self.worker_id:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()
                    raise RuntimeError(
                        f"worker root belongs to {recorded}, not {self.worker_id}: "
                        f"{self.worker_root}"
                    )
            else:
                identity_path.write_text(f"{self.worker_id}\n")
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


class WorkerRuntime:
    """Compose worker dependencies and join their cleanup before releasing the process root."""

    def __init__(self, settings: WorkerSettings) -> None:
        self._shutdown_requested = asyncio.Event()
        self._connection_task: asyncio.Task[None] | None = None
        self.settings = settings
        configure_logging("worker", self.settings.log_format)
        self.root_lock = WorkerRootLock(self.settings.worker_root, self.settings.worker_id)
        self.workspaces = RepositoryWorkspaces(
            self.settings.worker_root, self.settings.worker_id,
            limit=self.settings.worker_repo_workspace_limit,
        )
        self.executions: dict[int, WorkerExecution] = {}
        self._changed = asyncio.Event()
        self._initialized = False
        self._history = CodexHistory()
        self._amp_history = AmpHistory()
        timeout = (self.settings.amp_run_timeout_seconds if self.settings.coding_agent == "amp"
                   else self.settings.codex_run_timeout_seconds)
        agent_type = AmpCodingAgent if self.settings.coding_agent == "amp" else CodexAgent
        self.inference = NativeAgentInference(
            agent_type(trusted_host=self.settings.trusted_host_execution, timeout=timeout,
                       max_output_bytes=MAX_OUTPUT_BYTES),
            root=self.settings.worker_root / "inference", backend=self.settings.coding_agent, timeout=timeout,
            enabled=self.settings.trusted_host_execution,
        )
        self.gateway = Gateway[ServerMessageUnion, WorkerMessageUnion]()
        self._connected = False

    def initialize(self) -> None:
        """Load every Flow only while the process owns its root; reconnects keep live owners."""
        if self._initialized:
            return
        self.workspaces.initialize()
        self.inference.root = self.workspaces.inference_path
        for flow_id in ExecutionState.scan(self.settings.worker_root):
            execution = self._create_execution(flow_id)
            execution.state.reconcile_acknowledgement(self.workspaces)
            if execution.state.load() is not None:
                self.executions[flow_id] = execution
        self._initialized = True

    def _create_execution(self, flow_id: int) -> WorkerExecution:
        state = ExecutionState(self.settings.worker_root, flow_id)
        agent_type = AmpCodingAgent if self.settings.coding_agent == "amp" else CodexAgent
        timeout = (self.settings.amp_run_timeout_seconds if self.settings.coding_agent == "amp"
                   else self.settings.codex_run_timeout_seconds)
        executor = WorkerExecutor(
            self.settings.worker_root,
            agent_type(trusted_host=self.settings.trusted_host_execution, timeout=timeout),
            worker_id=self.settings.worker_id, state=state, workspaces=self.workspaces,
        )
        return WorkerExecution(
            worker_id=self.settings.worker_id, executor=executor, state=state,
            on_change=self.notify,
        )

    def execution_for(self, job_id: int, token: str | None = None) -> WorkerExecution:
        """Resolve immutable job credentials, never a worker-wide current pointer."""
        for execution in self.executions.values():
            record = execution.state.load()
            if record is not None and record.job_id == job_id:
                if token is not None and record.assignment_token != token:
                    raise WorkerProtocolError("execution token does not match its journal")
                return execution
        raise WorkerProtocolError("execution journal is missing")

    def receive(self, assignment: AssignmentArguments) -> WorkerExecution:
        """Validate the supplied concrete location before recording or starting an executor."""
        execution = self.executions.get(assignment.flow_id)
        if execution is not None:
            record = execution.state.load()
            if record is not None and (record.job_id, record.assignment_token) == (
                assignment.job_id, assignment.assignment_token,
            ):
                if (record.selected_workspace, record.selected_generation, record.selected_repository) != (
                    assignment.workspace, assignment.generation, assignment.task.repo,
                ) or (execution.state.assignment is not None and execution.state.assignment != assignment):
                    raise WorkerProtocolError("repeated assignment changed its execution target")
                return execution
        slot = next((item for item in self.workspaces.inventory()
                     if item.workspace == assignment.workspace), None)
        retrying_archive = (slot is not None and slot.state == "releasing"
                            and slot.flow_id == assignment.flow_id
                            and isinstance(assignment.task, ArchiveTask))
        if (slot is None or slot.repository != assignment.task.repo
                or slot.generation != assignment.generation
                or (slot.state not in {"idle", "occupied"} and not retrying_archive)
                or slot.flow_id not in {None, assignment.flow_id}):
            raise WorkerProtocolError("assignment workspace is unavailable or has a stale generation")
        if execution is None:
            execution = self._create_execution(assignment.flow_id)
            self.executions[assignment.flow_id] = execution
        for other in self.executions.values():
            record = other.state.load()
            if (other is not execution and record is not None and not record.acknowledged
                    and record.selected_workspace == assignment.workspace):
                raise WorkerProtocolError("workspace has another unsettled execution")
        execution.receive(assignment)
        return execution

    def read_history(self, query: HistoryQuery) -> HistoryPage:
        """History belongs to a session, not to whichever Flow currently occupies a slot."""
        return (self._amp_history if query.session_id.startswith("T-") else self._history).query(query)

    def notify(self) -> None:
        self._changed.set()

    async def wait_changed(self) -> None:
        """One readiness observer covers all executions, independent of their count."""
        await self._changed.wait()
        self._changed.clear()

    async def _run_connections(self) -> None:
        """Authenticate and reconnect; join connection cleanup without stopping execution."""
        settings = self.settings
        delay = settings.reconnect_min_backoff
        try:
            while not self._shutdown_requested.is_set():
                try:
                    async with connect(
                        settings.serve_ws_url,
                        additional_headers={
                            "Authorization": f"Bearer {settings.worker_token.get_secret_value()}",
                            "X-FlowTool-Worker-ID": settings.worker_id,
                        },
                    ) as connection:
                        if self._shutdown_requested.is_set():
                            break
                        delay = settings.reconnect_min_backoff
                        self._connection_task = asyncio.create_task(
                            self._serve_connection(WebSocketTransport(connection))
                        )
                        await self._connection_task
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 — reconnect after protocol/transport loss.
                    jitter = random.uniform(
                        1 - settings.reconnect_jitter, 1 + settings.reconnect_jitter
                    )
                    wait = delay * jitter
                    logger.warning("worker connection lost; retrying in {:.2f}s: {}", wait, error)
                    try:
                        await asyncio.wait_for(self._shutdown_requested.wait(), timeout=wait)
                    except TimeoutError:
                        delay = min(delay * 2, settings.reconnect_max_backoff)
        finally:
            if self._connection_task is not None:
                self._connection_task.cancel()
                await asyncio.gather(self._connection_task, return_exceptions=True)
                self._connection_task = None

    async def _serve_connection(self, transport: Transport) -> None:
        self.initialize()
        session = Session[ServerMessageUnion, WorkerMessageUnion](
            transport,
            parse=parse_server_input,
            make_ack=control_ack,
        )
        try:
            inference_callbacks = WorkerInferenceCallbacks(
                session=session, inference=self.inference, ready=lambda: state_callbacks.ready,
            )
            state_callbacks = WorkerStateCallbacks(
                worker_id=self.settings.worker_id,
                session=session,
                runtime=self,
                on_ready=self._set_connected,
                inference=await asyncio.to_thread(self.inference.capability),
                inference_callbacks=inference_callbacks,
            )
            business_callbacks = WorkerBusinessCallbacks(
                session=session, runtime=self, ready=lambda: state_callbacks.ready,
            )
        except BaseException:
            await session.close()
            raise
        try:
            await self.gateway.serve(
                "server", session, state_callbacks, business_callbacks, inference_callbacks, replace=False,
            )
        except InferenceProtocolError as error:
            raise WorkerProtocolError(str(error)) from error
        finally:
            try:
                await state_callbacks.aclose()
            finally:
                await inference_callbacks.aclose()

    def _set_connected(self, connected: bool) -> None:
        self._connected = connected

    async def run(self) -> None:
        validate_worker_environment(self.settings)
        self.root_lock.acquire()
        try:
            self.initialize()
        except BaseException:
            self.root_lock.release()
            raise
        health_task = asyncio.create_task(self._report_health()) if self.settings.worker_status_path else None
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self.stop)
        communication = asyncio.create_task(self._run_connections())
        cancelled = False
        try:
            await asyncio.shield(communication)
        except asyncio.CancelledError:
            cancelled = True
        finally:
            self.gateway.seal()
            if not communication.done():
                communication.cancel()
            try:
                # Join communication before execution cleanup; repeated process cancellation
                # must not interrupt either join and release the root while children are alive.
                while not communication.done():
                    try:
                        await asyncio.shield(communication)
                    except asyncio.CancelledError:
                        cancelled = True
                if not communication.cancelled():
                    communication.result()
            finally:
                try:
                    try:
                        await self.gateway.close()
                    finally:
                        cleanup = asyncio.gather(
                            *(execution.shutdown() for execution in self.executions.values()),
                            return_exceptions=True,
                        )
                        while not cleanup.done():
                            try:
                                await asyncio.shield(cleanup)
                            except asyncio.CancelledError:
                                cancelled = True
                        outcomes = cleanup.result()
                        failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
                        if failures:
                            raise BaseExceptionGroup("worker execution cleanup failed", failures)
                except asyncio.CancelledError:
                    cancelled = True
                finally:
                    loop.remove_signal_handler(signal.SIGTERM)
                    try:
                        if health_task is not None:
                            health_task.cancel()
                            await asyncio.gather(health_task, return_exceptions=True)
                    finally:
                        self.root_lock.release()
        if cancelled and not self._shutdown_requested.is_set():
            raise asyncio.CancelledError

    async def _report_health(self) -> None:
        assert self.settings.worker_status_path is not None
        marker = HealthFile(self.settings.worker_status_path)
        try:
            while True:
                marker.write(self._connected)
                await asyncio.sleep(5)
        finally:
            marker.write(False)

    def stop(self) -> None:
        self._shutdown_requested.set()
        self._connected = False
        self.gateway.seal()
        if self._connection_task is not None:
            self._connection_task.cancel()
