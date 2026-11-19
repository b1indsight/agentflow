"""Flow tools and history on one serial connection; execution owns the original Futures."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

from app.communication.dispatch import Dispatcher
from app.communication.session import Session
from app.contracts.conversation import ConversationFailure, HistoryQuery
from app.contracts.protocol import (
    CatalogQuery,
    CatalogResult,
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
from app.worker.model_catalog import CatalogUnavailable

if TYPE_CHECKING:
    from app.worker.runtime import WorkerRuntime


class WorkerBusinessCallbacks:
    """Mount history requests and borrowed tool calls without owning execution lifetime."""

    def __init__(
        self,
        *,
        session: Session[ServerMessageUnion, WorkerMessageUnion],
        runtime: WorkerRuntime,
        ready: Callable[[], bool],
    ) -> None:
        self.session, self.runtime = session, runtime
        self.ready = ready
        self.pending: dict[int, PendingToolCall] = {}
        self._queries: dict[asyncio.Task[ConversationResult | CatalogResult], str | None] = {}

    def bind(self, routes: Dispatcher[ServerMessageUnion]) -> None:
        self.routes = routes
        routes.register("worker.history", ConversationQuery, self.on_history)
        routes.register("worker.model_catalog", CatalogQuery, self.on_catalog)
        routes.before_wait(self.prepare)

    async def on_history(self, call: ConversationQuery, message: ServerMessageUnion) -> None:
        """Keep the driver free to receive registration replies while history is prepared."""
        if len(self._queries) >= 2:
            await self._send_query(ConversationResult(
                **result_header(call.message_id), method=call.method, error="query_busy",
            ), message.connection_epoch)
            return
        task = asyncio.create_task(self._read_history(call))
        self._queries[task] = message.connection_epoch
        task.add_done_callback(self._query_completed)

    async def on_catalog(self, call: CatalogQuery, message: ServerMessageUnion) -> None:
        if not self.ready() or len(self._queries) >= 2:
            await self._send_query(CatalogResult(**result_header(call.message_id), method=call.method,
                                                 models=None), message.connection_epoch)
            return
        task = asyncio.create_task(self._read_catalog(call))
        self._queries[task] = message.connection_epoch
        task.add_done_callback(self._query_completed)

    async def _read_catalog(self, call: CatalogQuery) -> CatalogResult:
        try:
            models = await self.runtime.coding_agent.discover_models()
        except CatalogUnavailable:
            models = None
        return CatalogResult(**result_header(call.message_id), method=call.method,
                             models=models if models is not None and len(models) <= 1024 else None)

    def _query_completed(self, task: asyncio.Task[ConversationResult | CatalogResult]) -> None:
        if not task.cancelled():
            task.exception()
        self.session.wake()

    async def _read_history(self, call: ConversationQuery) -> ConversationResult:
        try:
            query = HistoryQuery(**call.arguments.model_dump())
            page = await self.runtime.read_history(query)
            return ConversationResult(
                **result_header(call.message_id), method="worker.history", page=page
            )
        except ConversationFailure as error:
            return ConversationResult(
                **result_header(call.message_id), method="worker.history", error=error.code
            )

    async def _send_query(self, result: ConversationResult | CatalogResult, epoch: str | None) -> None:
        await self.session.send(
            WorkerMessage(
                **message_header(epoch), control=None, payload=[result]
            )
        )

    async def aclose(self) -> None:
        for task in self._queries:
            task.cancel()
        await asyncio.gather(*self._queries, return_exceptions=True)
        self._queries.clear()

    async def prepare(self) -> None:
        """Reconcile borrowed work between events, without another callback's lifecycle calls.

        Normal completion retains the sent call until its late reply or assignment retirement.
        A fenced stop withdraws it. Neither path takes ownership of the execution's Future.
        """
        if self.session.closed:
            return
        for task in tuple(self._queries):
            if task.done():
                epoch = self._queries.pop(task)
                await self._send_query(task.result(), epoch)
        if not self.ready():
            return
        active_jobs = set()
        for execution in self.runtime.executions.values():
            assignment = execution.state.assignment
            record = execution.state.load()
            if record is None:
                continue
            active_jobs.add(record.job_id)
            pending = self.pending.get(record.job_id)
            if pending is not None and (
                execution.stopping or assignment is None
                or (assignment.job_id, assignment.assignment_token)
                != (pending.request.arguments.job_id, pending.request.arguments.assignment_token)
            ):
                self._release(pending)
            if (self.session.epoch is not None and execution.lease() is not None
                    and record.job_id not in self.pending):
                call = execution.pending_tool_call()
                if call is not None:
                    await self.send_tool(execution, call)
        for job_id in self.pending.keys() - active_jobs:
            self._release(self.pending[job_id])

    async def send_tool(self, execution: WorkerExecution, call: PendingToolCall) -> None:
        if execution.lease() is None:
            return
        self.pending[call.request.arguments.job_id] = call

        async def deliver(reply: FlowToolResponse, message: ServerMessageUnion) -> None:
            execution.finish_tool_call(call, reply)
            self._release(call)

        self.routes.expect_reply(
            call.request,
            FlowToolResponse,
            deliver,
            on_reply=partial(self.on_reply, call),
            on_rejected=partial(self.on_rejected, execution, call),
        )
        try:
            written = await self.session.send(
                WorkerMessage(
                    **message_header(self.session.epoch), control=None, payload=[call.request]
                ),
                is_current=lambda: not call.future.done() and execution.lease() is not None,
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

    async def on_rejected(
        self, execution: WorkerExecution, call: PendingToolCall, reason: str
    ) -> None:
        """Fail the matched call only after the connection owner has authorized rejection."""
        execution.reject_tool_call(call, reason)
        self._release(call)

    def _release(self, call: PendingToolCall) -> None:
        self.routes.forget_reply(call.request.message_id)
        if self.pending.get(call.request.arguments.job_id) is call:
            self.pending.pop(call.request.arguments.job_id)
