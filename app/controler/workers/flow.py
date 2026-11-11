"""Flow tool routes on the server, fenced by the original worker connection."""

from collections.abc import Callable

from app.communication.dispatch import Dispatcher
from app.communication.session import Session
from app.contracts.protocol import (
    FlowToolErrorKind,
    FlowToolRequest,
    FlowToolResponse,
    ServerMessage,
    ServerMessageUnion,
    ToolCall,
    WorkerMessageUnion,
    message_header,
    result_header,
)
from app.controler.workers.errors import ProtocolViolation
from app.controler.workflow.tools import FlowToolService
from app.persistence.jobs import JobStore


class ServerFlowCallbacks:
    """Handle Flow calls while borrowing live assignment and stopping checks."""

    def __init__(
        self,
        *,
        worker_id: str,
        session: Session[WorkerMessageUnion, ServerMessageUnion],
        jobs: JobStore,
        tools: FlowToolService,
        validate_active: Callable[[int, str], None],
        is_stopping: Callable[[int], bool],
    ) -> None:
        self.worker_id, self.session = worker_id, session
        self.jobs, self.tools = jobs, tools
        self.validate_active, self.is_stopping = validate_active, is_stopping

    @property
    def epoch(self) -> str:
        epoch = self.session.epoch
        if epoch is None:
            raise RuntimeError("server session requires a connection epoch")
        return epoch

    def bind(self, routes: Dispatcher[WorkerMessageUnion]) -> None:
        routes.register("flow.open_pr", FlowToolRequest, self.on_tool)

    def _validate_tool(self, item: FlowToolRequest) -> None:
        args = item.arguments
        self.validate_active(args.job_id, args.assignment_token)
        job = self.jobs.get_job(args.job_id)
        if job is None or args.flow_id != job.flow_id:
            raise ProtocolViolation("Flow tool call belongs to another Flow")

    async def on_tool(self, item: FlowToolRequest, message: WorkerMessageUnion) -> None:
        self._validate_tool(item)
        if self.is_stopping(item.arguments.job_id):
            return
        result = await self.tools.execute(
            item, worker_id=self.worker_id, connection_epoch=self.epoch
        )
        await self.session.send(
            ServerMessage(**message_header(self.epoch), control=None, payload=[result])
        )

    async def on_unrouted(self, item: ToolCall, message: WorkerMessageUnion) -> None:
        if not isinstance(item, FlowToolRequest):
            raise ProtocolViolation("unregistered message route")
        self._validate_tool(item)
        if self.is_stopping(item.arguments.job_id):
            return
        result = FlowToolResponse(
            **result_header(item.message_id),
            method=item.method,
            call_id=item.arguments.call_id,
            error="unregistered Flow tool",
            error_kind=FlowToolErrorKind.INVALID_REQUEST,
        )
        await self.session.send(
            ServerMessage(**message_header(self.epoch), control=None, payload=[result])
        )
