"""Outbound WebSocket connections and worker protocol dispatch."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import Protocol

from loguru import logger
from websockets.asyncio.client import connect

from app.communication.dispatch import Dispatcher
from app.communication.gateway import Gateway, Registration
from app.communication.session import Responder, Session
from app.contracts.conversation import ConversationFailure, HistoryPage, HistoryQuery
from app.contracts.protocol import (
    AcceptResult,
    AcceptResultArguments,
    AcceptStop,
    AcceptStopArguments,
    Assignment,
    ConversationQuery,
    ConversationResult,
    ExecuteResult,
    ExecutionIdentity,
    FlowToolResponse,
    Heartbeat,
    ProtocolError,
    RecoveryRejected,
    Register,
    Registered,
    ResultAccepted,
    ServerMessage,
    ServerPayload,
    Started,
    StopAccepted,
    StopExecution,
    StopResult,
    Succeeded,
    TerminalPayload,
    ToolCall,
    ToolResult,
    WorkerFrame,
    WorkerMessage,
    accept_result_id,
    accept_stop_id,
    call_header,
    execution_call_id,
    frame_header,
    parse_server_message,
    result_header,
    stop_call_id,
    stop_receipt_id,
    terminal_id,
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


def _is_history_query(message: ToolCall | ToolResult) -> bool:
    return isinstance(message, ConversationQuery)


def _history_busy(message: ToolCall | ToolResult, epoch: str) -> WorkerMessage:
    assert isinstance(message, ConversationQuery)
    return WorkerFrame(
        **frame_header(epoch),
        control=None,
        payload=[
            ConversationResult(
                **result_header(message.message_id),
                method="worker.history",
                error="query_busy",
            )
        ],
    )


def _control_ack(ack_id: str) -> WorkerMessage:
    header = frame_header(None)
    header["ack"] = True
    header["ack_id"] = ack_id
    return WorkerFrame(**header, control=None, payload=[])


def _accept_result(terminal: TerminalPayload) -> AcceptResult:
    identity = terminal_id(terminal)
    return AcceptResult(
        **call_header(accept_result_id(identity)),
        method="server.accept_result",
        arguments=AcceptResultArguments(
            job_id=terminal.job_id,
            assignment_token=terminal.assignment_token,
            result_id=identity,
        ),
    )


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
                    jitter = random.uniform(1 - self.reconnect_jitter, 1 + self.reconnect_jitter)
                    wait = delay * jitter
                    logger.warning("worker connection lost; retrying in {:.2f}s: {}", wait, exc)
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
            _WebSocketTransport(connection),
            parse=parse_server_message,
            make_ack=_control_ack,
            responder=Responder(
                matches=_is_history_query, handle=self._answer_history, busy=_history_busy
            ),
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

    async def _answer_history(self, query: ToolCall | ToolResult, epoch: str) -> WorkerMessage:
        assert isinstance(query, ConversationQuery)
        try:
            request = HistoryQuery(**query.arguments.model_dump())
            page = await asyncio.to_thread(self.read_history, request)
            result = ConversationResult(
                **result_header(query.message_id),
                method="worker.history",
                page=page,
            )
        except ConversationFailure as exc:
            result = ConversationResult(
                **result_header(query.message_id),
                method="worker.history",
                error=exc.code,
            )
        return WorkerFrame(**frame_header(epoch), control=None, payload=[result])

    async def _run_connection(self, connection: Session[ServerMessage, WorkerMessage]) -> None:
        """One business consumer multiplexes frames, terminal observation and tool calls.

        Callbacks finish a transition and return. Replies to tools and A/B are dispatched by
        this same loop, never by a nested receive that blocks its own required reply.
        """
        recovery = self.execution.recovery()
        recovered_terminal = self.execution.result_for_registration()
        expected_result = (
            _accept_result(recovered_terminal) if recovered_terminal is not None else None
        )
        register = Register(
            **frame_header(None),
            control="register",
            worker_id=self.worker_id,
            recovery=recovery,
            payload=[expected_result] if expected_result is not None else [],
        )
        await connection.send(register)
        connection.start()
        incoming = asyncio.create_task(connection.receive())
        heartbeat_task: asyncio.Task[None] | None = None
        terminal: asyncio.Task[TerminalPayload] | None = None
        next_tool: asyncio.Task[PendingToolCall] | None = None
        pending_tool: PendingToolCall | None = None
        expected_stop: AcceptStop | None = None
        ready = False
        frame_epoch: str | None = None

        def observe_execution() -> None:
            nonlocal terminal, next_tool
            if terminal is not None or next_tool is not None:
                raise WorkerProtocolError("execution already observed")
            terminal = asyncio.create_task(self.execution.wait_terminal())
            next_tool = asyncio.create_task(self.execution.next_tool_call())

        async def stop_observing() -> None:
            nonlocal terminal, next_tool
            observers = [task for task in (terminal, next_tool) if task is not None]
            for task in observers:
                task.cancel()
            await asyncio.gather(*observers, return_exceptions=True)
            terminal = next_tool = None

        async def heartbeat(interval: float) -> None:
            while True:
                await asyncio.sleep(interval)
                assignment = self.execution.lease()
                if assignment is None:
                    continue
                await connection.send(
                    Heartbeat(
                        **frame_header(connection.epoch),
                        control="heartbeat",
                        payload=[],
                        job_id=assignment.job_id,
                        assignment_token=assignment.assignment_token,
                    )
                )

        async def execute(call: Assignment) -> None:
            arguments = call.arguments
            if not ready or call.message_id != execution_call_id(
                arguments.job_id, arguments.assignment_token
            ):
                raise WorkerProtocolError("assignment has no authorized execution identity")
            self.execution.receive(arguments)
            if not await connection.send(
                WorkerFrame(
                    **frame_header(connection.epoch),
                    control=None,
                    payload=[
                        Started(
                            **call_header(),
                            method="server.execution_started",
                            arguments=ExecutionIdentity(
                                job_id=arguments.job_id,
                                assignment_token=arguments.assignment_token,
                            ),
                        )
                    ],
                )
            ):
                raise WorkerProtocolError("Started was not written")
            self.execution.start()
            observe_execution()

        async def reject(call: RecoveryRejected) -> None:
            nonlocal expected_result
            if ready or call.arguments.register_message_id != register.message_id:
                raise WorkerProtocolError("recovery rejection belongs to another registration")
            await self.execution.reject_recovery(call.arguments.job_id)
            expected_result = None

        async def accept_result(reply: ResultAccepted) -> None:
            nonlocal expected_result
            if (
                expected_result is None
                or reply.reply_to != expected_result.message_id
                or (reply.result.job_id, reply.result.assignment_token)
                != (expected_result.arguments.job_id, expected_result.arguments.assignment_token)
            ):
                raise WorkerProtocolError("serve did not acknowledge the requested result")
            assert frame_epoch is not None
            _bind_epoch(connection, frame_epoch)
            if expected_stop is None:
                self.execution.on_result_accepted(reply.result)
            # A late, correctly correlated A cannot retire an in-progress stop.
            expected_result = None

        async def stop(call: StopExecution) -> None:
            nonlocal expected_stop, pending_tool
            if call.message_id != stop_call_id(call.arguments):
                raise WorkerProtocolError("stop invocation identity does not match its fence")
            assert frame_epoch is not None
            _bind_epoch(connection, frame_epoch)
            await stop_observing()
            receipt = await self.execution.stop(call.arguments)
            pending_tool = None
            identity = stop_receipt_id(receipt)
            expected_stop = AcceptStop(
                **call_header(accept_stop_id(identity)),
                method="server.accept_stop",
                arguments=AcceptStopArguments(
                    **call.arguments.model_dump(),
                    receipt_id=identity,
                ),
            )
            await connection.send(
                WorkerFrame(
                    **frame_header(connection.epoch),
                    control=None,
                    payload=[
                        StopResult(
                            **result_header(call.message_id, identity),
                            method="worker.stop",
                            result=receipt,
                        ),
                        expected_stop,
                    ],
                )
            )

        async def accept_stop(reply: StopAccepted) -> None:
            nonlocal expected_stop, expected_result
            if (
                expected_stop is None
                or reply.reply_to != expected_stop.message_id
                or reply.result.model_dump()
                != expected_stop.arguments.model_dump(exclude={"receipt_id"})
            ):
                raise WorkerProtocolError("stop was not acknowledged")
            self.execution.on_stop_accepted(reply.result)
            expected_stop = expected_result = None

        async def finish_tool(reply: FlowToolResponse) -> None:
            nonlocal pending_tool, next_tool
            if pending_tool is None:
                raise WorkerProtocolError("Flow tool result has no pending invocation")
            self.execution.finish_tool_call(pending_tool, reply)
            pending_tool = None
            if self.execution.lease() is not None:
                next_tool = asyncio.create_task(self.execution.next_tool_call())

        dispatcher = Dispatcher[ServerPayload]()
        dispatcher.register("tool_call", "worker.execute", Assignment, execute)
        dispatcher.register("tool_call", "worker.stop", StopExecution, stop)
        dispatcher.register("tool_call", "worker.reject_recovery", RecoveryRejected, reject)
        dispatcher.register("tool_result", "server.accept_result", ResultAccepted, accept_result)
        dispatcher.register("tool_result", "server.accept_stop", StopAccepted, accept_stop)
        dispatcher.register("tool_result", "*", FlowToolResponse, finish_tool)
        try:
            while not self._stop.is_set():
                observers: set[
                    asyncio.Task[ServerMessage]
                    | asyncio.Task[None]
                    | asyncio.Task[TerminalPayload]
                    | asyncio.Task[PendingToolCall]
                ] = {incoming}
                observers.update(
                    task for task in (heartbeat_task, terminal, next_tool) if task is not None
                )
                completed, _ = await asyncio.wait(observers, return_when=asyncio.FIRST_COMPLETED)
                if heartbeat_task is not None and heartbeat_task in completed:
                    heartbeat_task.result()
                    raise WorkerProtocolError("heartbeat task stopped unexpectedly")
                if incoming in completed:
                    frame = incoming.result()
                    if isinstance(frame, ProtocolError):
                        raise WorkerProtocolError(
                            f"serve protocol error: {frame.code}: {frame.detail}"
                        )
                    frame_epoch = frame.connection_epoch
                    assert frame_epoch is not None
                    if connection.epoch is not None and frame_epoch != connection.epoch:
                        raise WorkerProtocolError("message belongs to another connection epoch")
                    if isinstance(frame, Registered):
                        if ready or expected_stop is not None:
                            raise WorkerProtocolError("unexpected registration decision")
                        _bind_epoch(connection, frame_epoch)
                        if frame.recovery is not None:
                            self.execution.state.validate_recovery(
                                frame.recovery.job_id,
                                frame.recovery.assignment_token,
                            )
                            if frame.recovery.action == "resume":
                                self.execution.resume(frame.recovery)
                            observe_execution()
                        ready = self.connected = True
                        heartbeat_task = asyncio.create_task(heartbeat(frame.heartbeat_interval))
                        logger.bind(
                            event="worker_connected", reason_code="ok", worker_id=self.worker_id
                        ).info("")
                    for item in frame.payload:
                        if frame.control is not None and connection.dispatch_item(item, frame):
                            continue
                        await dispatcher.dispatch(item)
                    incoming = asyncio.create_task(connection.receive())
                if terminal is not None and terminal in completed:
                    result = terminal.result()
                    await stop_observing()
                    expected_result = _accept_result(result)
                    await connection.send(
                        WorkerFrame(
                            **frame_header(connection.epoch),
                            control=None,
                            payload=[
                                ExecuteResult(
                                    **result_header(
                                        execution_call_id(result.job_id, result.assignment_token),
                                        terminal_id(result),
                                    ),
                                    method="worker.execute",
                                    job_id=result.job_id,
                                    assignment_token=result.assignment_token,
                                    result=result.result if isinstance(result, Succeeded) else None,
                                    error=None if isinstance(result, Succeeded) else result.error,
                                ),
                                expected_result,
                            ],
                        )
                    )
                if next_tool is not None and next_tool in completed:
                    call = next_tool.result()
                    next_tool = None

                    def is_current(call: PendingToolCall = call) -> bool:
                        return not call.future.done()

                    if await connection.send(
                        WorkerFrame(
                            **frame_header(connection.epoch),
                            control=None,
                            payload=[call.request],
                        ),
                        is_current=is_current,
                    ):
                        pending_tool = call
                    else:
                        next_tool = asyncio.create_task(self.execution.next_tool_call())
        finally:
            tasks = [
                task for task in (incoming, heartbeat_task, terminal, next_tool) if task is not None
            ]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self) -> None:
        """Close communication only; the runtime separately shuts down its execution owner."""
        self.connected = False
        self._stop.set()
        self._peers.seal()
        if self._connection_task is not None:
            self._connection_task.cancel()
