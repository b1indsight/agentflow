"""Outbound WebSocket connections and worker protocol dispatch."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import Protocol

from loguru import logger
from websockets.asyncio.client import connect

from app.contracts.conversation import ConversationFailure, HistoryPage, HistoryQuery
from app.contracts.protocol import (
    Assignment,
    ConversationQuery,
    ConversationResult,
    FlowToolResponse,
    Heartbeat,
    ProtocolError,
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
    parse_server_message,
)
from app.worker.execution.lifecycle import WorkerExecution, WorkerProtocolError


class Connection(Protocol):
    async def send(self, data: str) -> None: ...

    async def recv(self) -> str | bytes: ...


class WorkerClient:
    """Drive fenced connections while borrowing a process-lifetime execution owner."""

    def __init__(
        self,
        *,
        worker_id: str,
        worker_token: str,
        serve_ws_url: str,
        execution: WorkerExecution,
        read_history: Callable[[HistoryQuery], HistoryPage],
        reconnect_min_backoff: float = 1.0,
        reconnect_max_backoff: float = 30.0,
        reconnect_jitter: float = 0.2,
    ) -> None:
        self.connected = False
        self.worker_id = worker_id
        self.worker_token = worker_token
        self.serve_ws_url = serve_ws_url
        self.execution = execution
        self.read_history = read_history
        self.reconnect_min_backoff = reconnect_min_backoff
        self.reconnect_max_backoff = reconnect_max_backoff
        self.reconnect_jitter = reconnect_jitter
        self._stop = asyncio.Event()
        self._connection_epoch: str | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._history_tasks: set[asyncio.Task[None]] = set()

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
            if self._connection_task is not None:
                self._connection_task.cancel()
                await asyncio.gather(self._connection_task, return_exceptions=True)
                self._connection_task = None

    async def run_connection(self, connection: Connection) -> None:
        try:
            await self._run_connection(connection)
        finally:
            self.connected = False
            tasks = list(self._history_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._history_tasks.clear()

    async def _receive_message(self, connection: Connection) -> ServerMessage:
        """Keep one socket reader while history requests run alongside task messages."""
        while True:
            raw = await connection.recv()
            message = parse_server_message(raw.decode() if isinstance(raw, bytes) else raw)
            if not isinstance(message, ConversationQuery):
                return message
            if len(self._history_tasks) >= 4:
                await connection.send(ConversationResult(
                    request_id=message.request_id, connection_epoch=message.connection_epoch,
                    error="query_busy",
                ).model_dump_json())
                continue
            task = asyncio.create_task(self._answer_history(connection, message))
            self._history_tasks.add(task)
            task.add_done_callback(self._history_finished)

    def _history_finished(self, task: asyncio.Task[None]) -> None:
        self._history_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("conversation response could not be delivered")

    async def _answer_history(self, connection: Connection, query: ConversationQuery) -> None:
        try:
            request = HistoryQuery(session_id=query.session_id, cursor=query.cursor, limit=query.limit)
            page = await asyncio.to_thread(self.read_history, request)
            result = ConversationResult(request_id=query.request_id, connection_epoch=query.connection_epoch, page=page)
        except ConversationFailure as exc:
            result = ConversationResult(request_id=query.request_id,
                                        connection_epoch=query.connection_epoch, error=exc.code)
        await connection.send(result.model_dump_json())

    async def _run_connection(self, connection: Connection) -> None:
        """Register, supervise assignment traffic and heartbeats, and join both on exit."""
        await connection.send(
            Register(worker_id=self.worker_id, recovery=self.execution.recovery()).model_dump_json()
        )
        first = await self._receive_message(connection)
        self._connection_epoch = getattr(first, "connection_epoch", None)
        if isinstance(first, ProtocolError):
            raise WorkerProtocolError(
                f"worker registration rejected: {first.code}: {first.detail}"
            )
        if isinstance(first, StopExecution):
            await self._handle_stop(connection, first)
            first = await self._receive_message(connection)
        if isinstance(first, RecoveryRejected):
            await self.execution.reject_recovery(first.job_id)
            first = await self._receive_message(connection)
        elif isinstance(first, ResultAccepted):
            self.execution.on_result_accepted(first)
            first = await self._receive_message(connection)
        if not isinstance(first, (Registered, ResumeAssignment, ReplayResult)):
            raise WorkerProtocolError(
                f"expected registration or recovery, received {type(first).__name__}"
            )
        self._connection_epoch = first.connection_epoch
        self.connected = True
        logger.bind(event="worker_connected", reason_code="ok", worker_id=self.worker_id).info("")

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(first.heartbeat_interval)
                assignment = self.execution.lease()
                if assignment is None:
                    continue
                await connection.send(Heartbeat(
                    job_id=assignment.job_id,
                    assignment_token=assignment.assignment_token,
                    connection_epoch=assignment.connection_epoch,
                ).model_dump_json())

        heartbeat_task = asyncio.create_task(heartbeat())
        assignments = asyncio.create_task(self._serve_assignments(connection, first))
        try:
            completed, _ = await asyncio.wait(
                {heartbeat_task, assignments}, return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in completed:
                heartbeat_task.result()
                raise WorkerProtocolError("heartbeat task stopped unexpectedly")
            assignments.result()
        finally:
            heartbeat_task.cancel()
            assignments.cancel()
            await asyncio.gather(heartbeat_task, assignments, return_exceptions=True)

    async def _serve_assignments(
        self, connection: Connection, first: Registered | ResumeAssignment | ReplayResult,
    ) -> None:
        """Resume prior work, then process assignments on the registered connection."""
        if isinstance(first, ResumeAssignment):
            self.execution.resume(first)
            await self._drive_assignment(connection)
        elif isinstance(first, ReplayResult):
            await self._replay_result(connection, first)
        while not self._stop.is_set():
            message = await self._receive_message(connection)
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
            await self._execute_assignment(connection, message)

    async def _execute_assignment(self, connection: Connection, assignment: Assignment) -> None:
        self.execution.receive(assignment)
        await connection.send(Started(
            job_id=assignment.job_id, assignment_token=assignment.assignment_token,
            connection_epoch=assignment.connection_epoch,
        ).model_dump_json())
        self.execution.start()
        await self._drive_assignment(connection)

    async def _drive_assignment(self, connection: Connection) -> None:
        """Observe execution and route tool traffic without owning either lifetime."""
        assert self._connection_epoch is not None
        terminal = asyncio.create_task(self.execution.wait_terminal(self._connection_epoch))
        incoming = asyncio.create_task(self._receive_message(connection))
        next_tool = asyncio.create_task(self.execution.next_tool_call())
        try:
            while not terminal.done():
                completed, _ = await asyncio.wait(
                    {terminal, next_tool, incoming}, return_when=asyncio.FIRST_COMPLETED,
                )
                if incoming in completed:
                    message = incoming.result()
                    if not isinstance(message, StopExecution):
                        raise WorkerProtocolError("unexpected message during execution")
                    await self._handle_stop(connection, message)
                    return
                if next_tool in completed and not terminal.done():
                    call = next_tool.result()
                    request = call.request.model_copy(update={"connection_epoch": self._connection_epoch})
                    await connection.send(request.model_dump_json())
                    response = await incoming
                    if isinstance(response, StopExecution):
                        await self._handle_stop(connection, response)
                        return
                    if not isinstance(response, FlowToolResponse):
                        raise WorkerProtocolError("Flow tool response does not match its request")
                    self.execution.finish_tool_call(call, response)
                    incoming = asyncio.create_task(self._receive_message(connection))
                    next_tool = asyncio.create_task(self.execution.next_tool_call())
            result = terminal.result()
            await connection.send(result.model_dump_json())
            accepted = await incoming
            if isinstance(accepted, StopExecution):
                await self._handle_stop(connection, accepted)
                return
            if (not isinstance(accepted, ResultAccepted)
                    or accepted.job_id != result.job_id
                    or accepted.assignment_token != result.assignment_token
                    or accepted.connection_epoch != result.connection_epoch):
                raise WorkerProtocolError("serve did not acknowledge the remote result")
            self.execution.on_result_accepted(accepted)
        finally:
            for task in (incoming, next_tool, terminal):
                task.cancel()
            await asyncio.gather(incoming, next_tool, terminal, return_exceptions=True)

    async def _handle_stop(self, connection: Connection, request: StopExecution) -> None:
        if self._connection_epoch != request.connection_epoch:
            raise WorkerProtocolError("stop belongs to another connection epoch")
        receipt = await self.execution.stop(request)
        await connection.send(receipt.model_dump_json())
        # Result ACKs cannot retire a stop. Only the matching stop ACK authorizes release.
        accepted = await self._receive_message(connection)
        if not isinstance(accepted, StopAccepted) or accepted.connection_epoch != request.connection_epoch:
            raise WorkerProtocolError("stop was not acknowledged")
        self.execution.on_stop_accepted(accepted)

    def stop(self) -> None:
        """Close communication only; the runtime separately shuts down its execution owner."""
        self.connected = False
        self._stop.set()
        if self._connection_task is not None:
            self._connection_task.cancel()

    async def _replay_result(self, connection: Connection, replay: ReplayResult) -> None:
        terminal = await self.execution.wait_terminal(replay.connection_epoch)
        if (terminal.job_id, terminal.assignment_token) != (replay.job_id, replay.assignment_token):
            raise WorkerProtocolError("recovery response does not match the worker session")
        await connection.send(terminal.model_dump_json())
        accepted = await self._receive_message(connection)
        if (not isinstance(accepted, ResultAccepted)
                or accepted.job_id != replay.job_id
                or accepted.assignment_token != replay.assignment_token
                or accepted.connection_epoch != replay.connection_epoch):
            raise WorkerProtocolError("serve did not acknowledge the replayed result")
        self.execution.on_result_accepted(accepted)
