"""Worker connection and execution-state protocol, borrowing process-lifetime execution."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

from loguru import logger

from app.communication.dispatch import Dispatcher
from app.communication.session import Ready, Session
from app.contracts.protocol import (
    MAX_PAYLOAD_ITEMS,
    AcceptResultArguments,
    AcceptResultRequest,
    AcceptResultResponse,
    AcceptStop,
    AcceptStopArguments,
    Assignment,
    CancelJob,
    ExecuteResult,
    ExecutionIdentity,
    ExecutionStopped,
    Heartbeat,
    Inventory,
    InventoryPage,
    PayloadValidationError,
    ProtocolError,
    RecoveryRejected,
    Register,
    Registered,
    RepositoryDemand,
    ServerMessageUnion,
    Started,
    StopAccepted,
    StopExecution,
    StopResult,
    Succeeded,
    TerminalPayload,
    ToolCall,
    ToolResult,
    ValidationErrorResult,
    WorkerMessage,
    WorkerMessageUnion,
    accept_result_id,
    accept_stop_id,
    call_header,
    execution_call_id,
    message_header,
    result_header,
    stop_call_id,
    stop_receipt_id,
    terminal_id,
)
from app.contracts.validation import RejectedMessage
from app.domain.inference_models import InferenceCapability
from app.inference.transport import WorkerInferenceCallbacks
from app.worker.execution.lifecycle import WorkerProtocolError

if TYPE_CHECKING:
    from app.worker.runtime import WorkerRuntime


def control_ack(ack_id: str) -> WorkerMessageUnion:
    return WorkerMessage(
        **(message_header(None) | {"ack": True, "ack_id": ack_id}),
        control=None,
        payload=[],
    )


def _accept_result(terminal: TerminalPayload) -> AcceptResultRequest:
    identity = terminal_id(terminal)
    return AcceptResultRequest(
        **call_header(accept_result_id(identity)),
        method="server.accept_result",
        arguments=AcceptResultArguments(
            job_id=terminal.job_id,
            assignment_token=terminal.assignment_token,
            result_id=identity,
        ),
    )


class WorkerStateCallbacks:
    """One connection's handshake and pending correlations; never owns the executor task."""

    def __init__(
        self,
        *,
        worker_id: str,
        session: Session[ServerMessageUnion, WorkerMessageUnion],
        runtime: WorkerRuntime,
        on_ready: Callable[[bool], None],
        inference: InferenceCapability | None,
        inference_callbacks: WorkerInferenceCallbacks,
    ) -> None:
        self.worker_id, self.session, self.runtime = worker_id, session, runtime
        self.on_ready = on_ready
        self.inference = inference
        self.inference_callbacks = inference_callbacks
        self.register: Register | None = None
        self.ready = False
        self.expected_result: dict[int, AcceptResultRequest] = {}
        self.expected_stop: dict[int, AcceptStop] = {}
        self._started: dict[int, Started] = {}
        self._terminal: dict[int, ExecuteResult] = {}
        self._stopped: dict[int, StopResult] = {}
        self._stops: dict[int, tuple[StopExecution, asyncio.Task[ExecutionStopped]]] = {}
        self._pages: deque[InventoryPage] = deque()
        self._authorized: set[int] = set()
        self._observing = False
        self._heartbeat_started = False
        self._inventory_dirty = False
        self._unmatched_error_logged = False
        self._recovery_required = False

    def bind(self, routes: Dispatcher[ServerMessageUnion]) -> None:
        self.routes = routes
        routes.register("worker.execute", Assignment, self.on_execute)
        routes.register("worker.stop", StopExecution, self.on_stop)
        routes.register("worker.reject_recovery", RecoveryRejected, self.on_reject)
        routes.register("worker.provision", RepositoryDemand, self.on_provision)
        routes.configure(
            opened=self.on_opened,
            message=self.on_message,
            unmatched_reply=self.on_unmatched_reply,
            validation_error=self.on_error,
            before_item=self.before_item,
            message_done=self.on_message_done,
            closed=self.on_closed,
            unrouted=self.on_unrouted,
            rejected=self.on_rejected,
        )

    async def on_unmatched_reply(self, reply: ToolResult, message: ServerMessageUnion) -> None:
        raise WorkerProtocolError("reply has no matching invocation")

    async def on_unrouted(self, item: ToolCall, message: ServerMessageUnion) -> None:
        raise WorkerProtocolError("unregistered message route")

    async def before_item(self, item: ToolCall | ToolResult, message: ServerMessageUnion) -> None:
        if self._recovery_required and not isinstance(item, ValidationErrorResult):
            raise WorkerProtocolError("critical protocol payload rejected; recovery required")

    def on_message_done(self) -> None:
        if self._recovery_required:
            raise WorkerProtocolError("critical protocol payload rejected; recovery required")

    async def on_opened(self) -> None:
        for flow_id, execution in list(self.runtime.executions.items()):
            execution.state.reconcile_acknowledgement(self.runtime.workspaces)
            if execution.state.load() is None:
                self.runtime.executions.pop(flow_id)
        inventory = self.runtime.workspaces.inventory()
        recoveries = []
        for execution in self.runtime.executions.values():
            recovery = execution.recovery()
            record = execution.state.load()
            if recovery is not None and record is not None and not record.acknowledged:
                recoveries.append(recovery)
        # All concrete targets must be registered before any recovery can authorize work.
        # Inventory and journal ordering are independent and may span several pages.
        for start in range(0, len(inventory), MAX_PAYLOAD_ITEMS):
            self._pages.append(InventoryPage(
                inventory=inventory[start:start + MAX_PAYLOAD_ITEMS],
                recoveries=[], inventory_complete=False,
            ))
        # The receiver processes a page's inventory before its recovery descriptors.
        self._pages[-1].recoveries = recoveries[:MAX_PAYLOAD_ITEMS]
        for start in range(MAX_PAYLOAD_ITEMS, len(recoveries), MAX_PAYLOAD_ITEMS):
            self._pages.append(InventoryPage(
                inventory=[],
                recoveries=recoveries[start:start + MAX_PAYLOAD_ITEMS],
                inventory_complete=False,
            ))
        self._pages[-1].inventory_complete = True
        default = next(item for item in inventory if item.repository is None)
        self.register = Register(
            **message_header(None),
            control="register",
            worker_id=self.worker_id,
            workspace=default.workspace,
            repo_workspace_limit=self.runtime.settings.worker_repo_workspace_limit,
            execute=True,
            inference=self.inference,
            payload=[],
            **self._pages.popleft().model_dump(),
        )
        await self.session.send(self.register)
        self.session.start()

    def _bind_epoch(self, epoch: str) -> None:
        try:
            self.session.bind_epoch(epoch)
        except ValueError as error:
            raise WorkerProtocolError(str(error)) from error

    async def on_message(self, message: ServerMessageUnion) -> None:
        if isinstance(message, ProtocolError):
            raise WorkerProtocolError(f"serve protocol error: {message.code}: {message.detail}")
        epoch = message.connection_epoch
        assert epoch is not None
        if self.session.epoch is not None and epoch != self.session.epoch:
            if message.control is None and all(
                isinstance(i, ValidationErrorResult) for i in message.payload
            ):
                return  # The error hook checks the epoch before using correlations.
            raise WorkerProtocolError("message belongs to another connection epoch")
        if isinstance(message, CancelJob):
            if not self.ready:
                raise WorkerProtocolError("job cancellation requires completed registration")
            self.inference_callbacks.cancel_job(message)
        if isinstance(message, Registered):
            if self.ready:
                raise WorkerProtocolError("unexpected registration decision")
            self._bind_epoch(epoch)
            for recovery in message.recoveries:
                execution = self.runtime.execution_for(recovery.job_id, recovery.assignment_token)
                execution.state.validate_recovery(recovery.job_id, recovery.assignment_token)
                if recovery.action == "resume":
                    execution.resume(recovery)
                self._authorized.add(recovery.job_id)
            if not self._heartbeat_started:
                self.session.periodic(message.heartbeat_interval, self.heartbeat)
                self._heartbeat_started = True
                self._observe_execution()
            await self.flush_execution()
            if message.ready:
                if self._pages:
                    raise WorkerProtocolError("registration completed before all inventory pages")
                self.ready = True
                self.on_ready(True)
                if self._inventory_dirty:
                    await self.report_inventory()
                logger.bind(event="worker_connected", reason_code="ok", worker_id=self.worker_id).info("")
            else:
                if not self._pages:
                    raise WorkerProtocolError("unexpected inventory page request")
                await self.session.send(Inventory(
                    **message_header(epoch), control="inventory", payload=[],
                    **self._pages.popleft().model_dump(),
                ))

    def on_closed(self) -> None:
        self.ready = False
        self.on_ready(False)

    def _observe_execution(self) -> None:
        if not self._observing:
            self.session.watch("executions", self.runtime.wait_changed, self.on_changed)
            self._observing = True

    async def on_changed(self, _: None) -> None:
        await self.session.unwatch("executions")
        self._observing = False
        await self.flush_execution()
        self._observe_execution()

    async def flush_execution(self) -> None:
        """Deliver only completed work; a slow stop never blocks another workspace's driver."""
        for job_id, (call, task) in list(self._stops.items()):
            if task.done() and job_id not in self.expected_stop:
                receipt = task.result()
                identity = stop_receipt_id(receipt)
                expected = AcceptStop(
                    **call_header(accept_stop_id(identity)), method="server.accept_stop",
                    arguments=AcceptStopArguments(**call.arguments.model_dump(), receipt_id=identity),
                )
                self.expected_stop[job_id] = expected
                self.routes.expect_reply(expected, StopAccepted, self.on_accept_stop)
                stopped = StopResult(
                    **result_header(call.message_id, identity), method="worker.stop", result=receipt,
                )
                self._stopped[job_id] = stopped
                await self.session.send(WorkerMessage(
                    **message_header(self.session.epoch), control=None, payload=[stopped, expected],
                ))
        for execution in self.runtime.executions.values():
            terminal = execution.result_for_registration()
            if (terminal is not None and terminal.job_id in self._authorized
                    and terminal.job_id not in self.expected_result
                    and terminal.job_id not in self._stops):
                await self.send_terminal(terminal)

    def heartbeat(self) -> list[WorkerMessageUnion]:
        messages: list[WorkerMessageUnion] = []
        for execution in self.runtime.executions.values():
            assignment = execution.lease()
            if assignment is not None and assignment.job_id in self._authorized:
                messages.append(Heartbeat(
                    **message_header(self.session.epoch), control="heartbeat", payload=[],
                    job_id=assignment.job_id, assignment_token=assignment.assignment_token,
                ))
        inference = self.inference_callbacks.heartbeat()
        if inference is not None:
            messages.append(inference)
        return messages

    async def on_execute(self, call: Assignment, message: ServerMessageUnion) -> None:
        arguments = call.arguments
        if not self.ready or call.message_id != execution_call_id(
            arguments.job_id, arguments.assignment_token
        ):
            raise WorkerProtocolError("assignment has no authorized execution identity")
        execution = self.runtime.receive(arguments)
        record = execution.state.load()
        assert record is not None
        if record.state != "received":
            if not record.acknowledged:
                self._authorized.add(arguments.job_id)
                await self.flush_execution()
            return  # Duplicate delivery never starts a second agent or rewrites the journal.
        started = Started(
            **call_header(),
            method="server.execution_started",
            arguments=ExecutionIdentity(
                job_id=arguments.job_id, assignment_token=arguments.assignment_token
            ),
        )
        self._started[arguments.job_id] = started
        try:
            if not await self.session.send(
                WorkerMessage(
                    **message_header(self.session.epoch),
                    control=None,
                    payload=[started],
                )
            ):
                raise WorkerProtocolError("Started was not written")
        except BaseException:
            self._started.pop(arguments.job_id, None)
            raise
        self._authorized.add(arguments.job_id)
        execution.start()
        self._observe_execution()

    async def on_reject(self, call: RecoveryRejected, message: ServerMessageUnion) -> None:
        assert self.register is not None
        if self.ready or call.arguments.register_message_id != self.register.message_id:
            raise WorkerProtocolError("recovery rejection belongs to another registration")
        execution = self.runtime.execution_for(call.arguments.job_id)
        await execution.reject_recovery(call.arguments.job_id)
        expected = self.expected_result.pop(call.arguments.job_id, None)
        if expected is not None:
            self.routes.forget_reply(expected.message_id)

    async def on_accept_result(self, reply: AcceptResultResponse, message: ServerMessageUnion) -> None:
        expected = self.expected_result.get(reply.result.job_id)
        if (
            expected is None
            or reply.reply_to != expected.message_id
            or (reply.result.job_id, reply.result.assignment_token)
            != (expected.arguments.job_id, expected.arguments.assignment_token)
        ):
            raise WorkerProtocolError("serve did not acknowledge the requested result")
        assert message.connection_epoch is not None
        self._bind_epoch(message.connection_epoch)
        execution = self.runtime.execution_for(reply.result.job_id, reply.result.assignment_token)
        record = execution.state.load()
        assert record is not None
        if reply.result.job_id not in self._stops:
            execution.on_result_accepted(reply.result)
            execution.state.reconcile_acknowledgement(self.runtime.workspaces)
            if execution.state.load() is None:
                self.runtime.executions.pop(record.flow_id)
        self.routes.forget_reply(expected.message_id)
        self.expected_result.pop(record.job_id, None)
        self._terminal.pop(record.job_id, None)
        self._started.pop(record.job_id, None)
        self._authorized.discard(record.job_id)
        await self.report_inventory()

    async def on_stop(self, call: StopExecution, message: ServerMessageUnion) -> None:
        if call.message_id != stop_call_id(call.arguments):
            raise WorkerProtocolError("stop invocation identity does not match its fence")
        assert message.connection_epoch is not None
        self._bind_epoch(message.connection_epoch)
        execution = self.runtime.execution_for(call.arguments.job_id, call.arguments.assignment_token)
        previous = self._stops.get(call.arguments.job_id)
        if previous is not None:
            if previous[0].arguments != call.arguments:
                raise WorkerProtocolError("stop conflicts with existing operation")
            return
        task = asyncio.create_task(execution.stop(call.arguments))
        self._stops[call.arguments.job_id] = call, task

        def completed(task: asyncio.Task[ExecutionStopped]) -> None:
            if not task.cancelled():
                task.exception()
            self.runtime.notify()

        task.add_done_callback(completed)
        self._observe_execution()

    async def on_accept_stop(self, reply: StopAccepted, message: ServerMessageUnion) -> None:
        expected = self.expected_stop.get(reply.result.job_id)
        if (
            expected is None
            or reply.reply_to != expected.message_id
            or reply.result.model_dump() != expected.arguments.model_dump(exclude={"receipt_id"})
        ):
            raise WorkerProtocolError("stop was not acknowledged")
        execution = self.runtime.execution_for(reply.result.job_id, reply.result.assignment_token)
        record = execution.state.load()
        assert record is not None
        execution.on_stop_accepted(reply.result)
        execution.state.reconcile_acknowledgement(self.runtime.workspaces)
        self.routes.forget_reply(expected.message_id)
        result = self.expected_result.pop(record.job_id, None)
        if result is not None:
            self.routes.forget_reply(result.message_id)
        self.expected_stop.pop(record.job_id, None)
        self._started.pop(record.job_id, None)
        self._terminal.pop(record.job_id, None)
        self._stopped.pop(record.job_id, None)
        self._stops.pop(record.job_id, None)
        self._authorized.discard(record.job_id)
        await self.report_inventory()

    async def on_provision(self, call: RepositoryDemand, message: ServerMessageUnion) -> None:
        if not self.ready:
            raise WorkerProtocolError("inventory demand requires completed registration")
        await self.runtime.workspaces.provision(call.repository)
        await self.report_inventory()

    async def report_inventory(self) -> None:
        if not self.ready:
            self._inventory_dirty = True
            return
        self._inventory_dirty = False
        inventory = self.runtime.workspaces.inventory()
        for start in range(0, len(inventory), MAX_PAYLOAD_ITEMS):
            await self.session.send(Inventory(
                **message_header(self.session.epoch), control="inventory", payload=[],
                inventory=inventory[start:start + MAX_PAYLOAD_ITEMS], recoveries=[], inventory_complete=True,
            ))

    async def aclose(self) -> None:
        observers = [task for _, task in self._stops.values()]
        for task in observers:
            task.cancel()
        await asyncio.gather(*observers, return_exceptions=True)

    async def send_terminal(self, terminal: TerminalPayload) -> None:
        previous = self.expected_result.get(terminal.job_id)
        if previous is not None:
            self.routes.forget_reply(previous.message_id)
        expected = _accept_result(terminal)
        self.expected_result[terminal.job_id] = expected
        self.routes.expect_reply(expected, AcceptResultResponse, self.on_accept_result)
        result = ExecuteResult(
            **result_header(
                execution_call_id(terminal.job_id, terminal.assignment_token), terminal_id(terminal)
            ),
            method="worker.execute",
            job_id=terminal.job_id,
            assignment_token=terminal.assignment_token,
            result=terminal.result if isinstance(terminal, Succeeded) else None,
            error=None if isinstance(terminal, Succeeded) else terminal.error,
        )
        self._terminal[terminal.job_id] = result
        try:
            await self.session.send(
                WorkerMessage(
                    **message_header(self.session.epoch),
                    control=None,
                    payload=[result, expected],
                )
            )
        except BaseException:
            self.routes.forget_reply(expected.message_id)
            self.expected_result.pop(terminal.job_id, None)
            self._terminal.pop(terminal.job_id, None)
            raise

    async def on_error(self, error: ValidationErrorResult, message: ServerMessageUnion) -> None:
        if message.connection_epoch == self.session.epoch:
            sent_items: list[ToolCall | ToolResult] = [
                *self._started.values(),
                *self._terminal.values(),
                *self._stopped.values(),
                *self.expected_result.values(),
                *self.expected_stop.values(),
            ]
            for sent in sent_items:
                if (error.method, error.reply_to) == (
                    sent.method,
                    sent.message_id,
                ):
                    # Finish ordinary failed calls in this same error batch before recovery.
                    self._recovery_required = True
                    return
            if self.routes.reject_reply(error, "message payload failed validation"):
                return
        if not self._unmatched_error_logged:
            logger.warning("ignored unmatched validation error on worker connection")
            self._unmatched_error_logged = True

    def on_rejected(self, message: RejectedMessage) -> Ready:
        """Reject the whole message without recording Assignment or granting control effects."""
        if (
            not self.ready
            or message.connection_epoch != self.session.epoch
            or message.control is not None
        ):
            raise WorkerProtocolError("invalid payload has no established connection authority")
        critical = False
        replies: list[ToolResult] = []
        for item in message.payload:
            if isinstance(item, ToolResult):
                model = self.routes.reply_model(item)
                if model is None:
                    raise WorkerProtocolError("invalid reply has no pending invocation")
                if model in (AcceptResultResponse, StopAccepted):
                    critical = True
                else:
                    replies.append(item)
            elif item.method in {"worker.stop", "worker.reject_recovery"}:
                critical = True
        # All headers must be authorized before any independent caller is rejected.
        for item in replies:
            self.routes.reject_reply(item, "invalid tool response; remote effect outcome unknown")

        async def respond() -> None:
            await self.session.send(
                WorkerMessage(
                    **message_header(self.session.epoch),
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
