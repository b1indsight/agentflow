"""Flow tools and history on one serial connection; execution owns the original Futures."""

import asyncio
from functools import partial

from app.communication.dispatch import Dispatcher
from app.communication.session import Session
from app.contracts.conversation import ConversationFailure, HistoryQuery
from app.contracts.protocol import (
    ConversationQuery,
    ConversationResult,
    FlowToolResponse,
    ServerMessageUnion,
    WorkerMessage,
    WorkerMessageUnion,
    message_header,
    result_header,
)
from app.worker.execution.lifecycle import PendingToolCall, WorkerExecution, WorkerProtocolError


class WorkerBusinessCallbacks:
    """Mount history requests and borrowed tool calls without owning execution lifetime."""

    def __init__(
        self,
        *,
        session: Session[ServerMessageUnion, WorkerMessageUnion],
        execution: WorkerExecution,
    ) -> None:
        self.session, self.execution = session, execution
        self.pending: PendingToolCall | None = None
        self._watching = False

    def bind(self, routes: Dispatcher[ServerMessageUnion]) -> None:
        self.routes = routes
        routes.register("worker.history", ConversationQuery, self.on_history)
        routes.before_wait(self.prepare)

    async def on_history(self, call: ConversationQuery, message: ServerMessageUnion) -> None:
        """Read inline with the driver; only the blocking filesystem/process IO leaves the loop."""
        try:
            query = HistoryQuery(**call.arguments.model_dump())
            page = await asyncio.to_thread(self.execution.state.read, query)
            result = ConversationResult(
                **result_header(call.message_id), method="worker.history", page=page
            )
        except ConversationFailure as error:
            result = ConversationResult(
                **result_header(call.message_id), method="worker.history", error=error.code
            )
        await self.session.send(
            WorkerMessage(
                **message_header(message.connection_epoch), control=None, payload=[result]
            )
        )

    async def prepare(self) -> None:
        """Reconcile borrowed work between events, without another callback's lifecycle calls.

        Normal completion retains the sent call until its late reply or assignment retirement.
        A fenced stop withdraws it. Neither path takes ownership of the execution's Future.
        """
        if self.session.closed:
            return
        assignment = self.execution.state.assignment
        if self.pending is not None and (
            self.execution.stopping
            or assignment is None
            or (assignment.job_id, assignment.assignment_token)
            != (
                self.pending.request.arguments.job_id,
                self.pending.request.arguments.assignment_token,
            )
        ):
            self._release(self.pending)
        active = self.session.epoch is not None and self.execution.lease() is not None
        if not active:
            if self._watching:
                await self.session.unwatch("tool")
                self._watching = False
        elif not self._watching and self.pending is None:
            self.session.watch("tool", self.execution.next_tool_call, self.send_tool)
            self._watching = True

    async def send_tool(self, call: PendingToolCall) -> None:
        self._watching = False
        if self.execution.lease() is None:
            return
        self.pending = call

        async def deliver(reply: FlowToolResponse, message: ServerMessageUnion) -> None:
            self.execution.finish_tool_call(call, reply)
            self._release(call)

        self.routes.expect_reply(
            call.request,
            FlowToolResponse,
            deliver,
            on_reply=partial(self.on_reply, call),
            on_rejected=partial(self.on_rejected, call),
        )
        try:
            written = await self.session.send(
                WorkerMessage(
                    **message_header(self.session.epoch), control=None, payload=[call.request]
                ),
                is_current=lambda: not call.future.done() and self.execution.lease() is not None,
            )
        except BaseException:
            self._release(call)
            raise
        if not written:
            self._release(call)

    async def on_reply(
        self, call: PendingToolCall, reply: FlowToolResponse, message: ServerMessageUnion
    ) -> None:
        """Check business identity after routing, before completing the caller's Future."""
        if reply.call_id != call.request.arguments.call_id:
            raise WorkerProtocolError("Flow tool response does not match its request")

    def on_rejected(self, call: PendingToolCall, reason: str) -> None:
        """Fail the matched call only after the connection owner has authorized rejection."""
        self.execution.reject_tool_call(call, reason)
        self._release(call)

    def _release(self, call: PendingToolCall) -> None:
        self.routes.forget_reply(call.request.message_id)
        if self.pending is call:
            self.pending = None
