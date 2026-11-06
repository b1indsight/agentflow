"""Server business callbacks for one authenticated Session and its original epoch."""

import asyncio
import hmac
from collections.abc import Callable
from uuid import uuid4

from fastapi import WebSocketDisconnect
from loguru import logger
from pydantic import ValidationError

from app.communication.dispatch import Dispatcher
from app.communication.session import Ready, Session, SessionOverflow
from app.contracts.protocol import (
    AcceptResultRequest,
    AcceptResultResponse,
    AcceptStop,
    Assignment,
    AssignmentArguments,
    ConversationResult,
    ExecuteResult,
    ExecutionTask,
    Heartbeat,
    InferenceResult,
    PayloadValidationError,
    ProtocolError,
    RecoveryDecision,
    RecoveryRejected,
    RecoveryRejectedArguments,
    Register,
    Registered,
    ResultAcceptance,
    ServerMessage,
    ServerMessageUnion,
    ServerPayload,
    Started,
    StopAccepted,
    StopExecution,
    StopResult,
    TerminalPayload,
    ToolCall,
    ToolResult,
    ValidationErrorResult,
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
from app.contracts.receipts import decode_result_inbox
from app.contracts.result_models import ResultPending
from app.contracts.validation import HeartbeatHeaders, RejectedMessage
from app.controler.workers.errors import ProtocolViolation
from app.controler.workers.flow import ServerFlowCallbacks
from app.controler.workers.inference import InferencePool
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.preparation import PreparationStale, PreparationState
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.tools import FlowToolService
from app.domain.models import DeliveryState, Job, JobKind, JobState, WorkerRecord
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.jobs import JobStore
from app.persistence.stops import StopIntent, StopStore
from app.persistence.workers import WorkerStore
from app.telemetry.metrics import Metrics


class ServerCallbacks:
    """Connection-local scheduling and evidence checks; Gateway alone drives events."""

    def __init__(
        self,
        coordinator: JobCoordinator,
        *,
        worker_id: str,
        register: Register,
        session: Session[WorkerMessageUnion, ServerMessageUnion],
        database: Database,
        tools: FlowToolService,
        results: ResultApplication,
        notify_jobs: Callable[[], None],
        inference: InferencePool,
        heartbeat_interval: float = 15.0,
        heartbeat_timeout: float = 45.0,
        poll_interval: float = 2.0,
        metrics: Metrics | None = None,
    ) -> None:
        self.worker_id, self.register, self.session = worker_id, register, session
        self.deliveries, self.jobs = DeliveryStore(database), JobStore(database)
        self.stops, self.workers = StopStore(database), WorkerStore(database)
        self.coordinator, self.results = coordinator, results
        self.notify_jobs, self.metrics = notify_jobs, metrics
        self.inference = inference
        self.heartbeat_interval, self.heartbeat_timeout = heartbeat_interval, heartbeat_timeout
        self.poll_interval = poll_interval
        self.worker: WorkerRecord | None = None
        self.current: tuple[Job, str, ExecutionTask | None] | None = None
        self.starting = False
        self.stopping: StopIntent | None = None
        self.stopped: StopResult | None = None
        self.initial_stop: StopIntent | None = None
        self.registered = False
        self.receive_deadline = 0.0
        self.disconnect_reason: str | None = None
        # Only the last critical reply of each fixed operation can require recovery.
        self._sent: dict[str, AcceptResultResponse | StopAccepted | RecoveryRejected] = {}
        self._unmatched_error_logged = False
        self._recovery_required = False
        self._refresh_receive = False
        self.flow = ServerFlowCallbacks(
            worker_id=worker_id,
            session=session,
            jobs=self.jobs,
            tools=tools,
            validate_active=self._validate_active,
            is_stopping=lambda: self.stopping is not None,
        )

    @property
    def epoch(self) -> str:
        epoch = self.session.epoch
        if epoch is None:
            raise RuntimeError("server session requires a connection epoch")
        return epoch

    def bind(self, routes: Dispatcher[WorkerMessageUnion]) -> None:
        self.routes = routes
        routes.register("server.execution_started", Started, self.on_started)
        self.flow.bind(routes)
        routes.register("server.accept_result", AcceptResultRequest, self.on_accept_result)
        routes.register("server.accept_stop", AcceptStop, self.on_accept_stop)
        routes.configure(
            opened=self.on_opened,
            message=self.on_message,
            unmatched_reply=self.on_unmatched_reply,
            validation_error=self.on_error,
            published=self.on_published,
            poll=self.poll,
            before_item=self.before_item,
            message_done=self.on_message_done,
            failed=self.on_failed,
            closed=self.on_closed,
            unrouted=self.flow.on_unrouted,
            rejected=self.on_rejected,
        )

    async def on_unmatched_reply(self, reply: ToolResult, message: WorkerMessageUnion) -> None:
        """Authorize durable replay when the original connection's receiver is gone."""
        if isinstance(reply, (ConversationResult, InferenceResult)):
            return  # A timed-out query is never replayed or evidence of execution progress.
        # Old results can replay after another assignment starts. The payload's job_id
        # is only a lookup hint; the original call ID must match persisted credentials.
        if isinstance(reply, ExecuteResult):
            job = self.jobs.get_job(reply.job_id)
            if (
                job is not None
                and job.assigned_worker_id == self.worker_id
                and job.assignment_token is not None
                and reply.reply_to == execution_call_id(job.id, job.assignment_token)
            ):
                await self.on_result(reply, message)
                return
        raise ProtocolViolation("reply has no matching invocation")

    def on_published(self) -> None:
        self.worker = self.workers.worker_connected(
            self.worker_id, self.register.protocol_version, self.epoch
        )

    async def on_opened(self) -> None:
        self.receive_deadline = asyncio.get_running_loop().time() + self.heartbeat_timeout
        self.initial_stop = self.stops.pending(self.worker_id)
        recovery = self.register.recovery
        if (
            self.initial_stop is None
            and recovery is not None
            and recovery.stop_request_id is not None
        ):
            self.initial_stop = self.stops.receipt_request(self.worker_id, recovery.stop_request_id)
        if self.initial_stop is not None:
            await self._begin_stop(self.initial_stop)
        elif recovery is not None:
            self.current = await self._recover()
            if self.current is not None:
                job, token, _ = self.current
                # Recovery restores only the original routing identity, not a new Assignment.
                self.routes.expect_reply(
                    ToolCall(
                        **call_header(execution_call_id(job.id, token)), method="worker.execute"
                    ),
                    ExecuteResult,
                    self.on_result,
                )
        self.registered = self.current is not None
        if self.registered:
            self.inference.ready(self.worker_id, self.session, self.register.inference)

    def _matches_current(self, job_id: int, token: str) -> bool:
        return (
            self.current is not None
            and self.current[0].id == job_id
            and hmac.compare_digest(self.current[1], token)
        )

    def _validate_active(self, job_id: int, token: str) -> None:
        if self.stopping is not None:
            if (job_id, token) != (self.stopping.job_id, self.stopping.assignment_token):
                raise ProtocolViolation("stop reply has stale assignment credentials")
        elif not self._matches_current(job_id, token):
            raise ProtocolViolation("message does not match the active assignment")

    async def _begin_stop(self, intent: StopIntent) -> None:
        self.stopping, self.stopped = intent, None
        fence = intent.fence()
        await self._send(
            StopExecution(**call_header(stop_call_id(fence)), method="worker.stop", arguments=fence)
        )
        self.session.set_deadline(asyncio.get_running_loop().time() + 20)
        self.receive_deadline = asyncio.get_running_loop().time() + self.heartbeat_timeout

    async def poll(self) -> float | None:
        """Advance scheduling once; return an absolute receive/idle deadline to Session."""
        assert self.worker is not None
        if not self.registered and self.stopping is None:
            stale = self.worker.current_job_id
            if self.initial_stop is not None and stale == self.initial_stop.job_id:
                stale = None
            if stale is not None and (
                self.register.recovery is None or self.register.recovery.job_id != stale
            ):
                await self._reject_missing_journal(stale)
            elif (
                self.register.recovery is None
                and (binding := self.workers.get_worker_execution(self.worker_id)) is not None
            ):
                delivery = self.deliveries.get_delivery(binding.flow_id)
                if delivery is not None and delivery.state is DeliveryState.MERGED:
                    raise ProtocolViolation("archive blocked: original worker journal is missing")
            await self.session.send(
                Registered(
                    **message_header(self.epoch),
                    control="registered",
                    payload=[],
                    recovery=None,
                    heartbeat_interval=self.heartbeat_interval,
                )
            )
            self.registered = True
            self.inference.ready(self.worker_id, self.session, self.register.inference)
        if self.stopping is None and not self.starting:
            stop = self.stops.pending(self.worker_id)
            if stop is not None:
                await self._begin_stop(stop)
        if self.current is None and self.stopping is None:
            token = uuid4().hex
            job = self.workers.assign_job(self.worker_id, token, self.epoch)
            if job is not None:
                prepared = await self._prepare_assignment(job, token)
                if isinstance(prepared, JobState):
                    if prepared is JobState.QUEUED:
                        self.workers.requeue_assignment(job.id, self.worker_id, token, self.epoch)
                    else:
                        self.workers.finish_assignment(
                            job.id, self.worker_id, token, prepared, self.epoch
                        )
                    self.notify_jobs()
                    return asyncio.get_running_loop().time()
                if not self.stops.authorize_dispatch(job.id, self.worker_id, token, self.epoch):
                    raise ProtocolViolation("assignment changed before dispatch authorization")
                self.current, self.starting = (job, token, prepared.task), True
                await self._send(
                    Assignment(
                        **call_header(execution_call_id(job.id, token)),
                        method="worker.execute",
                        arguments=prepared,
                    )
                )
                self.receive_deadline = asyncio.get_running_loop().time() + self.heartbeat_timeout
        now = asyncio.get_running_loop().time()
        if self.current is None and self.stopping is None:
            return now + self.poll_interval
        if now >= self.receive_deadline:
            raise TimeoutError("worker heartbeat timeout")
        return self.receive_deadline

    def _validate_control(self, message: WorkerMessageUnion | RejectedMessage) -> None:
        if message.connection_epoch != self.epoch or isinstance(message, Register):
            raise ProtocolViolation("message does not match the connection epoch")
        if isinstance(message, (Heartbeat, HeartbeatHeaders)):
            if self.starting:
                raise ProtocolViolation("assignment must be acknowledged with Started")
            self._validate_active(message.job_id, message.assignment_token)

    async def on_message(self, message: WorkerMessageUnion) -> None:
        # Independent inference/history and validation errors do not prove coding progress.
        self._refresh_receive = message.connection_epoch == self.epoch and (
            message.control is not None
            or any(
                not isinstance(item, (ValidationErrorResult, ConversationResult, InferenceResult))
                for item in message.payload
            )
        )
        if (
            message.control is None
            and message.connection_epoch != self.epoch
            and all(isinstance(item, ValidationErrorResult) for item in message.payload)
        ):
            return
        self._validate_control(message)
        if (
            isinstance(message, Heartbeat)
            and self.stopping is None
            and not self.workers.heartbeat_assignment(
                message.job_id,
                self.worker_id,
                message.assignment_token,
                self.epoch,
            )
        ):
            applied, _ = self._stored_result(message.job_id, message.assignment_token)
            if applied.result_applied_at is None or applied.state not in {
                JobState.DONE,
                JobState.FAILED,
            }:
                raise ProtocolViolation("heartbeat belongs to an inactive assignment")

    async def before_item(self, item: ToolCall | ToolResult, message: WorkerMessageUnion) -> None:
        if isinstance(item, ValidationErrorResult):
            return  # The error hook enforces correlation and the pre-start fence.
        if self._recovery_required:
            raise ProtocolViolation("critical protocol payload rejected; recovery required")
        if isinstance(item, (ConversationResult, InferenceResult)):
            return  # Read-only replies need no Started and cannot initiate execution effects.
        if self.starting and not isinstance(item, Started):
            raise ProtocolViolation("assignment must be acknowledged with Started")
        if not self.starting and self.stopping is None:
            stop = self.stops.pending(self.worker_id)
            if stop is not None:
                await self._begin_stop(stop)

    def on_message_done(self) -> None:
        if self._recovery_required:
            raise ProtocolViolation("critical protocol payload rejected; recovery required")
        if self._refresh_receive:
            self.receive_deadline = asyncio.get_running_loop().time() + self.heartbeat_timeout

    async def on_started(self, item: Started, message: WorkerMessageUnion) -> None:
        if not self.starting or self.current is None or self.stopping is not None:
            raise ProtocolViolation("unexpected Started")
        self._validate_active(item.arguments.job_id, item.arguments.assignment_token)
        job, token, task = self.current
        assert task is not None
        if not self.workers.start_assignment(
            job.id, task.attempt_id, self.worker_id, token, self.epoch
        ):
            raise ProtocolViolation("assignment is no longer active")
        self.coordinator.mark_started(job, task, self.worker_id)
        self.starting = False

    async def on_result(self, item: ExecuteResult, message: WorkerMessageUnion) -> None:
        if self.stopping is not None:
            self._validate_active(item.job_id, item.assignment_token)
        terminal = item.terminal()
        if item.reply_to != execution_call_id(
            item.job_id, item.assignment_token
        ) or item.message_id != terminal_id(terminal):
            raise ProtocolViolation("terminal has invalid execution/result identity")
        job = self.jobs.get_job(item.job_id)
        if (
            job is None
            or job.assigned_worker_id != self.worker_id
            or job.assignment_token != item.assignment_token
            or (
                self.stopping is None
                and not self._matches_current(job.id, item.assignment_token)
                and job.result_payload is None
            )
        ):
            raise ProtocolViolation("result belongs to another assignment")
        receipt = self.workers.record_assignment_result(
            job.id, self.worker_id, item.assignment_token, self.epoch, terminal
        )
        if receipt in {"conflict", "rejected"}:
            raise ProtocolViolation("result conflicts with persisted assignment facts")
        await self.results.apply(job.id)

    async def on_accept_result(self, item: AcceptResultRequest, message: WorkerMessageUnion) -> None:
        if self.stopping is not None:
            self._validate_active(item.arguments.job_id, item.arguments.assignment_token)
        job = self._validate_result_call(item)
        if self.stopping is not None:
            return
        if job.result_applied_at is None:
            if not self._matches_current(job.id, item.arguments.assignment_token):
                raise ProtocolViolation("unapplied result is not the current assignment")
            await self.results.apply(job.id)
        if await self._accept_result(item):
            if self._matches_current(job.id, item.arguments.assignment_token):
                self.routes.forget_reply(execution_call_id(job.id, item.arguments.assignment_token))
                self.current = None
            self.notify_jobs()

    async def on_stopped(self, item: StopResult, message: WorkerMessageUnion) -> None:
        receipt = item.result
        if self.stopping is None:
            raise ProtocolViolation("unexpected stop receipt")
        fence = self.stopping.fence()
        if (
            item.reply_to != stop_call_id(fence)
            or item.message_id != stop_receipt_id(receipt)
            or (receipt.request_id, receipt.flow_id, receipt.job_id, receipt.assignment_token)
            != (fence.request_id, fence.flow_id, fence.job_id, fence.assignment_token)
            or not self.stops.confirm(self.worker_id, receipt, self.epoch)
        ):
            raise ProtocolViolation("stop blocked: receipt or external effects not converged")
        self.stopped = item

    async def on_accept_stop(self, item: AcceptStop, message: WorkerMessageUnion) -> None:
        args = item.arguments
        if self.stopping is None or self.stopped is None:
            raise ProtocolViolation("stop acceptance requires an actual validated receipt")
        fence = self.stopping.fence()
        if (
            args.receipt_id != self.stopped.message_id
            or item.message_id != accept_stop_id(self.stopped.message_id)
            or (args.request_id, args.flow_id, args.job_id, args.assignment_token)
            != (fence.request_id, fence.flow_id, fence.job_id, fence.assignment_token)
            or not self.stops.confirm(self.worker_id, self.stopped.result, self.epoch)
        ):
            raise ProtocolViolation("stop acceptance conflicts with persisted facts")
        if await self._send(
            StopAccepted(
                **result_header(item.message_id), method="server.accept_stop", result=fence
            )
        ):
            if self._matches_current(fence.job_id, fence.assignment_token):
                self.routes.forget_reply(execution_call_id(fence.job_id, fence.assignment_token))
                self.current = None
            self.routes.forget_reply(stop_call_id(fence))
            self.stopping, self.stopped, self.starting = None, None, False
            self.session.set_deadline(None)
            self.notify_jobs()

    async def on_error(self, error: ValidationErrorResult, message: WorkerMessageUnion) -> None:
        if message.connection_epoch == self.epoch:
            if self.current is not None:
                job, token, _ = self.current
                if (
                    error.reply_to == execution_call_id(job.id, token)
                    and error.method == "worker.execute"
                    and self.starting
                ):
                    if self.stopping is not None:
                        return  # No journal exists to supply R; preserve the safe blocked stop.
                    if not self.workers.reject_assignment(
                        job.id, self.worker_id, token, self.epoch
                    ):
                        stop = self.stops.pending(self.worker_id)
                        if stop is not None:
                            await self._begin_stop(stop)
                            return
                        raise ProtocolViolation(
                            "assignment rejection fence expired; recovery required"
                        )
                    await self.results.apply(job.id)
                    current = self.jobs.get_job(job.id)
                    if (
                        current is None
                        or current.result_applied_at is None
                        or current.state is not JobState.FAILED
                    ):
                        raise ResultPending("assignment rejection application has not committed")
                    if (
                        self.session.closed
                        or current.assigned_worker_id != self.worker_id
                        or current.assignment_token != token
                    ):
                        raise ProtocolViolation("assignment rejection lost its connection fence")
                    self.routes.forget_reply(execution_call_id(job.id, token))
                    self.current, self.starting = None, False
                    self.notify_jobs()
                    return
            sent = next(
                (item for item in self._sent.values() if item.message_id == error.reply_to), None
            )
            if (sent is not None and error.method == sent.method) or (
                self.stopping is not None
                and error.reply_to == stop_call_id(self.stopping.fence())
                and error.method == "worker.stop"
            ):
                self._recovery_required = True
                return
            if self.routes.reject_reply(error, "message payload failed validation"):
                return
        if not self._unmatched_error_logged:
            logger.warning("ignored unmatched validation error on server connection")
            self._unmatched_error_logged = True

    def on_rejected(self, message: RejectedMessage) -> Ready:
        if not self.registered:
            raise ProtocolViolation("invalid payload before registration")
        self._validate_control(message)
        critical = False
        replies: list[ToolResult] = []
        for item in message.payload:
            if isinstance(item, ToolResult) and item.method in {"worker.history", "worker.infer"}:
                replies.append(item)
                continue
            if self.starting and item.method != "server.execution_started":
                raise ProtocolViolation("assignment must be acknowledged with Started")
            if item.method in {
                "server.execution_started",
                "server.accept_result",
                "server.accept_stop",
                "worker.execute",
                "worker.stop",
            }:
                critical = True
            elif isinstance(item, ToolResult):
                raise ProtocolViolation("invalid reply has no pending invocation")
        # Do not notify any caller until every header in the batch is authorized.
        for item in replies:
            self.routes.reject_reply(item, "message payload failed validation")

        async def respond() -> None:
            await self.session.send(
                ServerMessage(
                    **message_header(self.epoch),
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
                raise ProtocolViolation("critical protocol payload rejected; recovery required")

        return respond

    async def on_failed(self, error: Exception) -> int:
        if isinstance(error, ResultPending):
            self.disconnect_reason = str(error)
            return 1013
        if isinstance(error, (WebSocketDisconnect, ConnectionError, TimeoutError)):
            self.disconnect_reason = str(error) or type(error).__name__
            logger.bind(
                event="worker_disconnected", reason_code="connection_lost", worker_id=self.worker_id
            ).opt(exception=error).info("")
            return 1000
        if isinstance(error, (ProtocolViolation, SessionOverflow, ValidationError)):
            invalid = isinstance(error, ValidationError)
            self.disconnect_reason = "message failed protocol validation" if invalid else str(error)
            logger.warning("worker protocol violation: {}", self.disconnect_reason)
            try:
                await self.session.send(
                    ProtocolError(
                        **message_header(self.epoch),
                        control="error",
                        payload=[],
                        code="invalid_message" if invalid else "protocol_violation",
                        detail=self.disconnect_reason,
                    )
                )
            except (WebSocketDisconnect, ConnectionError):
                pass
            return 1008
        raise error

    def on_closed(self) -> None:
        self.inference.closed(self.worker_id, self.session)
        self.workers.worker_disconnected(
            self.worker_id, self.disconnect_reason, connection_epoch=self.epoch
        )

    async def _send(self, item: ServerPayload) -> bool:
        previous = self._sent.get(item.method)
        tracked = isinstance(item, (AcceptResultResponse, StopAccepted, RecoveryRejected))
        if isinstance(item, (AcceptResultResponse, StopAccepted, RecoveryRejected)):
            self._sent[item.method] = item
        if isinstance(item, Assignment):
            self.routes.expect_reply(item, ExecuteResult, self.on_result)
        elif isinstance(item, StopExecution):
            self.routes.expect_reply(item, StopResult, self.on_stopped)
        try:
            return await self.session.send(
                ServerMessage(**message_header(self.epoch), control=None, payload=[item])
            )
        except BaseException:
            if isinstance(item, (Assignment, StopExecution)):
                self.routes.forget_reply(item.message_id)
            if tracked:
                if previous is None:
                    self._sent.pop(item.method, None)
                else:
                    self._sent[item.method] = previous
            raise

    async def _prepare_assignment(
        self, job: Job, token: str
    ) -> AssignmentArguments | PreparationState:
        async def build() -> AssignmentArguments | PreparationState:
            try:
                return await self.coordinator.prepare_for_assignment(
                    job, self.worker_id, token, self.epoch
                )
            except PreparationStale as error:
                raise ProtocolViolation(str(error)) from error

        preparing = asyncio.create_task(build())
        try:
            while True:
                if not self.workers.heartbeat_assignment(job.id, self.worker_id, token, self.epoch):
                    raise ProtocolViolation("assignment expired during preparation")
                done, _ = await asyncio.wait({preparing}, timeout=self.heartbeat_interval)
                if done:
                    return preparing.result()
        finally:
            preparing.cancel()
            await asyncio.gather(preparing, return_exceptions=True)

    def _stored_result(self, job_id: int, token: str) -> tuple[Job, TerminalPayload]:
        job = self.jobs.get_job(job_id)
        if (
            job is None
            or job.assigned_worker_id != self.worker_id
            or job.assignment_token != token
            or job.result_payload is None
            or job.result_digest is None
        ):
            raise ProtocolViolation("result acceptance has no matching durable result")
        try:
            inbox = decode_result_inbox(
                job.result_payload,
                job_id=job.id,
                assignment_token=token,
                result_digest=job.result_digest,
            )
        except ValueError as error:
            raise ProtocolViolation("result acceptance conflicts with durable inbox") from error
        if inbox.source != "worker" or self.workers.record_assignment_result(
            job.id,
            self.worker_id,
            token,
            self.epoch,
            inbox.terminal,
        ) in {"conflict", "rejected"}:
            raise ProtocolViolation("result acceptance has invalid assignment/result identity")
        return job, inbox.terminal

    def _validate_result_call(self, call: AcceptResultRequest) -> Job:
        args = call.arguments
        job, terminal = self._stored_result(args.job_id, args.assignment_token)
        identity = terminal_id(terminal)
        if args.result_id != identity or call.message_id != accept_result_id(identity):
            raise ProtocolViolation("result acceptance has invalid result identity")
        return job

    async def _accept_result(self, call: AcceptResultRequest) -> bool:
        job = self.jobs.get_job(call.arguments.job_id)
        if (
            job is None
            or job.result_applied_at is None
            or job.state not in {JobState.DONE, JobState.FAILED}
        ):
            raise ProtocolViolation("result application has not committed")
        return await self._send(
            AcceptResultResponse(
                **result_header(call.message_id),
                method="server.accept_result",
                result=ResultAcceptance(
                    job_id=job.id,
                    assignment_token=call.arguments.assignment_token,
                    state="done" if job.state is JobState.DONE else "failed",
                ),
            )
        )

    async def _recover(self) -> tuple[Job, str, ExecutionTask | None] | None:
        recovery = self.register.recovery
        assert recovery is not None

        async def reject(reason: str) -> None:
            await self._send(
                RecoveryRejected(
                    **call_header(),
                    method="worker.reject_recovery",
                    arguments=RecoveryRejectedArguments(
                        job_id=recovery.job_id,
                        reason=reason,
                        register_message_id=self.register.message_id,
                    ),
                )
            )

        job = self.jobs.get_job(recovery.job_id)
        if (
            job is None
            or job.assigned_worker_id != self.worker_id
            or job.assignment_token != recovery.assignment_token
        ):
            await reject("assignment fence expired")
            return None
        if (
            job.result_digest is not None
            and recovery.result_digest is not None
            and job.result_digest != recovery.result_digest
        ):
            await reject("recovery result conflicts with persisted digest")
            return None
        if (
            job.result_applied_at is not None
            and job.result_digest is not None
            and recovery.result_digest == job.result_digest
            and job.state in (JobState.DONE, JobState.FAILED)
        ):
            if self.metrics is not None:
                self.metrics.replays.inc()
            logger.bind(
                event="job_result",
                reason_code="ok",
                job_id=job.id,
                flow_id=job.flow_id,
                worker_id=self.worker_id,
                epoch=self.epoch,
            ).info("")
            if self.register.payload:
                call = self.register.payload[0]
                assert isinstance(call, AcceptResultRequest)
                self._validate_result_call(call)
                await self._accept_result(call)
            else:
                self._stored_result(job.id, recovery.assignment_token)
            return None
        if recovery.state == "received":
            requeued = self.workers.requeue_assignment(
                job.id, self.worker_id, recovery.assignment_token, self.epoch
            )
            reason = "unstarted assignment abandoned"
            if not requeued:
                reason = "worker lost executor during assignment start"
                await self.coordinator.fail(job, reason)
            await reject(reason)
            self.notify_jobs()
            return None
        if (
            recovery.state == "started"
            and not recovery.executor_active
            and not (job.kind is JobKind.ARCHIVE and recovery.archive_resume)
        ):
            reason = "worker restarted after remote execution started"
            delivery = self.deliveries.get_delivery(job.flow_id)
            if delivery is not None and delivery.supersedes(job.kind):
                raise ProtocolViolation(
                    "archive blocked: original executor exit cannot be confirmed"
                )
            await self.coordinator.fail(job, reason)
            await reject(reason)
            return None
        if not self.workers.resume_assignment(
            job.id, self.worker_id, recovery.assignment_token, self.epoch
        ):
            await reject("recovery deadline expired")
            return None
        await self.session.send(
            Registered(
                **message_header(self.epoch),
                control="registered",
                payload=[],
                heartbeat_interval=self.heartbeat_interval,
                recovery=RecoveryDecision(
                    job_id=job.id,
                    assignment_token=recovery.assignment_token,
                    action="replay" if recovery.state in {"succeeded", "failed"} else "resume",
                ),
            )
        )
        return job, recovery.assignment_token, None

    async def _reject_missing_journal(self, job_id: int) -> None:
        job = self.jobs.get_job(job_id)
        if job is None or job.assignment_token is None:
            return
        if job.started_at is None and self.workers.requeue_assignment(
            job.id, self.worker_id, job.assignment_token, self.epoch
        ):
            self.notify_jobs()
            return
        delivery = self.deliveries.get_delivery(job.flow_id)
        if delivery is not None and delivery.state in {DeliveryState.CLOSED, DeliveryState.MERGED}:
            action = "reopen" if delivery.state is DeliveryState.CLOSED else "archive"
            raise ProtocolViolation(
                f"{action} blocked: original worker assignment journal is missing"
            )
        await self.coordinator.fail(job, "worker reconnected without its assignment journal")
