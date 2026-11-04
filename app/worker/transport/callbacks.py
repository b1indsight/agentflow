"""Worker business callbacks for one Session, borrowing process-lifetime execution."""

import asyncio
from collections.abc import Callable

from loguru import logger

from app.communication.dispatch import Dispatcher
from app.communication.session import Ready, Responder, Session
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
    PayloadValidationError,
    ProtocolError,
    RecoveryRejected,
    Register,
    Registered,
    ResultAccepted,
    ServerMessage,
    Started,
    StopAccepted,
    StopExecution,
    StopResult,
    Succeeded,
    TerminalPayload,
    ToolCall,
    ToolResult,
    ValidationErrorResult,
    WorkerFrame,
    WorkerMessage,
    accept_result_id,
    accept_stop_id,
    call_header,
    execution_call_id,
    frame_header,
    result_header,
    stop_call_id,
    stop_receipt_id,
    terminal_id,
)
from app.contracts.validation import RejectedMessage
from app.worker.execution.lifecycle import PendingToolCall, WorkerExecution, WorkerProtocolError


def history_responder(read: Callable[[HistoryQuery], HistoryPage]) -> Responder[WorkerMessage]:
    """Keep independent history reads bounded by Session, outside the business driver."""

    def matches(item: ToolCall | ToolResult) -> bool:
        return isinstance(item, ToolCall) and item.method == "worker.history"

    def busy(item: ToolCall | ToolResult, epoch: str) -> WorkerMessage:
        return WorkerFrame(
            **frame_header(epoch),
            control=None,
            payload=[
                ConversationResult(
                    **result_header(item.message_id),
                    method="worker.history",
                    error="query_busy",
                )
            ],
        )

    async def handle(item: ToolCall | ToolResult, epoch: str) -> WorkerMessage:
        assert isinstance(item, ConversationQuery)
        try:
            query = HistoryQuery(**item.arguments.model_dump())
            page = await asyncio.to_thread(read, query)
            result = ConversationResult(
                **result_header(item.message_id),
                method="worker.history",
                page=page,
            )
        except ConversationFailure as error:
            result = ConversationResult(
                **result_header(item.message_id),
                method="worker.history",
                error=error.code,
            )
        return WorkerFrame(**frame_header(epoch), control=None, payload=[result])

    return Responder(matches=matches, handle=handle, busy=busy)


def control_ack(ack_id: str) -> WorkerMessage:
    return WorkerFrame(
        **(frame_header(None) | {"ack": True, "ack_id": ack_id}),
        control=None,
        payload=[],
    )


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


class WorkerCallbacks:
    """One connection's handshake and pending correlations; never owns the executor task."""

    def __init__(
        self,
        *,
        worker_id: str,
        session: Session[ServerMessage, WorkerMessage],
        execution: WorkerExecution,
        on_ready: Callable[[bool], None],
    ) -> None:
        self.worker_id, self.session, self.execution = worker_id, session, execution
        self.on_ready = on_ready
        self.register: Register | None = None
        self.ready = False
        self.expected_result: AcceptResult | None = None
        self.expected_stop: AcceptStop | None = None
        self.pending_tool: PendingToolCall | None = None
        self._started: Started | None = None
        self._terminal: ExecuteResult | None = None
        self._stopped: StopResult | None = None
        self._unmatched_error_logged = False
        self._recovery_required = False

    def bind(self, routes: Dispatcher[ServerMessage]) -> None:
        routes.register("worker.execute", Assignment, self.on_execute)
        routes.register("worker.stop", StopExecution, self.on_stop)
        routes.register("worker.reject_recovery", RecoveryRejected, self.on_reject)
        routes.register("server.accept_result", ResultAccepted, self.on_accept_result)
        routes.register("server.accept_stop", StopAccepted, self.on_accept_stop)
        routes.register("server.execution_started", ValidationErrorResult, self.on_error)
        routes.register("flow.open_pr", FlowToolResponse, self.on_tool)
        routes.configure(
            opened=self.on_opened,
            frame=self.on_frame,
            before_item=self.before_item,
            frame_done=self.on_frame_done,
            closed=self.on_closed,
            unrouted=self.on_unrouted,
            rejected=self.on_rejected,
        )

    async def before_item(self, item: ToolCall | ToolResult, message: ServerMessage) -> None:
        if self._recovery_required and not isinstance(item, ValidationErrorResult):
            raise WorkerProtocolError("critical protocol payload rejected; recovery required")

    def on_frame_done(self) -> None:
        if self._recovery_required:
            raise WorkerProtocolError("critical protocol payload rejected; recovery required")

    async def on_opened(self) -> None:
        recovery = self.execution.recovery()
        terminal = self.execution.result_for_registration()
        self.expected_result = _accept_result(terminal) if terminal is not None else None
        self.register = Register(
            **frame_header(None),
            control="register",
            worker_id=self.worker_id,
            recovery=recovery,
            payload=[self.expected_result] if self.expected_result is not None else [],
        )
        await self.session.send(self.register)
        self.session.start()

    def _bind_epoch(self, epoch: str) -> None:
        try:
            self.session.bind_epoch(epoch)
        except ValueError as error:
            raise WorkerProtocolError(str(error)) from error

    async def on_frame(self, message: ServerMessage) -> None:
        if isinstance(message, ProtocolError):
            raise WorkerProtocolError(f"serve protocol error: {message.code}: {message.detail}")
        epoch = message.connection_epoch
        assert epoch is not None
        if self.session.epoch is not None and epoch != self.session.epoch:
            if message.control is None and all(
                isinstance(i, ValidationErrorResult) for i in message.payload
            ):
                return  # Individual error callbacks check the epoch before using correlations.
            raise WorkerProtocolError("message belongs to another connection epoch")
        if isinstance(message, Registered):
            if self.ready or self.expected_stop is not None:
                raise WorkerProtocolError("unexpected registration decision")
            self._bind_epoch(epoch)
            if message.recovery is not None:
                self.execution.state.validate_recovery(
                    message.recovery.job_id,
                    message.recovery.assignment_token,
                )
                if message.recovery.action == "resume":
                    self.execution.resume(message.recovery)
                self._observe_execution()
            self.ready = True
            self.on_ready(True)
            self.session.periodic(message.heartbeat_interval, self.heartbeat)
            logger.bind(event="worker_connected", reason_code="ok", worker_id=self.worker_id).info(
                ""
            )

    def on_closed(self) -> None:
        self.on_ready(False)

    def _observe_execution(self) -> None:
        self.session.watch("terminal", self.execution.wait_terminal, self.send_terminal)
        self.session.watch("tool", self.execution.next_tool_call, self.send_tool)

    def heartbeat(self) -> WorkerMessage | None:
        assignment = self.execution.lease()
        if assignment is None:
            return None
        return Heartbeat(
            **frame_header(self.session.epoch),
            control="heartbeat",
            payload=[],
            job_id=assignment.job_id,
            assignment_token=assignment.assignment_token,
        )

    async def on_execute(
        self, call: Assignment | ValidationErrorResult, message: ServerMessage
    ) -> None:
        if isinstance(call, ValidationErrorResult):
            await self.on_error(call, message)
            return
        arguments = call.arguments
        if not self.ready or call.message_id != execution_call_id(
            arguments.job_id, arguments.assignment_token
        ):
            raise WorkerProtocolError("assignment has no authorized execution identity")
        self.execution.receive(arguments)
        self._started = Started(
            **call_header(),
            method="server.execution_started",
            arguments=ExecutionIdentity(
                job_id=arguments.job_id, assignment_token=arguments.assignment_token
            ),
        )
        try:
            if not await self.session.send(
                WorkerFrame(
                    **frame_header(self.session.epoch),
                    control=None,
                    payload=[self._started],
                )
            ):
                raise WorkerProtocolError("Started was not written")
        except BaseException:
            self._started = None
            raise
        self.execution.start()
        self._observe_execution()

    async def on_reject(
        self, call: RecoveryRejected | ValidationErrorResult, message: ServerMessage
    ) -> None:
        if isinstance(call, ValidationErrorResult):
            await self.on_error(call, message)
            return
        assert self.register is not None
        if self.ready or call.arguments.register_message_id != self.register.message_id:
            raise WorkerProtocolError("recovery rejection belongs to another registration")
        await self.execution.reject_recovery(call.arguments.job_id)
        self.expected_result = None

    async def on_accept_result(
        self, reply: ResultAccepted | ValidationErrorResult, message: ServerMessage
    ) -> None:
        if isinstance(reply, ValidationErrorResult):
            await self.on_error(reply, message)
            return
        expected = self.expected_result
        if (
            expected is None
            or reply.reply_to != expected.message_id
            or (reply.result.job_id, reply.result.assignment_token)
            != (expected.arguments.job_id, expected.arguments.assignment_token)
        ):
            raise WorkerProtocolError("serve did not acknowledge the requested result")
        assert message.connection_epoch is not None
        self._bind_epoch(message.connection_epoch)
        if self.expected_stop is None:
            self.execution.on_result_accepted(reply.result)
        self.expected_result = self._terminal = self._started = None

    async def on_stop(
        self, call: StopExecution | ValidationErrorResult, message: ServerMessage
    ) -> None:
        if isinstance(call, ValidationErrorResult):
            await self.on_error(call, message)
            return
        if call.message_id != stop_call_id(call.arguments):
            raise WorkerProtocolError("stop invocation identity does not match its fence")
        assert message.connection_epoch is not None
        self._bind_epoch(message.connection_epoch)
        await self.session.unwatch("terminal", "tool")
        receipt = await self.execution.stop(call.arguments)
        self.pending_tool = None
        identity = stop_receipt_id(receipt)
        self.expected_stop = AcceptStop(
            **call_header(accept_stop_id(identity)),
            method="server.accept_stop",
            arguments=AcceptStopArguments(**call.arguments.model_dump(), receipt_id=identity),
        )
        self._stopped = StopResult(
            **result_header(call.message_id, identity),
            method="worker.stop",
            result=receipt,
        )
        try:
            await self.session.send(
                WorkerFrame(
                    **frame_header(self.session.epoch),
                    control=None,
                    payload=[self._stopped, self.expected_stop],
                )
            )
        except BaseException:
            self.expected_stop = self._stopped = None
            raise

    async def on_accept_stop(
        self, reply: StopAccepted | ValidationErrorResult, message: ServerMessage
    ) -> None:
        if isinstance(reply, ValidationErrorResult):
            await self.on_error(reply, message)
            return
        expected = self.expected_stop
        if (
            expected is None
            or reply.reply_to != expected.message_id
            or reply.result.model_dump() != expected.arguments.model_dump(exclude={"receipt_id"})
        ):
            raise WorkerProtocolError("stop was not acknowledged")
        self.execution.on_stop_accepted(reply.result)
        self.expected_stop = self.expected_result = self._started = self._terminal = (
            self._stopped
        ) = None

    async def on_tool(
        self, reply: FlowToolResponse | ValidationErrorResult, message: ServerMessage
    ) -> None:
        if isinstance(reply, ValidationErrorResult):
            await self.on_error(reply, message)
            return
        if self.pending_tool is None:
            raise WorkerProtocolError("Flow tool result has no pending invocation")
        self.execution.finish_tool_call(self.pending_tool, reply)
        self._tool_finished()

    def _tool_finished(self) -> None:
        self.pending_tool = None
        if self.execution.lease() is not None:
            self.session.watch("tool", self.execution.next_tool_call, self.send_tool)

    async def on_unrouted(self, item: ToolCall | ToolResult, message: ServerMessage) -> None:
        if isinstance(item, (FlowToolResponse, ValidationErrorResult)):
            await self.on_tool(item, message)
            return
        raise WorkerProtocolError("unregistered message route")

    async def send_terminal(self, terminal: TerminalPayload) -> None:
        await self.session.unwatch("terminal", "tool")
        self.expected_result = _accept_result(terminal)
        self._terminal = ExecuteResult(
            **result_header(
                execution_call_id(terminal.job_id, terminal.assignment_token), terminal_id(terminal)
            ),
            method="worker.execute",
            job_id=terminal.job_id,
            assignment_token=terminal.assignment_token,
            result=terminal.result if isinstance(terminal, Succeeded) else None,
            error=None if isinstance(terminal, Succeeded) else terminal.error,
        )
        try:
            await self.session.send(
                WorkerFrame(
                    **frame_header(self.session.epoch),
                    control=None,
                    payload=[self._terminal, self.expected_result],
                )
            )
        except BaseException:
            self.expected_result = self._terminal = None
            raise

    async def send_tool(self, call: PendingToolCall) -> None:
        self.pending_tool = call
        try:
            written = await self.session.send(
                WorkerFrame(
                    **frame_header(self.session.epoch), control=None, payload=[call.request]
                ),
                is_current=lambda: not call.future.done(),
            )
        except BaseException:
            self.pending_tool = None
            raise
        if not written:
            self._tool_finished()

    async def on_error(self, error: ValidationErrorResult, message: ServerMessage) -> None:
        if message.connection_epoch == self.session.epoch:
            call = self.pending_tool
            if call is not None and (error.method, error.reply_to) == (
                call.request.method,
                call.request.message_id,
            ):
                self.execution.reject_tool_call(call, "message payload failed validation")
                self._tool_finished()
                return
            for sent in (
                self._started,
                self._terminal,
                self._stopped,
                self.expected_result,
                self.expected_stop,
            ):
                if sent is not None and (error.method, error.reply_to) == (
                    sent.method,
                    sent.message_id,
                ):
                    # Finish ordinary failed calls in this same error batch before recovery.
                    self._recovery_required = True
                    return
        if not self._unmatched_error_logged:
            logger.warning("ignored unmatched validation error on worker connection")
            self._unmatched_error_logged = True

    def on_rejected(self, message: RejectedMessage) -> Ready:
        """Reject the whole msg without recording Assignment or granting control effects."""
        if (
            not self.ready
            or message.connection_epoch != self.session.epoch
            or message.control is not None
        ):
            raise WorkerProtocolError("invalid payload has no established connection authority")
        critical = False
        failed_tool = False
        for item in message.payload:
            if isinstance(item, ToolResult):
                call = self.pending_tool
                if call is not None and (item.method, item.reply_to) == (
                    call.request.method,
                    call.request.message_id,
                ):
                    failed_tool = True
                elif any(
                    sent is not None
                    and (item.method, item.reply_to) == (sent.method, sent.message_id)
                    for sent in (self.expected_result, self.expected_stop)
                ):
                    critical = True
                else:
                    raise WorkerProtocolError("invalid reply has no pending invocation")
            elif item.method in {"worker.stop", "worker.reject_recovery"}:
                critical = True
        if failed_tool:
            assert self.pending_tool is not None
            self.execution.reject_tool_call(
                self.pending_tool, "invalid tool response; remote effect outcome unknown"
            )
            self._tool_finished()

        async def respond() -> None:
            await self.session.send(
                WorkerFrame(
                    **frame_header(self.session.epoch),
                    control=None,
                    payload=[
                        ValidationErrorResult(
                            **result_header(item.message_id),
                            method=item.method,
                            error=PayloadValidationError(
                                code="validation_error", message="message payload failed validation"
                            ),
                        )
                        for item in message.payload
                    ],
                )
            )
            if critical:
                raise WorkerProtocolError("critical protocol payload rejected; recovery required")

        return respond
