"""Outbound WebSocket client with durable single-assignment recovery."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from loguru import logger
from websockets.asyncio.client import connect

from app.delivery_models import DeliveryReceipt
from app.models import VcsOutcome
from app.protocol import (
    Assignment,
    AssignmentSecret,
    ExecutionResult,
    ExecutionStopped,
    ExecutionTask,
    Failed,
    FlowToolErrorKind,
    FlowToolRequest,
    FlowToolResponse,
    Heartbeat,
    ProtocolError,
    RecoveryDescriptor,
    RecoveryRejected,
    Register,
    Registered,
    ReplayResult,
    ResultAccepted,
    ResumeAssignment,
    ServerMessage,
    Started,
    StopAccepted,
    StopExecution,
    Succeeded,
    parse_server_message,
)
from app.worker.journal import WorkerJournal
from app.worker.tools import FlowToolCaller


class Connection(Protocol):
    async def send(self, data: str) -> None: ...

    async def recv(self) -> str | bytes: ...


class TaskExecutor(Protocol):
    async def execute(
        self, task: ExecutionTask, secret: AssignmentSecret | None = None, *, tool_caller: FlowToolCaller
    ) -> ExecutionResult: ...


class WorkerProtocolError(RuntimeError):
    pass


class FlowToolCallError(WorkerProtocolError):
    """A classified control-plane tool rejection surfaced to the local MCP caller."""

    def __init__(self, response: FlowToolResponse) -> None:
        assert response.error is not None and response.error_kind is not None
        self.kind: FlowToolErrorKind = response.error_kind
        self.retryable = response.retryable
        self.retry_after = response.retry_after
        detail = f"{self.kind.value}: {response.error}"
        if self.retryable:
            detail += "; retryable=true"
        if self.retry_after is not None:
            detail += f"; retry_after={self.retry_after}"
        super().__init__(detail)


@dataclass
class _PendingTool:
    request: FlowToolRequest
    future: asyncio.Future[DeliveryReceipt]


class WorkerClient:
    """Keep executor work alive while fenced WebSocket connections come and go."""

    def __init__(
        self,
        *,
        worker_id: str,
        worker_token: str,
        serve_ws_url: str,
        executor: TaskExecutor,
        journal: WorkerJournal,
        reconnect_min_backoff: float = 1.0,
        reconnect_max_backoff: float = 30.0,
        reconnect_jitter: float = 0.2,
    ) -> None:
        self.connected = False
        self.worker_id = worker_id
        self.worker_token = worker_token
        self.serve_ws_url = serve_ws_url
        self.executor = executor
        self.journal = journal
        self.reconnect_min_backoff = reconnect_min_backoff
        self.reconnect_max_backoff = reconnect_max_backoff
        self.reconnect_jitter = reconnect_jitter
        self._stop = asyncio.Event()
        self._connection_epoch: str | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._assignment: Assignment | None = None
        self._execution: asyncio.Task[ExecutionResult] | None = None
        self._tool_requests: asyncio.Queue[_PendingTool] = asyncio.Queue()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        call_id: str | None = None,
        vcs_outcome: VcsOutcome,
    ) -> DeliveryReceipt:
        """Route one worker-local tool invocation over the active assignment socket."""

        assignment = self._assignment
        if assignment is None:
            raise WorkerProtocolError("Flow tool call has no active assignment")
        future: asyncio.Future[DeliveryReceipt] = asyncio.get_running_loop().create_future()
        request = FlowToolRequest(
            job_id=assignment.job_id,
            flow_id=assignment.flow_id,
            attempt_id=assignment.task.attempt_id,
            assignment_token=assignment.assignment_token,
            connection_epoch=assignment.connection_epoch,
            call_id=call_id or uuid4().hex,
            name=name,
            arguments=arguments,
            vcs_backend=vcs_outcome.backend,
            branch=vcs_outcome.branch,
            commit_sha=vcs_outcome.commit_sha,
            remote_sha=vcs_outcome.remote_sha,
        )
        await self._tool_requests.put(_PendingTool(request, future))
        return await future

    async def run(self) -> None:
        delay = self.reconnect_min_backoff
        try:
            while not self._stop.is_set():
                try:
                    async with connect(
                        self.serve_ws_url,
                        additional_headers={
                            "Authorization": f"Bearer {self.worker_token}",
                            "X-FlowTool-Worker-ID": self.worker_id,
                        },
                    ) as connection:
                        delay = self.reconnect_min_backoff
                        self._connection_task = asyncio.create_task(self.run_connection(connection))
                        await self._connection_task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.connected = False
                    jitter = random.uniform(
                        1 - self.reconnect_jitter, 1 + self.reconnect_jitter
                    )
                    wait = delay * jitter
                    logger.warning(
                        "worker connection lost; retrying in {:.2f}s: {}", wait, exc
                    )
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=wait)
                    except TimeoutError:
                        delay = min(delay * 2, self.reconnect_max_backoff)
        finally:
            self.connected = False
            await self._cancel_execution()

    async def run_connection(self, connection: Connection) -> None:
        await connection.send(
            Register(
                worker_id=self.worker_id,
                recovery=self._recovery_descriptor(),
            ).model_dump_json()
        )
        heartbeat_interval = 15.0
        first: ServerMessage | None = parse_server_message(
            await _receive_text(connection)
        )
        self._connection_epoch = getattr(first, "connection_epoch", None)
        if isinstance(first, ProtocolError):
            raise WorkerProtocolError(
                f"worker registration rejected: {first.code}: {first.detail}"
            )
        if isinstance(first, StopExecution):
            await self._handle_stop(connection, first)
            first = parse_server_message(await _receive_text(connection))
        if isinstance(first, RecoveryRejected):
            self._clear_rejected(first.job_id)
            await self._cancel_execution()
            first = parse_server_message(await _receive_text(connection))
        elif isinstance(first, ResultAccepted):
            self._validate_recovery_message(first.job_id, first.assignment_token)
            self._acknowledge(first)
            first = parse_server_message(await _receive_text(connection))
        elif isinstance(first, ResumeAssignment):
            self._validate_recovery_message(first.job_id, first.assignment_token)
            if self._assignment is None or self._execution is None:
                record = self.journal.load()
                if record is None or record.state != "started" or record.archive_task is None:
                    raise WorkerProtocolError("serve resumed an assignment without a live executor")
                # Runtime holds the original root's process lock. Archive starts no subprocesses,
                # so a new process can safely continue this journaled deletion only.
                self._assignment = Assignment(
                    job_id=record.job_id, flow_id=record.flow_id,
                    assignment_token=record.assignment_token, connection_epoch=first.connection_epoch,
                    task=record.archive_task,
                )
                self._execution = asyncio.create_task(self.executor.execute(record.archive_task, tool_caller=self))
            self._assignment = self._assignment.model_copy(
                update={"connection_epoch": first.connection_epoch}
            )
            heartbeat_interval = first.heartbeat_interval
            self.connected = True
            await self._drive_assignment(connection, first.heartbeat_interval)
            first = None
        elif isinstance(first, ReplayResult):
            self._validate_recovery_message(first.job_id, first.assignment_token)
            heartbeat_interval = first.heartbeat_interval
            self.connected = True
            await self._replay_result(connection, first)
            first = None
        if first is not None and not isinstance(first, Registered):
            raise WorkerProtocolError(
                f"expected Registered, received {type(first).__name__}"
            )
        self.connected = True
        logger.bind(event="worker_connected", reason_code="ok", worker_id=self.worker_id).info("")

        if isinstance(first, Registered):
            self._connection_epoch = first.connection_epoch
            heartbeat_interval = first.heartbeat_interval
        while not self._stop.is_set():
            message = parse_server_message(await _receive_text(connection))
            if isinstance(message, ProtocolError):
                raise WorkerProtocolError(
                    f"serve protocol error: {message.code}: {message.detail}"
                )
            if isinstance(message, StopExecution):
                await self._handle_stop(connection, message)
                continue
            if not isinstance(message, Assignment):
                raise WorkerProtocolError(
                    f"expected Assignment, received {type(message).__name__}"
                )
            await self._execute_assignment(connection, message, heartbeat_interval)

    async def _execute_assignment(
        self,
        connection: Connection,
        assignment: Assignment,
        heartbeat_interval: float,
    ) -> None:
        if self._assignment is not None or self._execution is not None:
            raise WorkerProtocolError("received a second assignment while busy")
        self._assignment = assignment
        self.journal.record_received(assignment)
        await connection.send(
            Started(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch,
            ).model_dump_json()
        )
        self.journal.record_started()
        with logger.contextualize(flow_id=assignment.flow_id, job_id=assignment.job_id,
                                  worker_id=self.worker_id, epoch=assignment.connection_epoch):
            logger.bind(event="job_started", reason_code="ok").info("")
            self._execution = asyncio.create_task(
                self.executor.execute(assignment.task, assignment.secret, tool_caller=self)
            )
        await self._drive_assignment(connection, heartbeat_interval)

    async def _drive_assignment(
        self,
        connection: Connection,
        heartbeat_interval: float,
    ) -> None:
        assignment = self._assignment
        execution = self._execution
        if assignment is None:
            raise WorkerProtocolError("active assignment is missing")
        record = self.journal.load()
        if execution is None and (record is None or record.terminal is None):
            raise WorkerProtocolError("active assignment has no executor or terminal result")
        heartbeat = asyncio.create_task(
            self._heartbeat(connection, assignment, heartbeat_interval)
        )
        incoming = asyncio.create_task(_receive_text(connection))
        try:
            if execution is not None:
                while not execution.done():
                    next_tool = asyncio.create_task(self._tool_requests.get())
                    completed, _ = await asyncio.wait(
                        {execution, heartbeat, next_tool, incoming},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if heartbeat in completed:
                        next_tool.cancel()
                        await asyncio.gather(next_tool, return_exceptions=True)
                        heartbeat.result()
                        raise WorkerProtocolError("heartbeat task stopped unexpectedly")
                    if incoming in completed:
                        next_tool.cancel()
                        await asyncio.gather(next_tool, return_exceptions=True)
                        message = parse_server_message(incoming.result())
                        if not isinstance(message, StopExecution):
                            raise WorkerProtocolError("unexpected message during execution")
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
                        await self._handle_stop(connection, message)
                        return
                    if next_tool in completed:
                        pending = next_tool.result()
                        request = pending.request.model_copy(
                            update={"connection_epoch": assignment.connection_epoch}
                        )
                        try:
                            await connection.send(request.model_dump_json())
                            response = parse_server_message(await incoming)
                            if isinstance(response, StopExecution):
                                heartbeat.cancel()
                                await asyncio.gather(heartbeat, return_exceptions=True)
                                pending.future.cancel()
                                await self._handle_stop(connection, response)
                                return
                            incoming = asyncio.create_task(_receive_text(connection))
                        except BaseException:
                            await self._tool_requests.put(pending)
                            raise
                        if not isinstance(response, FlowToolResponse) or response.call_id != request.call_id:
                            await self._tool_requests.put(pending)
                            raise WorkerProtocolError("Flow tool response does not match its request")
                        if response.error is not None:
                            pending.future.set_exception(FlowToolCallError(response))
                        elif response.result is None:
                            pending.future.set_exception(
                                WorkerProtocolError("Flow tool returned no result")
                            )
                        else:
                            pending.future.set_result(response.result)
                        continue
                    next_tool.cancel()
                    await asyncio.gather(next_tool, return_exceptions=True)
                terminal = self._record_completed_execution()
            else:
                assert record is not None and record.terminal is not None
                terminal = record.terminal.model_copy(
                    update={"connection_epoch": assignment.connection_epoch}
                )

            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await connection.send(terminal.model_dump_json())
            accepted = parse_server_message(await incoming)
            if isinstance(accepted, StopExecution):
                await self._handle_stop(connection, accepted)
                return
            if (
                not isinstance(accepted, ResultAccepted)
                or accepted.job_id != assignment.job_id
                or accepted.assignment_token != assignment.assignment_token
                or accepted.connection_epoch != assignment.connection_epoch
            ):
                raise RuntimeError("serve did not acknowledge the remote result")
            self._acknowledge(accepted)
        finally:
            incoming.cancel()
            heartbeat.cancel()
            await asyncio.gather(incoming, heartbeat, return_exceptions=True)

    async def _handle_stop(self, connection: Connection, request: StopExecution) -> None:
        record = self.journal.load()
        if record is None or (
                record.job_id, record.flow_id, record.assignment_token) != (
                request.job_id, request.flow_id, request.assignment_token):
            raise WorkerProtocolError("stop blocked: original journal is missing")
        if self._connection_epoch != request.connection_epoch:
            raise WorkerProtocolError("stop belongs to another connection epoch")
        receipt = record.stopped
        if receipt is None:
            if record.execution_nonce is None:
                raise WorkerProtocolError("stop blocked: journal lacks execution ownership nonce")
            if record.state == "started" and self._execution is None:
                raise WorkerProtocolError("stop blocked: old process ownership is unknown")
            if self._execution is not None:
                execution = self._execution
                execution.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(execution), timeout=15)
                except asyncio.CancelledError:
                    if not execution.cancelled():
                        raise
                except Exception as exc:
                    raise WorkerProtocolError("stop blocked: executor cleanup was not confirmed") from exc
                self._execution = None
            receipt = ExecutionStopped(**request.model_dump(exclude={'type'}), execution_nonce=record.execution_nonce)
            self.journal.record_stopped(receipt)
        if receipt.request_id != request.request_id:
            raise WorkerProtocolError("stop request conflicts with durable receipt")
        self._discard_tool_requests()
        receipt = receipt.model_copy(update={'connection_epoch': request.connection_epoch})
        await connection.send(receipt.model_dump_json())
        # A result may already have been sent when the server requested stop. Only a matching
        # stop ACK permits journal retirement; unrelated result ACKs cannot authorize release.
        accepted = parse_server_message(await _receive_text(connection))
        if not isinstance(accepted, StopAccepted) or accepted.connection_epoch != request.connection_epoch:
            raise WorkerProtocolError("stop was not acknowledged")
        self.journal.acknowledge_stop(accepted)
        self._assignment = None

    def _record_completed_execution(self) -> Succeeded | Failed:
        """Capture a finished task before recovery or sending; retain it if journaling fails."""
        assignment, execution = self._assignment, self._execution
        if assignment is None or execution is None or not execution.done():
            raise WorkerProtocolError("cannot record an unfinished or missing executor")
        try:
            result = execution.result()
            terminal: Succeeded | Failed = Succeeded(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch,
                result=result,
            )
        except Exception as exc:  # noqa: BLE001
            # Exceptions can contain model output or a credential echoed by a command.
            logger.bind(event="job_failed", reason_code="execution_failed",
                        job_id=assignment.job_id, flow_id=assignment.flow_id,
                        worker_id=self.worker_id).opt(exception=exc).error("")
            terminal = Failed(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch,
                error=str(exc),
            )
        if assignment.secret is not None:
            terminal = type(terminal).model_validate_json(
                terminal.model_dump_json().replace(
                    assignment.secret.push.token.get_secret_value(), "[REDACTED]",
                )
            )
        self.journal.record_terminal(terminal)
        self._execution = None
        return terminal

    async def _heartbeat(
        self,
        connection: Connection,
        assignment: Assignment,
        interval: float,
    ) -> None:
        while True:
            await asyncio.sleep(interval)
            await connection.send(
                Heartbeat(
                    job_id=assignment.job_id,
                    assignment_token=assignment.assignment_token,
                    connection_epoch=assignment.connection_epoch,
                ).model_dump_json()
            )

    def stop(self) -> None:
        self.connected = False
        self._stop.set()
        if self._connection_task is not None:
            self._connection_task.cancel()

    def _recovery_descriptor(self) -> RecoveryDescriptor | None:
        if (self._execution is not None
                and self._execution.done() and not self._execution.cancelled()):
            self._record_completed_execution()
        record = self.journal.load()
        if record is None:
            return None
        return RecoveryDescriptor(
            job_id=record.job_id,
            assignment_token=record.assignment_token,
            state=record.state,
            result_digest=record.result_digest,
            stop_request_id=record.stopped.request_id if record.stopped else None,
            archive_resume=record.archive_task is not None and record.state == "started",
            executor_active=(
                record.state == "started"
                and self._execution is not None
                and not self._execution.done()
            ),
        )

    def _validate_recovery_message(self, job_id: int, token: str) -> None:
        record = self.journal.load()
        if record is None or (record.job_id, record.assignment_token) != (job_id, token):
            raise WorkerProtocolError("recovery response does not match the worker journal")

    async def _replay_result(
        self, connection: Connection, replay: ReplayResult
    ) -> None:
        record = self.journal.load()
        if record is None or record.terminal is None:
            raise WorkerProtocolError("serve requested a result missing from the journal")
        terminal = record.terminal.model_copy(
            update={"connection_epoch": replay.connection_epoch}
        )
        await connection.send(terminal.model_dump_json())
        accepted = parse_server_message(await _receive_text(connection))
        if (
            not isinstance(accepted, ResultAccepted)
            or accepted.job_id != replay.job_id
            or accepted.assignment_token != replay.assignment_token
            or accepted.connection_epoch != replay.connection_epoch
        ):
            raise WorkerProtocolError("serve did not acknowledge the replayed result")
        self._acknowledge(accepted)

    def _acknowledge(self, accepted: ResultAccepted) -> None:
        self.journal.acknowledge(accepted.job_id, accepted.state)
        self._assignment = None
        self._execution = None

    def _clear_rejected(self, job_id: int) -> None:
        self.journal.clear_rejected(job_id)
        self._assignment = None

    async def _cancel_execution(self) -> None:
        if self._execution is None:
            return
        self._execution.cancel()
        await asyncio.gather(self._execution, return_exceptions=True)
        self._execution = None
        self._discard_tool_requests()

    def _discard_tool_requests(self) -> None:
        while not self._tool_requests.empty():
            pending = self._tool_requests.get_nowait()
            if not pending.future.done():
                pending.future.set_exception(WorkerProtocolError("assignment was cancelled"))


async def _receive_text(connection: Connection) -> str:
    raw = await connection.recv()
    return raw.decode() if isinstance(raw, bytes) else raw
