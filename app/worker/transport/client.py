"""Outbound WebSocket connections and worker protocol dispatch."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import Protocol

from loguru import logger
from websockets.asyncio.client import connect

from app.communication.gateway import Gateway, Registration
from app.communication.session import Responder, Session
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
    WorkerMessage,
    parse_server_message,
)
from app.worker.execution.lifecycle import PendingToolCall, WorkerExecution, WorkerProtocolError


class Connection(Protocol):
    async def send(self, data: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self, code: int = 1000) -> None: ...


class _WebSocketTransport:
    """Map the outbound WebSocket API to the same transport the server uses."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    async def send_text(self, data: str) -> None:
        await self.connection.send(data)

    async def receive_text(self) -> str:
        raw = await self.connection.recv()
        return raw.decode() if isinstance(raw, bytes) else raw

    async def close(self, code: int = 1000) -> None:
        await self.connection.close(code)


def _is_history_query(message: ServerMessage) -> bool:
    return isinstance(message, ConversationQuery)


def _history_busy(message: ServerMessage) -> WorkerMessage:
    assert isinstance(message, ConversationQuery)
    return ConversationResult(request_id=message.request_id, connection_epoch=message.connection_epoch,
                              error="query_busy")


def _bind_epoch(connection: Session[ServerMessage, WorkerMessage], epoch: str) -> None:
    try:
        connection.bind_epoch(epoch)
    except ValueError as exc:
        raise WorkerProtocolError(str(exc)) from exc


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
        self._peers = Gateway[ServerMessage, WorkerMessage]()
        self._connection_task: asyncio.Task[None] | None = None

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
            self._peers.seal()
            try:
                if self._connection_task is not None:
                    self._connection_task.cancel()
                    await asyncio.gather(self._connection_task, return_exceptions=True)
                    self._connection_task = None
            finally:
                await self._peers.close()

    async def run_connection(self, connection: Connection) -> None:
        session = Session[ServerMessage, WorkerMessage](
            _WebSocketTransport(connection), parse=parse_server_message,
            responder=Responder(matches=_is_history_query, handle=self._answer_history, busy=_history_busy),
        )
        registration: Registration[ServerMessage, WorkerMessage] | None = None
        try:
            # Admission precedes Register and every borrow of the execution owner.
            registration = await self._peers.register("server", session, replace=False)
            await self._run_connection(session)
        finally:
            if registration is not None:
                self.connected = False
                await self._peers.unregister(registration)
            else:
                await session.close()

    async def _answer_history(self, query: ServerMessage) -> WorkerMessage:
        assert isinstance(query, ConversationQuery)
        try:
            request = HistoryQuery(session_id=query.session_id, cursor=query.cursor, limit=query.limit)
            page = await asyncio.to_thread(self.read_history, request)
            return ConversationResult(request_id=query.request_id, connection_epoch=query.connection_epoch, page=page)
        except ConversationFailure as exc:
            return ConversationResult(request_id=query.request_id,
                                      connection_epoch=query.connection_epoch, error=exc.code)

    async def _run_connection(self, connection: Session[ServerMessage, WorkerMessage]) -> None:
        """Register, supervise assignment traffic and heartbeats, and join both on exit."""
        await connection.send(
            Register(worker_id=self.worker_id, recovery=self.execution.recovery())
        )
        connection.start()
        first = await connection.receive()
        if isinstance(first, ProtocolError):
            raise WorkerProtocolError(
                f"worker registration rejected: {first.code}: {first.detail}"
            )
        if isinstance(first, StopExecution):
            _bind_epoch(connection, first.connection_epoch)
            await self._handle_stop(connection, first)
            first = await connection.receive()
        if isinstance(first, RecoveryRejected):
            await self.execution.reject_recovery(first.job_id)
            first = await connection.receive()
        elif isinstance(first, ResultAccepted):
            _bind_epoch(connection, first.connection_epoch)
            self.execution.on_result_accepted(first)
            first = await connection.receive()
        if not isinstance(first, (Registered, ResumeAssignment, ReplayResult)):
            raise WorkerProtocolError(
                f"expected registration or recovery, received {type(first).__name__}"
            )
        _bind_epoch(connection, first.connection_epoch)
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
                ))

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
        self, connection: Session[ServerMessage, WorkerMessage], first: Registered | ResumeAssignment | ReplayResult,
    ) -> None:
        """Resume prior work, then process assignments on the registered connection."""
        if isinstance(first, ResumeAssignment):
            self.execution.resume(first)
            await self._drive_assignment(connection)
        elif isinstance(first, ReplayResult):
            await self._replay_result(connection, first)
        while not self._stop.is_set():
            message = await connection.receive()
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

    async def _execute_assignment(self, connection: Session[ServerMessage, WorkerMessage], assignment: Assignment) -> None:
        _bind_epoch(connection, assignment.connection_epoch)
        self.execution.receive(assignment)
        await connection.send(Started(
            job_id=assignment.job_id, assignment_token=assignment.assignment_token,
            connection_epoch=assignment.connection_epoch,
        ))
        self.execution.start()
        await self._drive_assignment(connection)

    async def _drive_assignment(self, connection: Session[ServerMessage, WorkerMessage]) -> None:
        """Observe execution and route tool traffic without owning either lifetime."""
        assert connection.epoch is not None
        terminal = asyncio.create_task(self.execution.wait_terminal(connection.epoch))
        incoming = asyncio.create_task(connection.receive())
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
                    request = call.request.model_copy(update={"connection_epoch": connection.epoch})
                    # Waiting for another writer must not send a tool cancelled since borrowing.
                    def is_current(call: PendingToolCall = call) -> bool:
                        return not call.future.done()

                    if not await connection.send(request, is_current=is_current):
                        next_tool = asyncio.create_task(self.execution.next_tool_call())
                        continue
                    response = await incoming
                    if isinstance(response, StopExecution):
                        await self._handle_stop(connection, response)
                        return
                    if not isinstance(response, FlowToolResponse):
                        raise WorkerProtocolError("Flow tool response does not match its request")
                    self.execution.finish_tool_call(call, response)
                    incoming = asyncio.create_task(connection.receive())
                    next_tool = asyncio.create_task(self.execution.next_tool_call())
            result = terminal.result()
            await connection.send(result)
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

    async def _handle_stop(self, connection: Session[ServerMessage, WorkerMessage], request: StopExecution) -> None:
        if connection.epoch != request.connection_epoch:
            raise WorkerProtocolError("stop belongs to another connection epoch")
        receipt = await self.execution.stop(request)
        await connection.send(receipt)
        # Result ACKs cannot retire a stop. Only the matching stop ACK authorizes release.
        accepted = await connection.receive()
        if not isinstance(accepted, StopAccepted) or accepted.connection_epoch != request.connection_epoch:
            raise WorkerProtocolError("stop was not acknowledged")
        self.execution.on_stop_accepted(accepted)

    def stop(self) -> None:
        """Close communication only; the runtime separately shuts down its execution owner."""
        self.connected = False
        self._stop.set()
        self._peers.seal()
        if self._connection_task is not None:
            self._connection_task.cancel()

    async def _replay_result(self, connection: Session[ServerMessage, WorkerMessage], replay: ReplayResult) -> None:
        terminal = await self.execution.wait_terminal(replay.connection_epoch)
        if (terminal.job_id, terminal.assignment_token) != (replay.job_id, replay.assignment_token):
            raise WorkerProtocolError("recovery response does not match the worker session")
        await connection.send(terminal)
        accepted = await connection.receive()
        if (not isinstance(accepted, ResultAccepted)
                or accepted.job_id != replay.job_id
                or accepted.assignment_token != replay.assignment_token
                or accepted.connection_epoch != replay.connection_epoch):
            raise WorkerProtocolError("serve did not acknowledge the replayed result")
        self.execution.on_result_accepted(accepted)
