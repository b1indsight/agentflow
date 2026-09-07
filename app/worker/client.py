"""Outbound WebSocket client with durable single-assignment recovery."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from loguru import logger
from websockets.asyncio.client import connect

from app.models import VcsOutcome
from app.protocol import (
    Assignment,
    ExecutionResult,
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
    Succeeded,
    parse_server_message,
)
from app.worker.journal import JournalRecord, WorkerJournal


class Connection(Protocol):
    async def send(self, data: str) -> None: ...

    async def recv(self) -> str | bytes: ...


class TaskExecutor(Protocol):
    async def execute(
        self, task: ExecutionTask, secret=None
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
    future: asyncio.Future[dict[str, object]]


class WorkerClient:
    """Keep executor work alive while fenced WebSocket connections come and go."""

    def __init__(
        self,
        *,
        worker_id: str,
        worker_token: str,
        serve_ws_url: str,
        executor: TaskExecutor,
        journal: WorkerJournal | None = None,
        reconnect_min_backoff: float = 1.0,
        reconnect_max_backoff: float = 30.0,
        reconnect_jitter: float = 0.2,
    ) -> None:
        self.worker_id = worker_id
        self.worker_token = worker_token
        self.serve_ws_url = serve_ws_url
        self.executor = executor
        self.journal = journal
        self.reconnect_min_backoff = reconnect_min_backoff
        self.reconnect_max_backoff = reconnect_max_backoff
        self.reconnect_jitter = reconnect_jitter
        self._stop = asyncio.Event()
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
    ) -> dict[str, object]:
        """Route one worker-local tool invocation over the active assignment socket."""

        assignment = self._assignment
        if assignment is None:
            raise WorkerProtocolError("Flow tool call has no active assignment")
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        request = FlowToolRequest(
            job_id=assignment.job_id,
            flow_id=assignment.flow_id,
            attempt_id=assignment.task.attempt_id,
            assignment_token=assignment.assignment_token,
            connection_epoch=assignment.connection_epoch,
            call_id=call_id or uuid4().hex,
            name=name,
            arguments=arguments,
            vcs_backend=vcs_outcome.verified_backend,
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
                        await self.run_connection(connection)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
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
        if isinstance(first, ProtocolError):
            raise WorkerProtocolError(
                f"worker registration rejected: {first.code}: {first.detail}"
            )
        if isinstance(first, RecoveryRejected):
            self._clear_rejected(first.job_id)
            await self._cancel_execution()
            first = parse_server_message(await _receive_text(connection))
        elif isinstance(first, ResultAccepted):
            self._validate_recovery_message(first.job_id, first.assignment_token)
            self._acknowledge(first)
            first = parse_server_message(await _receive_text(connection))
        elif isinstance(first, ResumeAssignment):
            if self._assignment is None or self._execution is None:
                raise WorkerProtocolError("serve resumed an assignment without a live executor")
            self._validate_recovery_message(first.job_id, first.assignment_token)
            self._assignment = self._assignment.model_copy(
                update={"connection_epoch": first.connection_epoch}
            )
            heartbeat_interval = first.heartbeat_interval
            await self._drive_assignment(connection, first.heartbeat_interval)
            first = None
        elif isinstance(first, ReplayResult):
            self._validate_recovery_message(first.job_id, first.assignment_token)
            heartbeat_interval = first.heartbeat_interval
            await self._replay_result(connection, first)
            first = None
        if first is not None and not isinstance(first, Registered):
            raise WorkerProtocolError(
                f"expected Registered, received {type(first).__name__}"
            )
        logger.info("worker {} registered with control plane", self.worker_id)

        if isinstance(first, Registered):
            heartbeat_interval = first.heartbeat_interval
        while not self._stop.is_set():
            message = parse_server_message(await _receive_text(connection))
            if isinstance(message, ProtocolError):
                raise WorkerProtocolError(
                    f"serve protocol error: {message.code}: {message.detail}"
                )
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
        if self.journal is not None:
            self.journal.record_received(assignment)
        await connection.send(
            Started(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
                connection_epoch=assignment.connection_epoch,
            ).model_dump_json()
        )
        if self.journal is not None:
            self.journal.record_started()
        self._execution = asyncio.create_task(
            self.executor.execute(assignment.task, assignment.secret)
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
        record = self._journal_record()
        if execution is None and (record is None or record.terminal is None):
            raise WorkerProtocolError("active assignment has no executor or terminal result")
        heartbeat = asyncio.create_task(
            self._heartbeat(connection, assignment, heartbeat_interval)
        )
        try:
            if execution is not None:
                while not execution.done():
                    next_tool = asyncio.create_task(self._tool_requests.get())
                    completed, _ = await asyncio.wait(
                        {execution, heartbeat, next_tool},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if heartbeat in completed:
                        next_tool.cancel()
                        await asyncio.gather(next_tool, return_exceptions=True)
                        heartbeat.result()
                        raise WorkerProtocolError("heartbeat task stopped unexpectedly")
                    if next_tool in completed:
                        pending = next_tool.result()
                        request = pending.request.model_copy(
                            update={"connection_epoch": assignment.connection_epoch}
                        )
                        try:
                            await connection.send(request.model_dump_json())
                            response = parse_server_message(await _receive_text(connection))
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
                    logger.error("remote job {} failed ({})", assignment.job_id, type(exc).__name__)
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
                self._execution = None
                if self.journal is not None:
                    self.journal.record_terminal(terminal)
            else:
                assert record is not None and record.terminal is not None
                terminal = record.terminal.model_copy(
                    update={"connection_epoch": assignment.connection_epoch}
                )

            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await connection.send(terminal.model_dump_json())
            accepted = parse_server_message(await _receive_text(connection))
            if (
                not isinstance(accepted, ResultAccepted)
                or accepted.job_id != assignment.job_id
                or accepted.assignment_token != assignment.assignment_token
                or accepted.connection_epoch != assignment.connection_epoch
            ):
                raise RuntimeError("serve did not acknowledge the remote result")
            self._acknowledge(accepted)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

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
        self._stop.set()

    def _journal_record(self) -> JournalRecord | None:
        return self.journal.load() if self.journal is not None else None

    def _recovery_descriptor(self) -> RecoveryDescriptor | None:
        record = self._journal_record()
        if record is None:
            return None
        return RecoveryDescriptor(
            job_id=record.job_id,
            assignment_token=record.assignment_token,
            state=record.state,
            result_digest=record.result_digest,
            executor_active=(
                record.state == "started"
                and self._execution is not None
                and not self._execution.done()
            ),
        )

    def _validate_recovery_message(self, job_id: int, token: str) -> None:
        record = self._journal_record()
        if record is None or (record.job_id, record.assignment_token) != (job_id, token):
            raise WorkerProtocolError("recovery response does not match the worker journal")

    async def _replay_result(
        self, connection: Connection, replay: ReplayResult
    ) -> None:
        record = self._journal_record()
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
        if self.journal is not None:
            self.journal.acknowledge(accepted.job_id, accepted.state)
        self._assignment = None
        self._execution = None

    def _clear_rejected(self, job_id: int) -> None:
        if self.journal is not None:
            self.journal.clear_rejected(job_id)
        self._assignment = None

    async def _cancel_execution(self) -> None:
        if self._execution is None:
            return
        self._execution.cancel()
        await asyncio.gather(self._execution, return_exceptions=True)
        self._execution = None
        while not self._tool_requests.empty():
            pending = self._tool_requests.get_nowait()
            if not pending.future.done():
                pending.future.set_exception(WorkerProtocolError("assignment was cancelled"))


async def _receive_text(connection: Connection) -> str:
    raw = await connection.recv()
    return raw.decode() if isinstance(raw, bytes) else raw
