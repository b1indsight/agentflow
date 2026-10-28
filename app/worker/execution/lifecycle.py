"""Own a worker's execution and tool waiters independently of replaceable connections."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from loguru import logger

from app.contracts.protocol import (
    Assignment,
    AssignmentSecret,
    ExecutionResult,
    ExecutionStopped,
    ExecutionTask,
    FlowToolErrorKind,
    FlowToolRequest,
    FlowToolResponse,
    RecoveryDescriptor,
    ResultAccepted,
    ResumeAssignment,
    StopAccepted,
    StopExecution,
    TerminalMessage,
)
from app.domain.delivery_models import DeliveryReceipt
from app.domain.models import VcsOutcome
from app.worker.state.execution import ExecutionState
from app.worker.transport.mcp import FlowToolCaller


class TaskExecutor(Protocol):
    """Execute an assignment and reclaim its workspace after a confirmed stop."""

    async def execute(
        self, task: ExecutionTask, secret: AssignmentSecret | None = None, *, tool_caller: FlowToolCaller
    ) -> ExecutionResult: ...

    async def reclaim_workspace(self) -> None: ...


class WorkerProtocolError(RuntimeError):
    """A worker message cannot be applied to the owned execution."""


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
class PendingToolCall:
    """A fixed request and its original caller, retained until response or execution cleanup."""

    request: FlowToolRequest
    future: asyncio.Future[DeliveryReceipt]


class WorkerExecution:
    """Supervise one execution slot for the worker process.

    A single communication consumer may borrow this owner. Join the previous consumer before
    replacing it; cancelling its observers never cancels the execution or an authorized stop.
    """

    def __init__(self, *, worker_id: str, executor: TaskExecutor, state: ExecutionState) -> None:
        self.worker_id = worker_id
        self.executor = executor
        self.state = state
        self._execution: asyncio.Task[ExecutionResult] | None = None
        self._cancel_requested = False
        self._tool_calls: deque[PendingToolCall] = deque()
        self._tool_ready = asyncio.Event()
        self._stop_task: asyncio.Task[ExecutionStopped] | None = None
        self._stop_request: StopExecution | None = None
        self._shutdown_task: asyncio.Task[None] | None = None

    def receive(self, assignment: Assignment) -> None:
        """Persist received before the connection sends Started; do not start any work."""
        if (self.state.assignment is not None or self._execution is not None
                or self._stop_task is not None or self._shutdown_task is not None):
            raise WorkerProtocolError("received a second assignment while busy")
        record = self.state.load()
        if record is not None and record.stopped is not None and not record.acknowledged:
            raise WorkerProtocolError("received a second assignment while busy")
        self.state.record_received(assignment)

    def start(self) -> None:
        """After Started was sent, persist started and launch without an intervening await."""
        assignment = self.state.assignment
        if (assignment is None or self._execution is not None
                or self._stop_task is not None or self._shutdown_task is not None):
            raise WorkerProtocolError("active assignment is missing or already executing")
        self.state.record_started()
        self._launch(assignment)

    def _launch(self, assignment: Assignment) -> None:
        self._cancel_requested = False
        with logger.contextualize(flow_id=assignment.flow_id, job_id=assignment.job_id,
                                  worker_id=self.worker_id, epoch=assignment.connection_epoch):
            logger.bind(event="job_started", reason_code="ok").info("")
            self._execution = asyncio.create_task(
                self.executor.execute(assignment.task, assignment.secret, tool_caller=self)
            )
            self._execution.add_done_callback(self._execution_finished)

    def _execution_finished(self, task: asyncio.Task[ExecutionResult]) -> None:
        # Retrieve every exception, including when the connection disappeared before completion.
        if not task.cancelled():
            task.exception()
        if task is not self._execution:
            return
        if self._cancel_requested:
            # Recovery rejection removes the assignment before joining. Its observer may have
            # disconnected, so retire the rejected task here, but retain stop/shutdown evidence.
            if self.state.assignment is None and self._stop_task is None and self._shutdown_task is None:
                self._execution = None
                self._discard_tool_calls()
            return
        try:
            self._collect_finished()
        except Exception:  # noqa: BLE001
            logger.exception("worker execution result could not be persisted; retaining it for recovery")

    def _collect_finished(self) -> None:
        execution = self._execution
        if execution is None or not execution.done() or self._cancel_requested:
            return
        assignment = self.state.assignment
        if assignment is None or execution.cancelled():
            raise WorkerProtocolError("cannot record an unfinished or missing executor")
        try:
            outcome: ExecutionResult | Exception = execution.result()
        except Exception as exc:  # noqa: BLE001
            logger.bind(event="job_failed", reason_code="execution_failed",
                        job_id=assignment.job_id, flow_id=assignment.flow_id,
                        worker_id=self.worker_id).opt(exception=exc).error("")
            outcome = exc
        self.state.complete(outcome)
        self._execution = None
        self._discard_tool_calls()

    def recovery(self) -> RecoveryDescriptor | None:
        """Report durable evidence only after retrying any unpersisted completed result."""
        self._collect_finished()
        return self.state.recovery(executor_active=self.lease() is not None)

    def resume(self, message: ResumeAssignment) -> None:
        """Rebind the original work, or restart only the existing frozen archive exception."""
        self.state.validate_recovery(message.job_id, message.assignment_token)
        self._collect_finished()
        record = self.state.load()
        if (self._cancel_requested or self._stop_task is not None or self._shutdown_task is not None
                or record is None or record.stopped is not None or record.acknowledged):
            raise WorkerProtocolError("serve resumed an assignment without a live executor")
        active = self.lease() is not None
        assignment = self.state.resume(message.connection_epoch, executor_active=active)
        if not active and record.terminal is None:
            self._launch(assignment)

    async def reject_recovery(self, job_id: int) -> None:
        """Cancel rejected work without letting observer cancellation interrupt its cleanup."""
        self.state.clear_rejected(job_id)
        execution = self._request_cancel()
        self._discard_tool_calls()
        if execution is not None:
            try:
                await asyncio.shield(execution)
            except asyncio.CancelledError:
                if not execution.cancelled():
                    raise
            finally:
                if execution.done():
                    self._execution_finished(execution)

    def lease(self) -> Assignment | None:
        """Return the assignment only while an uncancelled execution is still running."""
        if (self._execution is None or self._execution.done() or self._cancel_requested
                or self._stop_task is not None or self._shutdown_task is not None):
            return None
        return self.state.assignment

    async def wait_terminal(self, connection_epoch: str) -> TerminalMessage:
        """Observe completion without owning the task; return only a persisted terminal."""
        execution = self._execution
        if execution is not None:
            # wait does not propagate observer cancellation or the executor's exception.
            # The collector below retrieves failures and persists them through the same path.
            await asyncio.wait({execution})
        self._collect_finished()
        return self.state.replay(connection_epoch)

    def on_result_accepted(self, accepted: ResultAccepted) -> None:
        """Apply the server's result acceptance without retiring an active execution or stop."""
        self.state.validate_recovery(accepted.job_id, accepted.assignment_token)
        self._collect_finished()
        record = self.state.load()
        if (self._execution is not None or self._stop_task is not None
                or (record is not None and record.stopped is not None and not record.acknowledged)):
            raise WorkerProtocolError("result acknowledgement does not match terminal journal")
        self.state.acknowledge(accepted.job_id, accepted.state)

    async def call_tool(
        self, name: str, arguments: dict[str, object], *,
        call_id: str | None = None, vcs_outcome: VcsOutcome,
    ) -> DeliveryReceipt:
        """Queue one execution-local call and await its original future across connections."""
        assignment = self.lease()
        if assignment is None:
            raise WorkerProtocolError("Flow tool call has no active assignment")
        future: asyncio.Future[DeliveryReceipt] = asyncio.get_running_loop().create_future()
        request = FlowToolRequest(
            job_id=assignment.job_id, flow_id=assignment.flow_id,
            attempt_id=assignment.task.attempt_id, assignment_token=assignment.assignment_token,
            connection_epoch=assignment.connection_epoch, call_id=call_id or uuid4().hex,
            name=name, arguments=arguments, vcs_backend=vcs_outcome.backend,
            branch=vcs_outcome.branch, commit_sha=vcs_outcome.commit_sha,
            remote_sha=vcs_outcome.remote_sha,
        )
        self._tool_calls.append(PendingToolCall(request, future))
        self._tool_ready.set()
        return await future

    async def next_tool_call(self) -> PendingToolCall:
        """Borrow the queue head without removing it, so a disconnected observer loses nothing."""
        while True:
            while self._tool_calls:
                call = self._tool_calls[0]
                if not call.future.done():
                    return call
                self._tool_calls.popleft()
            self._tool_ready.clear()
            await self._tool_ready.wait()

    def finish_tool_call(self, call: PendingToolCall, response: FlowToolResponse) -> None:
        """Consume a matching reply, including a late reply to an already discarded call."""
        if response.call_id != call.request.call_id:
            raise WorkerProtocolError("Flow tool response does not match its request")
        if self._tool_calls and self._tool_calls[0] is call:
            self._tool_calls.popleft()
        elif not call.future.done():
            raise WorkerProtocolError("Flow tool response does not match its request")
        if call.future.done():
            return
        if response.error is not None:
            call.future.set_exception(FlowToolCallError(response))
        elif response.result is None:
            call.future.set_exception(WorkerProtocolError("Flow tool returned no result"))
        else:
            call.future.set_result(response.result)

    def _discard_tool_calls(self) -> None:
        while self._tool_calls:
            call = self._tool_calls.popleft()
            if not call.future.done():
                call.future.set_exception(WorkerProtocolError("assignment was cancelled"))

    def _request_cancel(self) -> asyncio.Task[ExecutionResult] | None:
        execution = self._execution
        if not self._cancel_requested:
            self._cancel_requested = True
            if execution is not None and not execution.done():
                execution.cancel()
        return execution

    async def stop(self, request: StopExecution) -> ExecutionStopped:
        """Join a fenced, owner-held stop operation; a connection only observes its outcome."""
        record = self.state.load()
        if record is None or (record.job_id, record.flow_id, record.assignment_token) != (
                request.job_id, request.flow_id, request.assignment_token):
            raise WorkerProtocolError("stop blocked: original session is missing")
        if ((record.stopped is not None and record.stopped.request_id != request.request_id)
                or (self._stop_request is not None
                    and self._stop_request.model_dump(exclude={"connection_epoch"})
                    != request.model_dump(exclude={"connection_epoch"}))):
            raise WorkerProtocolError("stop request conflicts with durable receipt")
        if self._shutdown_task is not None:
            raise WorkerProtocolError("stop blocked: worker is shutting down")
        if self._stop_task is None or (self._stop_task.done() and self._stop_task.exception() is not None):
            self._stop_request = request
            self._stop_task = asyncio.create_task(self._stop_execution(request))
            self._stop_task.add_done_callback(self._stop_finished)
        receipt = await asyncio.shield(self._stop_task)
        return receipt.model_copy(update={"connection_epoch": request.connection_epoch})

    async def _stop_execution(self, request: StopExecution) -> ExecutionStopped:
        self._collect_finished()
        record = self.state.load()
        assert record is not None
        receipt = record.stopped
        if receipt is None:
            if record.execution_nonce is None:
                raise WorkerProtocolError("stop blocked: session lacks execution ownership nonce")
            if record.state == "started" and self._execution is None:
                raise WorkerProtocolError("stop blocked: old process ownership is unknown")
            execution = self._request_cancel()
            if execution is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(execution), timeout=15)
                except asyncio.CancelledError:
                    if not execution.cancelled():
                        raise
                except Exception as exc:
                    raise WorkerProtocolError("stop blocked: executor cleanup was not confirmed") from exc
            receipt = ExecutionStopped(**request.model_dump(exclude={"type"}),
                                       execution_nonce=record.execution_nonce)
            self.state.record_stopped(receipt)
        self._execution = None
        self._discard_tool_calls()
        # Stop evidence precedes workspace reuse; only successful reclamation permits a reply.
        if record.stopped is None or not record.acknowledged:
            await self.executor.reclaim_workspace()
        return receipt

    def _stop_finished(self, task: asyncio.Task[ExecutionStopped]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.opt(exception=task.exception()).error("worker stop remains blocked")

    def on_stop_accepted(self, accepted: StopAccepted) -> None:
        """Retire stop state only after the server accepts the durable receipt."""
        self.state.acknowledge_stop(accepted)
        self._stop_task = None
        self._stop_request = None

    async def shutdown(self) -> None:
        """Finish owned cleanup before returning, even if the caller is repeatedly cancelled."""
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown())
        cancelled = False
        while not self._shutdown_task.done():
            try:
                await asyncio.shield(self._shutdown_task)
            except asyncio.CancelledError:
                cancelled = True
        self._shutdown_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _shutdown(self) -> None:
        failure: Exception | None = None
        try:
            if self._stop_task is not None:
                await asyncio.shield(self._stop_task)
            self._collect_finished()
        except Exception as exc:  # noqa: BLE001
            failure = exc
        execution = self._request_cancel()
        if execution is not None:
            try:
                await execution
            except asyncio.CancelledError:
                if not execution.cancelled():
                    raise
            except Exception as exc:  # noqa: BLE001
                if failure is None:
                    failure = exc
        self._discard_tool_calls()
        if failure is not None:
            raise failure
        self._execution = None
