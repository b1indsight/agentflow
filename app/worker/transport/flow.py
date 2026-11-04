"""Flow tool callbacks for one worker connection; execution owns the original Futures."""

from app.communication.session import Session
from app.contracts.protocol import (
    FlowToolResponse,
    ServerMessage,
    ToolResult,
    WorkerFrame,
    WorkerMessage,
    frame_header,
)
from app.worker.execution.lifecycle import PendingToolCall, WorkerExecution, WorkerProtocolError


class WorkerFlowCallbacks:
    """Borrow a pending tool call without transferring its lifetime to the connection."""

    def __init__(
        self, *, session: Session[ServerMessage, WorkerMessage], execution: WorkerExecution
    ) -> None:
        self.session, self.execution = session, execution
        self.pending: PendingToolCall | None = None

    def observe(self) -> None:
        self.session.watch("tool", self.execution.next_tool_call, self.send_tool)

    async def send_tool(self, call: PendingToolCall) -> None:
        self.pending = call
        try:
            written = await self.session.send(
                WorkerFrame(
                    **frame_header(self.session.epoch), control=None, payload=[call.request]
                ),
                is_current=lambda: not call.future.done(),
            )
        except BaseException:
            self.pending = None
            raise
        if not written:
            self._finished()

    async def on_reply(self, reply: ToolResult, message: ServerMessage) -> None:
        if self.pending is None or reply.reply_to != self.pending.request.message_id:
            raise WorkerProtocolError("Flow tool result has no pending invocation")
        if not isinstance(reply, FlowToolResponse):
            raise WorkerProtocolError("Flow tool reply has the wrong result model")
        self.execution.finish_tool_call(self.pending, reply)
        self._finished()

    def matches(self, reply: ToolResult) -> bool:
        """Check correlation without changing a Future during whole-message validation."""
        return self.pending is not None and (reply.method, reply.reply_to) == (
            self.pending.request.method,
            self.pending.request.message_id,
        )

    def reject(self, reason: str) -> None:
        """Fail the matched call only after the connection owner has authorized rejection."""
        assert self.pending is not None
        self.execution.reject_tool_call(self.pending, reason)
        self._finished()

    def _finished(self) -> None:
        self.pending = None
        if self.execution.lease() is not None:
            self.observe()
