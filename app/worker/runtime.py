"""Composition root and process-lifetime root lock for a remote worker."""

from __future__ import annotations

import asyncio
import fcntl
import os
import random
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import IO, Protocol

from loguru import logger
from pydantic import JsonValue
from websockets.asyncio.client import connect

from app.communication.dispatch import SessionCallbacks
from app.communication.gateway import Gateway
from app.communication.requests import ReplyRejected, request_reply
from app.communication.session import Session, SessionClosed, Transport
from app.contracts.conversation import ConversationFailure, HistoryPage, HistoryQuery
from app.contracts.files import FileFailure
from app.contracts.protocol import (
    AssignmentArguments,
    ExecutionIdentity,
    FileRegister,
    FileRegistered,
    FlowSearchCall,
    FlowSearchPage,
    FlowSearchQuery,
    FlowSearchResult,
    InitialTask,
    ServerMessageUnion,
    WorkerMessage,
    WorkerMessageUnion,
    call_header,
    message_header,
)
from app.contracts.validation import parse_server_input
from app.domain.inference_models import (
    InferenceCapability,
    InferenceError,
    InferenceFailure,
    InferenceOutput,
    InferenceTask,
)
from app.inference.transport import InferenceProtocolError, WorkerInferenceCallbacks
from app.worker.coding_agent import CodingAgent
from app.worker.config import WorkerSettings
from app.worker.execution.amp_agent import AmpCodingAgent
from app.worker.execution.codex import CodexAgent
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.lifecycle import WorkerExecution, WorkerProtocolError
from app.worker.execution.permissions import FULL_ACCESS
from app.worker.execution.workspaces import RepositoryWorkspaces
from app.worker.health import HealthFile
from app.worker.state.execution import ExecutionState
from app.worker.state.file_links import FileLinks
from app.worker.state.history_links import link_history
from app.worker.transport.business import WorkerBusinessCallbacks
from app.worker.transport.files import WorkerFileCallbacks
from app.worker.transport.state import WorkerStateCallbacks, control_ack

HISTORY_TIMEOUT = 25.0  # Leave time for the reply within the controller's 30-second budget.

_CONTROL_ENVIRONMENT_KEYS = frozenset(
    {
        "AGENTFLOW_DATABASE_URL",
        "AGENTFLOW_GITHUB_APP_ID",
        "AGENTFLOW_GITHUB_PRIVATE_KEY_PATH",
        "AGENTFLOW_GITHUB_WEBHOOK_SECRET",
        "AGENTFLOW_WORKER_TOKENS",
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
    settings: WorkerSettings | InferenceOnly, source: Mapping[str, str] | None = None
) -> None:
    """Fail closed when a full-access coding agent shares control-plane process secrets."""

    trusted_host = settings.agent.trusted_host if isinstance(settings, InferenceOnly) else settings.trusted_host_execution
    if not trusted_host:
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


@dataclass(frozen=True, kw_only=True)
class InferenceOnly:
    """Run a host-supplied agent on one connection, without coding routes or repository state."""

    worker_id: str
    transport: Transport
    agent: CodingAgent
    workspace: Path

    def __post_init__(self) -> None:
        if self.agent.trusted_host is False:
            raise ValueError("this inference backend requires trusted-host execution")
        validate_worker_environment(self)


class WorkerRuntime:
    """Use WorkerSettings for a coding worker or InferenceOnly for a host-supplied agent."""

    def __init__(self, settings: WorkerSettings | InferenceOnly) -> None:
        self._shutdown_requested = asyncio.Event()
        self._connection_task: asyncio.Task[None] | None = None
        self._mode = settings
        self.executions: dict[int, WorkerExecution] = {}
        self._changed = asyncio.Event()
        self._initialized = False
        self.gateway = Gateway[ServerMessageUnion, WorkerMessageUnion]()
        self._connected = False
        self._file_registrations = 0

    @property
    def inference_only(self) -> bool:
        return isinstance(self._mode, InferenceOnly)

    @property
    def settings(self) -> WorkerSettings:
        if isinstance(self._mode, InferenceOnly):
            raise WorkerProtocolError("coding is disabled for this worker")
        return self._mode

    @cached_property
    def root_lock(self) -> WorkerRootLock:
        return WorkerRootLock(self.settings.worker_root, self.settings.worker_id)

    @cached_property
    def workspaces(self) -> RepositoryWorkspaces:
        return RepositoryWorkspaces(
            self.settings.worker_root, self.settings.worker_id,
            limit=self.settings.worker_repo_workspace_limit,
        )

    @cached_property
    def files(self) -> FileLinks:
        return FileLinks(self.settings.worker_root)

    @cached_property
    def _agents(self) -> dict[str, CodingAgent]:
        # Coding, inference, history and files share instances; per-run state stays in each call.
        return {
            "codex": CodexAgent(trusted_host=self.settings.trusted_host_execution,
                                permissions=FULL_ACCESS,
                                timeout=self.settings.codex_run_timeout_seconds),
            "amp": AmpCodingAgent(trusted_host=self.settings.trusted_host_execution,
                                  permissions=FULL_ACCESS,
                                  history_root=self.settings.worker_root / "amp-history",
                                  timeout=self.settings.amp_run_timeout_seconds),
        }

    def initialize(self) -> None:
        """Load every Flow only while the process owns its root; reconnects keep live owners."""
        if self.inference_only or self._initialized:
            return
        self.workspaces.initialize()
        for flow_id in ExecutionState.scan(self.settings.worker_root):
            execution = self._create_execution(flow_id)
            execution.state.reconcile_acknowledgement()
            if execution.state.load() is not None:
                self.executions[flow_id] = execution
        self._initialized = True

    @property
    def coding_agent(self) -> CodingAgent:
        if isinstance(self._mode, InferenceOnly):
            return self._mode.agent
        return self._agents[self.settings.coding_agent]

    def _create_execution(self, flow_id: int) -> WorkerExecution:
        state = ExecutionState(self.settings.worker_root, flow_id)
        executor = WorkerExecutor(
            self.settings.worker_root,
            self.coding_agent,
            worker_id=self.settings.worker_id, state=state, workspaces=self.workspaces,
        )
        return WorkerExecution(
            worker_id=self.settings.worker_id, executor=executor, state=state,
            on_change=self.notify, search=self._search_flows,
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
        superseding = (slot is not None and assignment.generation > slot.generation
                       and isinstance(assignment.task, InitialTask))
        if (slot is None or slot.repository != assignment.task.repo
                or (not superseding and (slot.generation != assignment.generation
                                        or slot.flow_id not in {None, assignment.flow_id}))):
            raise WorkerProtocolError("assignment workspace is unavailable or has a stale generation")
        for other in self.executions.values():
            record = other.state.load()
            if (other is not execution and record is not None
                    and record.selected_workspace == assignment.workspace
                    and other.may_modify_workspace()):
                raise WorkerProtocolError("workspace has another live or unknown execution")
        if execution is None:
            execution = self._create_execution(assignment.flow_id)
        execution.receive(assignment)
        self.executions[assignment.flow_id] = execution
        return execution

    async def read_history(self, query: HistoryQuery) -> HistoryPage:
        """Page native history first, then register destinations only in its outbound copy."""
        deadline = asyncio.get_running_loop().time() + HISTORY_TIMEOUT
        backend = "amp" if query.session_id.startswith("T-") else "codex"
        source = self._agents[backend]
        try:
            async with asyncio.timeout_at(deadline):
                page = await asyncio.to_thread(source.read_history, query)
        except TimeoutError:
            raise ConversationFailure("query_timeout") from None
        return await link_history(page, self.register_file, backend=backend, deadline=deadline)

    def inference_capability(self) -> InferenceCapability | None:
        """Describe the authorized backend; actual execution checks native prerequisites."""
        if not self.inference_only and not self.settings.trusted_host_execution:
            return None
        return InferenceCapability(adapter_version=self.coding_agent.adapter_version)

    async def infer(self, task: InferenceTask) -> InferenceOutput:
        """Adapt the external inference contract to the shared agent's normal start API."""
        adapter_version = self.coding_agent.adapter_version
        if not self.inference_only and not self.settings.trusted_host_execution:
            raise InferenceError(InferenceFailure(code="configuration"), backend=adapter_version)
        cwd = self._mode.workspace if isinstance(self._mode, InferenceOnly) else self.workspaces.inference_path
        result = await self.coding_agent.start(
            task.messages, cwd, output_schema=task.output_schema,
            context=None, max_output_bytes=task.max_output_bytes, timeout_seconds=task.timeout_seconds,
        )
        if result.exit_code != 0 or result.failure_kind is not None:
            raise InferenceError(InferenceFailure(
                code="execution_timeout" if result.failure_kind == "timeout"
                else result.failure_kind or "backend_error",
                retry_after_seconds=result.retry_after_seconds,
            ), backend=adapter_version)
        return InferenceOutput(
            request_id=task.request_id, request_digest=task.digest, text=result.last_message,
            adapter_version=adapter_version, model=result.model,
        )

    async def resolve_file(self, file_id: str) -> tuple[CodingAgent, dict[str, JsonValue]]:
        """Recover the registering backend independently of active Flows and coding settings."""
        backend, source = await asyncio.to_thread(self.files.lookup, file_id)
        if backend is None:
            # Pre-ownership records used only local identities and Amp attachment URLs.
            backend = "amp" if "url" in source else self.settings.coding_agent
        agent = self._agents.get(backend)
        if agent is None:
            raise FileFailure("file_unreadable")
        return agent, source

    async def _search_flows(
        self, identity: ExecutionIdentity, query: FlowSearchQuery,
    ) -> FlowSearchPage:
        """Bind the read to one registered session, just like file registration; never replay it."""
        registration = self.gateway.get("server")
        if not self._connected or registration is None:
            raise WorkerProtocolError("Flow search unavailable")
        call = FlowSearchCall(
            **call_header(), method="server.search_flows", arguments=identity, query=query,
        )
        message = WorkerMessage(**message_header(registration.session.epoch), control=None, payload=[call])
        try:
            response, _ = await request_reply(
                registration, call, FlowSearchResult, message, timeout=10.0,
            )
        except TimeoutError:
            raise WorkerProtocolError("Flow search timed out") from None
        except SessionClosed:
            raise WorkerProtocolError("Flow search unavailable") from None
        except ReplyRejected:
            raise WorkerProtocolError("Invalid Flow search response") from None
        if response.error is not None:
            raise WorkerProtocolError(response.error)
        assert response.page is not None
        return response.page

    async def register_file(self, source: Path | str, backend: str | None = None) -> str:
        """Return an owner-only URL for local bytes or a private Amp attachment (at most 512 KiB).

        Call on this runtime's loop, outside Session callbacks, after registration.
        A timed-out call may be retried: both local and server mappings are idempotent.
        """
        registration = self.gateway.get("server")
        if not self._connected or registration is None:
            raise FileFailure("worker_unavailable")
        if self._file_registrations >= 4:
            raise FileFailure("query_busy")
        self._file_registrations += 1
        try:
            registration.session.check_request()
            async with asyncio.timeout(10):
                agent = self._agents[backend or self.settings.coding_agent]
                file_id = await asyncio.to_thread(agent.register_file, source, self.files)
                call = FileRegister(**call_header(), method="server.file_register", file_id=file_id)
                response, _ = await request_reply(
                    registration, call, FileRegistered,
                    WorkerMessage(**message_header(registration.session.epoch), control=None, payload=[call]),
                    timeout=10.0,
                )
            if response.error is not None:
                raise FileFailure(response.error)
            assert response.link is not None
            return response.link
        except TimeoutError:
            raise FileFailure("query_timeout") from None
        except SessionClosed:
            raise FileFailure("worker_unavailable") from None
        except ReplyRejected:
            raise FileFailure("invalid_response") from None
        finally:
            self._file_registrations -= 1

    def notify(self) -> None:
        self._changed.set()

    async def wait_changed(self) -> None:
        """One readiness observer covers all executions, independent of their count."""
        await self._changed.wait()
        self._changed.clear()

    async def _run_connections(self) -> None:
        """Serve the supplied connection once, or authenticate and reconnect over WebSocket."""
        if isinstance(self._mode, InferenceOnly):
            self._connection_task = asyncio.current_task()
            try:
                await self._serve_connection(self._mode.transport)
            except SessionClosed:
                pass  # The host owns reconnection for a supplied transport.
            finally:
                self._connection_task = None
            return
        settings = self.settings
        delay = settings.reconnect_min_backoff
        try:
            while not self._shutdown_requested.is_set():
                try:
                    async with connect(
                        settings.serve_ws_url,
                        max_size=16 * 1024 * 1024,
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
        async with AsyncExitStack() as cleanup:
            try:
                inference = WorkerInferenceCallbacks(
                    session=session, inference=self, ready=lambda: state.ready,
                )
                state = WorkerStateCallbacks(
                    worker_id=self._mode.worker_id, session=session, runtime=self,
                    on_ready=self._set_connected, inference=self.inference_capability(),
                    inference_callbacks=inference,
                )
                callbacks: list[SessionCallbacks[ServerMessageUnion]] = [state, inference]
                if not self.inference_only:
                    business = WorkerBusinessCallbacks(
                        session=session, runtime=self, ready=lambda: state.ready,
                    )
                    files = WorkerFileCallbacks(
                        session=session, resolve_file=self.resolve_file, root=self.settings.worker_root,
                    )
                    callbacks.extend((business, files))
                    cleanup.push_async_callback(business.aclose)
                    cleanup.push_async_callback(files.aclose)
                cleanup.push_async_callback(inference.aclose)
                cleanup.push_async_callback(state.aclose)
            except BaseException:
                await session.close()
                raise
            try:
                await self.gateway.serve("server", session, *callbacks, replace=False)
            except InferenceProtocolError as error:
                raise WorkerProtocolError(str(error)) from error

    def _set_connected(self, connected: bool) -> None:
        self._connected = connected

    async def run(self) -> None:
        """Serve on the caller's event loop until stopped or cancelled, then join cleanup."""
        health_task = None
        if not self.inference_only:
            validate_worker_environment(self.settings)
            self.root_lock.acquire()
            try:
                self.initialize()
            except BaseException:
                self.root_lock.release()
                raise
            if self.settings.worker_status_path:
                health_task = asyncio.create_task(self._report_health())
        communication = asyncio.create_task(self._run_connections())
        cancelled = False
        try:
            await asyncio.shield(communication)
        except asyncio.CancelledError:
            cancelled = True
        finally:
            self.gateway.seal()
            if not communication.done() and not communication.cancelling():
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
                    try:
                        if health_task is not None:
                            health_task.cancel()
                            await asyncio.gather(health_task, return_exceptions=True)
                    finally:
                        if not self.inference_only:
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
        """Request shutdown from the worker's event loop; await run() to finish cleanup."""
        self._shutdown_requested.set()
        self._connected = False
        self.gateway.seal()
        if self._connection_task is not None and not self._connection_task.cancelling():
            self._connection_task.cancel()
