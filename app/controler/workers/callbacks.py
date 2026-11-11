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
    MAX_PAYLOAD_ITEMS,
    AcceptResultRequest,
    AcceptResultResponse,
    AcceptStop,
    Assignment,
    AssignmentArguments,
    CancelJob,
    ConversationResult,
    ExecuteResult,
    ExecutionTask,
    Failed,
    FlowToolRequest,
    Heartbeat,
    InferenceRequest,
    InferenceResult,
    Inventory,
    InventoryPage,
    PayloadValidationError,
    ProtocolError,
    RecoveryDecision,
    RecoveryDescriptor,
    RecoveryRejected,
    RecoveryRejectedArguments,
    Register,
    Registered,
    RepositoryDemand,
    ResultAcceptance,
    ServerMessage,
    ServerMessageUnion,
    ServerPayload,
    Started,
    StopAccepted,
    StopExecution,
    StopResult,
    Succeeded,
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
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.preparation import PreparationStale, PreparationState
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.tools import FlowToolService
from app.domain.inference_models import InferenceFailure
from app.domain.models import Job, JobKind, JobState, WorkerRecord
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
        self.heartbeat_interval, self.heartbeat_timeout = heartbeat_interval, heartbeat_timeout
        self.poll_interval = poll_interval
        self.worker: WorkerRecord | None = None
        self.current: dict[int, tuple[Job, str, ExecutionTask | None]] = {}
        self.preparing: dict[int, tuple[Job, str, asyncio.Task[AssignmentArguments | PreparationState]]] = {}
        self.inference_current: Job | None = None
        self.inference_cancelling = False
        self.starting: set[int] = set()
        self.stopping: dict[int, StopIntent] = {}
        self.stopped: dict[int, StopResult] = {}
        self._seen_workspaces: set[str] = set()
        self._seen_recoveries: set[int] = set()
        self.registered = False
        self.receive_deadline = 0.0
        self.disconnect_reason: str | None = None
        # Critical replies are retained per invocation until the worker reports settlement.
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
            is_stopping=lambda job_id: job_id in self.stopping,
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
            self.worker_id, self.register.protocol_version, self.epoch, execute=self.register.execute,
            workspace=self.register.workspace, repo_workspace_limit=self.register.repo_workspace_limit,
        )

    async def on_opened(self) -> None:
        self.receive_deadline = asyncio.get_running_loop().time() + self.heartbeat_timeout
        await self._inventory(self.register)

    async def _inventory(self, page: InventoryPage) -> None:
        """Register bounded pages without treating a missing item as release evidence."""
        registering = not self.registered
        if not registering and page.recoveries:
            raise ProtocolViolation("recovery is only allowed during registration")
        for item in page.inventory:
            if registering:
                if item.workspace in self._seen_workspaces:
                    raise ProtocolViolation("duplicate workspace in registration")
                self._seen_workspaces.add(item.workspace)
            if not self.workers.report_workspace(self.worker_id, self.epoch, item):
                raise ProtocolViolation("inventory conflicts with workspace ownership")
            if item.settled_job_id is not None:
                for identity, reply in list(self._sent.items()):
                    if isinstance(reply, StopAccepted) and item.flow_id is not None:
                        # A retained result witness cannot acknowledge a later stop
                        # for the same job/token; stop cleanup must relinquish ownership.
                        continue
                    if isinstance(reply, (AcceptResultResponse, StopAccepted)) and (
                        reply.result.job_id, reply.result.assignment_token,
                    ) == (item.settled_job_id, item.settled_assignment_token):
                        self._sent.pop(identity)
        if not registering:
            self.notify_jobs()
            return
        decisions = []
        pending = {intent.job_id: intent for intent in self.stops.pending_for_worker(self.worker_id)}
        for recovery in page.recoveries:
            if recovery.job_id in self._seen_recoveries:
                raise ProtocolViolation("duplicate execution in registration")
            self._seen_recoveries.add(recovery.job_id)
            stop = pending.get(recovery.job_id)
            if stop is None and recovery.stop_request_id is not None:
                stop = self.stops.receipt_request(self.worker_id, recovery.stop_request_id)
            if stop is not None:
                await self._begin_stop(stop)
            else:
                decision = await self._recover(recovery)
                if decision is not None:
                    decisions.append(decision)
        if page.inventory_complete:
            if self.register.workspace not in self._seen_workspaces:
                raise ProtocolViolation("default inference workspace was not reported")
            for workspace in self.workers.list_workspaces(self.worker_id):
                job_id = workspace.current_job_id
                if job_id is not None and job_id not in self._seen_recoveries:
                    await self._reject_missing_journal(job_id)
            self.registered = True
            self.workers.worker_ready(self.worker_id, self.epoch, self.register.inference)
            self.notify_jobs()
        await self.session.send(Registered(
            **message_header(self.epoch), control="registered", payload=[],
            recoveries=decisions, ready=self.registered, heartbeat_interval=self.heartbeat_interval,
        ))

    def _matches_current(self, job_id: int, token: str) -> bool:
        current = self.current.get(job_id)
        return current is not None and hmac.compare_digest(current[1], token)

    def _validate_active(self, job_id: int, token: str) -> None:
        stop = self.stopping.get(job_id)
        if stop is not None:
            if not hmac.compare_digest(token, stop.assignment_token):
                raise ProtocolViolation("stop reply has stale assignment credentials")
        elif not self._matches_current(job_id, token):
            raise ProtocolViolation("message does not match the active assignment")

    async def _begin_stop(self, intent: StopIntent) -> None:
        if intent.job_id in self.stopping:
            return
        self.stopping[intent.job_id] = intent
        for job_id, (job, token, task) in list(self.preparing.items()):
            if job.flow_id == intent.flow_id:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.preparing.pop(job_id)
                self.workers.requeue_assignment(job.id, self.worker_id, token, self.epoch)
                self.notify_jobs()
        fence = intent.fence()
        await self._send(
            StopExecution(**call_header(stop_call_id(fence)), method="worker.stop", arguments=fence)
        )
        self.receive_deadline = asyncio.get_running_loop().time() + self.heartbeat_timeout

    async def poll(self) -> float | None:
        """Advance scheduling once; return an absolute receive/idle deadline to Session."""
        assert self.worker is not None
        if not self.registered:
            return self.receive_deadline
        for stop in self.stops.pending_for_worker(self.worker_id):
            if stop.job_id not in self.starting:
                await self._begin_stop(stop)
        for job_id, (job, token, task) in list(self.preparing.items()):
            if task.done():
                self.preparing.pop(job_id)
                await self._on_prepared(job, token, task.result())
        if self.register.execute:
            for repository in self.workers.workspace_demand(self.worker_id, self.epoch)[:MAX_PAYLOAD_ITEMS]:
                await self._send(RepositoryDemand(
                    **call_header(), method="worker.provision", repository=repository,
                ))
            for _ in range(MAX_PAYLOAD_ITEMS):
                token = uuid4().hex
                claimed = self.workers.assign_job(self.worker_id, token, self.epoch)
                if claimed is None:
                    break
                task = asyncio.create_task(self._prepare_assignment(claimed, token))
                self.preparing[claimed.id] = claimed, token, task
                task.add_done_callback(self._preparation_ready)
            else:
                self.session.wake()
        await self._poll_inference()
        now = asyncio.get_running_loop().time()
        if not self.current and self.inference_current is None and not self.stopping:
            return now + self.poll_interval
        if now >= self.receive_deadline:
            raise TimeoutError("worker heartbeat timeout")
        return min(self.receive_deadline, now + self.poll_interval)

    def _preparation_ready(self, task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()
        self.session.wake()

    async def aclose(self) -> None:
        tasks = [task for _, _, task in self.preparing.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_inference(self) -> None:
        """The same driver claims a second slot; provider work never runs in this callback."""
        current = self.inference_current
        if current is not None:
            stored = self.jobs.get_job(current.id)
            if stored is not None and stored.state is not JobState.RUNNING:
                assert current.inference is not None and current.inference.current_task is not None
                self.routes.forget_reply(current.inference.current_task.request_id)
                self.inference_current, self.inference_cancelling = None, False
            elif not self.inference_cancelling and self.workers.inference_needs_cancel(current.id):
                assert current.assignment_token is not None
                await self.session.send(CancelJob(
                    **message_header(self.epoch), control="cancel_job", payload=[],
                    job_id=current.id, assignment_token=current.assignment_token,
                ))
                self.inference_cancelling = True
        if self.inference_current is None and self.register.inference is not None:
            token = uuid4().hex
            job = self.workers.assign_job(self.worker_id, token, self.epoch, slot="infer")
            if job is not None:
                assert job.inference is not None and job.inference.current_task is not None
                task = job.inference.current_task
                self.inference_current = job
                await self._send(InferenceRequest(
                    **call_header(task.request_id), method="worker.infer", arguments=task,
                    job_id=job.id, assignment_token=token,
                ))
                if not self.current and not self.stopping:
                    self.receive_deadline = asyncio.get_running_loop().time() + self.heartbeat_timeout

    def _inference_heartbeat(self, message: WorkerMessageUnion | RejectedMessage) -> bool:
        current = self.inference_current
        return (isinstance(message, (Heartbeat, HeartbeatHeaders)) and current is not None
                and message.job_id == current.id
                and message.assignment_token == current.assignment_token)

    def _validate_control(self, message: WorkerMessageUnion | RejectedMessage) -> bool:
        """Authorize control; True identifies a completed assignment needing no renewal."""
        if message.connection_epoch != self.epoch or isinstance(message, Register):
            raise ProtocolViolation("message does not match the connection epoch")
        if isinstance(message, (Heartbeat, HeartbeatHeaders)):
            job = self.jobs.get_job(message.job_id)
            if (job is not None and job.state in {JobState.DONE, JobState.FAILED}
                    and job.result_applied_at is not None
                    and job.assigned_worker_id == self.worker_id
                    and job.assignment_token == message.assignment_token
                    and (job.inference is None or job.inference.assignment_epoch == self.epoch)):
                return True  # A queued heartbeat may arrive after its assignment's result.
            if self._inference_heartbeat(message):
                return False
            if message.job_id in self.starting:
                raise ProtocolViolation("assignment must be acknowledged with Started")
            self._validate_active(message.job_id, message.assignment_token)
        return False

    async def on_message(self, message: WorkerMessageUnion) -> None:
        # Independent inference/history and validation errors do not prove coding progress.
        liveness = not self._inference_heartbeat(message) or (not self.current and not self.stopping)
        self._refresh_receive = liveness and message.connection_epoch == self.epoch and (
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
        if self._validate_control(message):
            self._refresh_receive = False  # Old work cannot keep another assignment alive.
            return  # Gateway still validates and dispatches every payload item.
        if isinstance(message, Inventory):
            await self._inventory(message)
            return
        if isinstance(message, Heartbeat) and self._inference_heartbeat(message):
            self.workers.heartbeat_assignment(
                message.job_id, self.worker_id, message.assignment_token, self.epoch,
            )
            return
        if (
            isinstance(message, Heartbeat)
            and message.job_id not in self.stopping
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
        job_id = self._item_job(item)
        if job_id in self.starting and not isinstance(item, Started):
            raise ProtocolViolation("assignment must be acknowledged with Started")
        for stop in self.stops.pending_for_worker(self.worker_id):
            if stop.job_id not in self.starting:
                await self._begin_stop(stop)

    @staticmethod
    def _item_job(item: ToolCall | ToolResult) -> int | None:
        if isinstance(item, (Started, AcceptResultRequest, AcceptStop, FlowToolRequest)):
            return item.arguments.job_id
        if isinstance(item, ExecuteResult):
            return item.job_id
        if isinstance(item, StopResult):
            return item.result.job_id
        return None

    def on_message_done(self) -> None:
        if self._recovery_required:
            raise ProtocolViolation("critical protocol payload rejected; recovery required")
        if self._refresh_receive:
            self.receive_deadline = asyncio.get_running_loop().time() + self.heartbeat_timeout

    async def on_started(self, item: Started, message: WorkerMessageUnion) -> None:
        job_id = item.arguments.job_id
        if job_id not in self.starting or job_id not in self.current or job_id in self.stopping:
            raise ProtocolViolation("unexpected Started")
        self._validate_active(item.arguments.job_id, item.arguments.assignment_token)
        job, token, task = self.current[job_id]
        assert task is not None
        if not self.workers.start_assignment(
            job.id, task.attempt_id, self.worker_id, token, self.epoch
        ):
            raise ProtocolViolation("assignment is no longer active")
        self.coordinator.mark_started(job, task, self.worker_id)
        self.starting.discard(job_id)

    async def on_result(self, item: ExecuteResult, message: WorkerMessageUnion) -> None:
        if item.job_id in self.stopping:
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
                job.id not in self.stopping
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

    async def on_inference_result(self, item: InferenceResult,
                                  message: WorkerMessageUnion | RejectedMessage) -> None:
        current = self.inference_current
        if (current is None or current.id != item.job_id
                or current.assignment_token != item.assignment_token
                or current.inference is None or current.inference.current_task is None
                or current.inference.current_task.request_id != item.reply_to):
            return  # A late or foreign result cannot complete or release another slot.
        receipt = self.workers.record_assignment_result(
            item.job_id, self.worker_id, item.assignment_token, self.epoch, item,
        )
        if receipt in {"conflict", "rejected"}:
            return
        await self.results.apply(item.job_id)
        self.routes.forget_reply(item.reply_to)
        self.inference_current, self.inference_cancelling = None, False
        self.notify_jobs()

    async def on_accept_result(self, item: AcceptResultRequest, message: WorkerMessageUnion) -> None:
        if item.arguments.job_id in self.stopping:
            self._validate_active(item.arguments.job_id, item.arguments.assignment_token)
        job = self._validate_result_call(item)
        if job.id in self.stopping:
            return
        if job.result_applied_at is None:
            if not self._matches_current(job.id, item.arguments.assignment_token):
                raise ProtocolViolation("unapplied result is not the current assignment")
            await self.results.apply(job.id)
        if await self._accept_result(item):
            if self._matches_current(job.id, item.arguments.assignment_token):
                self.routes.forget_reply(execution_call_id(job.id, item.arguments.assignment_token))
                self.current.pop(job.id)
            self.notify_jobs()

    async def on_stopped(self, item: StopResult, message: WorkerMessageUnion) -> None:
        receipt = item.result
        stop = self.stopping.get(receipt.job_id)
        if stop is None:
            raise ProtocolViolation("unexpected stop receipt")
        fence = stop.fence()
        if (
            item.reply_to != stop_call_id(fence)
            or item.message_id != stop_receipt_id(receipt)
            or (receipt.request_id, receipt.flow_id, receipt.job_id, receipt.assignment_token)
            != (fence.request_id, fence.flow_id, fence.job_id, fence.assignment_token)
            or not self.stops.confirm(self.worker_id, receipt, self.epoch)
        ):
            raise ProtocolViolation("stop blocked: receipt or external effects not converged")
        self.stopped[receipt.job_id] = item

    async def on_accept_stop(self, item: AcceptStop, message: WorkerMessageUnion) -> None:
        args = item.arguments
        stop, stopped = self.stopping.get(args.job_id), self.stopped.get(args.job_id)
        if stop is None or stopped is None:
            raise ProtocolViolation("stop acceptance requires an actual validated receipt")
        fence = stop.fence()
        if (
            args.receipt_id != stopped.message_id
            or item.message_id != accept_stop_id(stopped.message_id)
            or (args.request_id, args.flow_id, args.job_id, args.assignment_token)
            != (fence.request_id, fence.flow_id, fence.job_id, fence.assignment_token)
            or not self.stops.confirm(self.worker_id, stopped.result, self.epoch)
        ):
            raise ProtocolViolation("stop acceptance conflicts with persisted facts")
        if await self._send(
            StopAccepted(
                **result_header(item.message_id), method="server.accept_stop", result=fence
            )
        ):
            if self._matches_current(fence.job_id, fence.assignment_token):
                self.routes.forget_reply(execution_call_id(fence.job_id, fence.assignment_token))
                self.current.pop(fence.job_id)
            self.routes.forget_reply(stop_call_id(fence))
            self.stopping.pop(fence.job_id)
            self.stopped.pop(fence.job_id)
            self.starting.discard(fence.job_id)
            self.notify_jobs()

    async def on_error(self, error: ValidationErrorResult,
                       message: WorkerMessageUnion | RejectedMessage) -> None:
        if message.connection_epoch == self.epoch:
            inference = self.inference_current
            if (inference is not None and inference.inference is not None
                    and inference.inference.current_task is not None
                    and error.method == "worker.infer"
                    and error.reply_to == inference.inference.current_task.request_id):
                assert inference.assignment_token is not None
                await self.on_inference_result(InferenceResult(
                    **result_header(error.reply_to), method="worker.infer",
                    job_id=inference.id, assignment_token=inference.assignment_token,
                    failure=InferenceFailure(code="invalid_response"),
                ), message)
                return
            for job, token, _ in list(self.current.values()):
                if (
                    error.reply_to == execution_call_id(job.id, token)
                    and error.method == "worker.execute"
                    and job.id in self.starting
                ):
                    if job.id in self.stopping:
                        return  # No journal exists to supply R; preserve the safe blocked stop.
                    if not self.workers.reject_assignment(
                        job.id, self.worker_id, token, self.epoch
                    ):
                        stop = next((intent for intent in self.stops.pending_for_worker(self.worker_id)
                                     if intent.job_id == job.id), None)
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
                    self.current.pop(job.id)
                    self.starting.discard(job.id)
                    self.notify_jobs()
                    return
            sent = next(
                (item for item in self._sent.values() if item.message_id == error.reply_to), None
            )
            if (sent is not None and error.method == sent.method) or (
                error.method == "worker.stop"
                and any(error.reply_to == stop_call_id(stop.fence()) for stop in self.stopping.values())
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
            # Job replies have no temporary Future to reject. Persist the authorized
            # failure now so malformed answers cannot turn into retryable timeouts.
            for item in replies:
                if item.method == "worker.infer":
                    await self.on_error(ValidationErrorResult(
                        **result_header(item.reply_to), method=item.method,
                        error=PayloadValidationError(
                            code="validation_error", message="message payload failed validation",
                        ),
                    ), message)
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
        self.workers.worker_disconnected(
            self.worker_id, self.disconnect_reason, connection_epoch=self.epoch
        )

    async def _send(self, item: ServerPayload) -> bool:
        previous = self._sent.get(item.message_id)
        tracked = isinstance(item, (AcceptResultResponse, StopAccepted, RecoveryRejected))
        if isinstance(item, (AcceptResultResponse, StopAccepted, RecoveryRejected)):
            self._sent[item.message_id] = item
        if isinstance(item, Assignment):
            self.routes.expect_reply(item, ExecuteResult, self.on_result)
        elif isinstance(item, StopExecution):
            self.routes.expect_reply(item, StopResult, self.on_stopped)
        elif isinstance(item, InferenceRequest):
            self.routes.expect_reply(item, InferenceResult, self.on_inference_result)
        try:
            return await self.session.send(
                ServerMessage(**message_header(self.epoch), control=None, payload=[item])
            )
        except BaseException:
            if isinstance(item, (Assignment, StopExecution, InferenceRequest)):
                self.routes.forget_reply(item.message_id)
            if tracked:
                if previous is None:
                    self._sent.pop(item.message_id, None)
                else:
                    self._sent[item.message_id] = previous
            raise

    async def _on_prepared(self, job: Job, token: str,
                           prepared: AssignmentArguments | PreparationState) -> None:
        """Resume on the serial driver; preparing never authorizes worker execution."""
        if isinstance(prepared, JobState):
            if prepared is JobState.QUEUED:
                self.workers.requeue_assignment(job.id, self.worker_id, token, self.epoch)
            else:
                self.workers.finish_assignment(job.id, self.worker_id, token, prepared, self.epoch)
            self.notify_jobs()
            return
        if not self.stops.authorize_dispatch(job.id, self.worker_id, token, self.epoch):
            raise ProtocolViolation("assignment changed before dispatch authorization")
        if (prepared.workspace, prepared.generation) != (job.workspace, job.generation):
            raise ProtocolViolation("preparation changed the claimed workspace")
        self.current[job.id] = job, token, prepared.task
        self.starting.add(job.id)
        await self._send(
            Assignment(
                **call_header(execution_call_id(job.id, token)),
                method="worker.execute",
                arguments=prepared,
            )
        )
        self.receive_deadline = asyncio.get_running_loop().time() + self.heartbeat_timeout

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
        if inbox.source != "worker" or not isinstance(inbox.terminal, (Succeeded, Failed)):
            raise ProtocolViolation("result acceptance requires a remote execution result")
        if self.workers.record_assignment_result(
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

    async def _recover(self, recovery: RecoveryDescriptor) -> RecoveryDecision | None:
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
            self._stored_result(job.id, recovery.assignment_token)
            self.current[job.id] = job, recovery.assignment_token, None
            return RecoveryDecision(job_id=job.id, assignment_token=recovery.assignment_token, action="replay")
        if recovery.state == "received":
            requeued = self.workers.requeue_assignment(
                job.id, self.worker_id, recovery.assignment_token, self.epoch
            )
            reason = "unstarted assignment abandoned"
            retired_unstarted = (job.state in {JobState.DONE, JobState.FAILED}
                                 and job.started_at is None and job.dispatch_authorized_at is None)
            if not requeued and not retired_unstarted:
                logger.warning("retaining dispatched job {} with incomplete start evidence", job.id)
                return None
            await reject(reason)
            self.notify_jobs()
            return None
        if (
            recovery.state == "started"
            and not recovery.executor_active
            and not (job.kind is JobKind.ARCHIVE and recovery.archive_resume)
        ):
            logger.warning("retaining job {}: original executor exit cannot be confirmed", job.id)
            return None
        if not self.workers.resume_assignment(
            job.id, self.worker_id, recovery.assignment_token, self.epoch
        ):
            await reject("recovery deadline expired")
            return None
        self.current[job.id] = job, recovery.assignment_token, None
        self.routes.expect_reply(
            ToolCall(**call_header(execution_call_id(job.id, recovery.assignment_token)), method="worker.execute"),
            ExecuteResult, self.on_result,
        )
        return RecoveryDecision(
            job_id=job.id, assignment_token=recovery.assignment_token,
            action="replay" if recovery.state in {"succeeded", "failed"} else "resume",
        )

    async def _reject_missing_journal(self, job_id: int) -> None:
        job = self.jobs.get_job(job_id)
        if job is None or job.assignment_token is None:
            return
        if job.started_at is None and self.workers.requeue_assignment(
            job.id, self.worker_id, job.assignment_token, self.epoch
        ):
            self.notify_jobs()
            return
        logger.warning("retaining workspace for job {}: original journal is missing", job.id)
