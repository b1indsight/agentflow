"""Server-owned worker scheduling, recovery and durable protocol effects."""

from __future__ import annotations

import asyncio
import hmac
from uuid import uuid4

from fastapi import WebSocketDisconnect
from loguru import logger
from pydantic import ValidationError

from app.communication.dispatch import Dispatcher
from app.communication.gateway import Gateway, Registration
from app.communication.session import Session, SessionOverflow
from app.contracts.protocol import (
    AcceptedResult,
    AcceptResult,
    AcceptStop,
    Assignment,
    AssignmentArguments,
    ExecuteResult,
    ExecutionTask,
    FlowToolRequest,
    Heartbeat,
    ProtocolError,
    RecoveryDecision,
    RecoveryRejected,
    RecoveryRejectedArguments,
    Register,
    Registered,
    ResultAccepted,
    ServerFrame,
    ServerMessage,
    ServerPayload,
    Started,
    StopAccepted,
    StopExecution,
    StopResult,
    TerminalPayload,
    WorkerMessage,
    WorkerPayload,
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
from app.contracts.receipts import decode_result_inbox
from app.contracts.result_models import ResultPending
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.preparation import PreparationStale, PreparationState
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.tools import FlowToolService
from app.domain.models import (
    DeliveryState,
    Job,
    JobKind,
    JobState,
)
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.jobs import JobStore
from app.persistence.stops import StopIntent, StopStore
from app.persistence.workers import WorkerStore
from app.telemetry.metrics import Metrics


class ProtocolViolation(RuntimeError):
    pass


class WorkerHandler:
    """Drive business on authenticated sessions; delegate communication to Gateway."""

    def __init__(
        self,
        coordinator: JobCoordinator,
        *,
        peers: Gateway[WorkerMessage, ServerMessage],
        heartbeat_interval: float = 15.0,
        heartbeat_timeout: float = 45.0,
        poll_interval: float = 2.0,
        metrics: Metrics | None = None,
        database: Database,
        tools: FlowToolService,
        results: ResultApplication,
    ) -> None:
        self.deliveries = DeliveryStore(database)
        self.jobs = JobStore(database)
        self.stops = StopStore(database)
        self.workers = WorkerStore(database)
        self.metrics = metrics
        self.coordinator = coordinator
        self.tools = tools
        self.results = results
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.poll_interval = poll_interval
        self._peers = peers
        self._wakes: dict[Registration[WorkerMessage, ServerMessage], asyncio.Event] = {}

    async def _prepare_assignment(
        self,
        job: Job,
        worker_id: str,
        token: str,
        epoch: str,
    ) -> AssignmentArguments | PreparationState:
        """Keep the unstarted lease alive across both context and capability I/O; fence dispatch."""

        async def build() -> AssignmentArguments | PreparationState:
            try:
                return await self.coordinator.prepare_for_assignment(job, worker_id, token, epoch)
            except PreparationStale as error:
                raise ProtocolViolation(str(error)) from error

        preparing = asyncio.create_task(build())
        try:
            while True:
                if not self.workers.heartbeat_assignment(job.id, worker_id, token, epoch):
                    raise ProtocolViolation("assignment expired during preparation")
                done, _ = await asyncio.wait({preparing}, timeout=self.heartbeat_interval)
                if done:
                    return preparing.result()
        finally:
            preparing.cancel()
            await asyncio.gather(preparing, return_exceptions=True)

    async def handle(
        self,
        worker_id: str,
        register: Register,
        connection: Session[WorkerMessage, ServerMessage],
    ) -> None:
        """Take ownership immediately, including cleanup if admission never succeeds."""
        current: tuple[Job, str, ExecutionTask | None] | None = None
        connected = False
        close_code = 1000
        connection_epoch = connection.epoch
        registration: Registration[WorkerMessage, ServerMessage] | None = None
        disconnect_reason: str | None = None
        try:
            if connection_epoch is None:
                raise RuntimeError("server session requires a connection epoch")
            epoch = connection_epoch
            registration = await self._peers.register(worker_id, connection, replace=True)
            wake = self._wakes[registration] = asyncio.Event()
            # No await after publication: a superseded candidate cannot update DB presence.
            worker = self.workers.worker_connected(worker_id, register.protocol_version, epoch)
            connected = True
            starting = False
            stopping: StopIntent | None = None
            stopped: StopResult | None = None
            stop_deadline: float | None = None
            receive_deadline = asyncio.get_running_loop().time() + self.heartbeat_timeout

            def matches_current(job_id: int, token: str) -> bool:
                return (
                    current is not None
                    and current[0].id == job_id
                    and hmac.compare_digest(current[1], token)
                )

            def validate_active(job_id: int, token: str) -> None:
                if stopping is not None:
                    if (job_id, token) != (stopping.job_id, stopping.assignment_token):
                        raise ProtocolViolation("stop reply has stale assignment credentials")
                elif not matches_current(job_id, token):
                    raise ProtocolViolation("message does not match the active assignment")

            async def begin_stop(intent: StopIntent) -> None:
                nonlocal stopping, stopped, stop_deadline, receive_deadline
                stopping, stopped = intent, None
                fence = intent.fence()
                await self._send(
                    connection,
                    epoch,
                    StopExecution(
                        **call_header(stop_call_id(fence)),
                        method="worker.stop",
                        arguments=fence,
                    ),
                )
                stop_deadline = asyncio.get_running_loop().time() + 20
                receive_deadline = asyncio.get_running_loop().time() + self.heartbeat_timeout

            async def on_started(message: Started) -> None:
                nonlocal starting
                if not starting or current is None or stopping is not None:
                    raise ProtocolViolation("unexpected Started")
                validate_active(message.arguments.job_id, message.arguments.assignment_token)
                job, token, task = current
                assert task is not None
                if not self.workers.start_assignment(
                    job.id, task.attempt_id, worker_id, token, epoch
                ):
                    raise ProtocolViolation("assignment is no longer active")
                self.coordinator.mark_started(job, task, worker_id)
                starting = False

            async def on_tool(message: FlowToolRequest) -> None:
                args = message.arguments
                validate_active(args.job_id, args.assignment_token)
                job = self.jobs.get_job(args.job_id)
                if job is None or args.flow_id != job.flow_id:
                    raise ProtocolViolation("Flow tool call belongs to another Flow")
                if stopping is not None:
                    return  # In-flight calls cannot start new effects while draining.
                response = await self.tools.execute(
                    message,
                    worker_id=worker_id,
                    connection_epoch=epoch,
                )
                await self._send(connection, epoch, response)

            async def on_result(message: ExecuteResult) -> None:
                if stopping is not None:
                    validate_active(message.job_id, message.assignment_token)
                terminal = message.terminal()
                if message.reply_to != execution_call_id(
                    message.job_id, message.assignment_token
                ) or message.message_id != terminal_id(terminal):
                    raise ProtocolViolation("terminal has invalid execution/result identity")
                job = self.jobs.get_job(message.job_id)
                if (
                    job is None
                    or job.assigned_worker_id != worker_id
                    or job.assignment_token != message.assignment_token
                    or (
                        stopping is None
                        and not matches_current(job.id, message.assignment_token)
                        and job.result_payload is None
                    )
                ):
                    raise ProtocolViolation("result belongs to another assignment")
                receipt = self.workers.record_assignment_result(
                    job.id,
                    worker_id,
                    message.assignment_token,
                    epoch,
                    terminal,
                )
                if receipt in {"conflict", "rejected"}:
                    raise ProtocolViolation("result conflicts with persisted assignment facts")
                await self.results.apply(job.id)
                # T alone never retires an assignment or manufactures an A response.

            async def on_accept_result(message: AcceptResult) -> None:
                nonlocal current
                args = message.arguments
                if stopping is not None:
                    validate_active(args.job_id, args.assignment_token)
                job = self._validate_result_call(message, worker_id, epoch)
                if stopping is not None:
                    return  # Only B can acknowledge the stop; A cannot start more effects.
                if job.result_applied_at is None:
                    if not matches_current(job.id, args.assignment_token):
                        raise ProtocolViolation("unapplied result is not the current assignment")
                    await self.results.apply(job.id)
                if await self._accept_result(connection, epoch, message):
                    if matches_current(job.id, args.assignment_token):
                        current = None
                    self.notify_jobs()

            async def on_stopped(message: StopResult) -> None:
                nonlocal stopped
                receipt = message.result
                if stopping is None:
                    raise ProtocolViolation("unexpected stop receipt")
                fence = stopping.fence()
                if (
                    message.reply_to != stop_call_id(fence)
                    or message.message_id != stop_receipt_id(receipt)
                    or (
                        receipt.request_id,
                        receipt.flow_id,
                        receipt.job_id,
                        receipt.assignment_token,
                    )
                    != (fence.request_id, fence.flow_id, fence.job_id, fence.assignment_token)
                    or not self.stops.confirm(worker_id, receipt, epoch)
                ):
                    raise ProtocolViolation(
                        "stop blocked: receipt or external effects not converged"
                    )
                stopped = message

            async def on_accept_stop(message: AcceptStop) -> None:
                nonlocal current, stopping, stopped, stop_deadline, starting
                args = message.arguments
                if stopping is None or stopped is None:
                    raise ProtocolViolation("stop acceptance requires an actual validated receipt")
                fence = stopping.fence()
                if (
                    args.receipt_id != stopped.message_id
                    or message.message_id != accept_stop_id(stopped.message_id)
                    or (args.request_id, args.flow_id, args.job_id, args.assignment_token)
                    != (fence.request_id, fence.flow_id, fence.job_id, fence.assignment_token)
                    or not self.stops.confirm(worker_id, stopped.result, epoch)
                ):
                    raise ProtocolViolation("stop acceptance conflicts with persisted facts")
                if await self._send(
                    connection,
                    epoch,
                    StopAccepted(
                        **result_header(message.message_id),
                        method="server.accept_stop",
                        result=fence,
                    ),
                ):
                    if matches_current(fence.job_id, fence.assignment_token):
                        current = None
                    stopping, stopped, stop_deadline, starting = None, None, None, False
                    self.notify_jobs()

            dispatch = Dispatcher[WorkerPayload]()
            dispatch.register("tool_call", "server.execution_started", Started, on_started)
            dispatch.register("tool_call", "*", FlowToolRequest, on_tool)
            dispatch.register("tool_result", "worker.execute", ExecuteResult, on_result)
            dispatch.register("tool_call", "server.accept_result", AcceptResult, on_accept_result)
            dispatch.register("tool_result", "worker.stop", StopResult, on_stopped)
            dispatch.register("tool_call", "server.accept_stop", AcceptStop, on_accept_stop)

            initial_stop = self.stops.pending(worker_id)
            if (
                initial_stop is None
                and register.recovery is not None
                and register.recovery.stop_request_id is not None
            ):
                initial_stop = self.stops.receipt_request(
                    worker_id, register.recovery.stop_request_id
                )
            if initial_stop is not None:
                await begin_stop(initial_stop)
            elif register.recovery is not None:
                current = await self._recover(connection, worker_id, epoch, register)
            registered = current is not None  # Recovery already sent its Registered decision.

            while True:
                wake.clear()
                if not registered and stopping is None:
                    stale_job_id = worker.current_job_id
                    if initial_stop is not None and stale_job_id == initial_stop.job_id:
                        stale_job_id = None
                    if stale_job_id is not None and (
                        register.recovery is None or register.recovery.job_id != stale_job_id
                    ):
                        # A lost dispatch can leave the preceding acknowledged journal behind it.
                        await self._reject_missing_journal(worker_id, epoch, stale_job_id)
                    elif (
                        register.recovery is None
                        and (binding := self.workers.get_worker_execution(worker_id)) is not None
                    ):
                        delivery = self.deliveries.get_delivery(binding.flow_id)
                        if delivery is not None and delivery.state is DeliveryState.MERGED:
                            raise ProtocolViolation(
                                "archive blocked: original worker journal is missing"
                            )
                    await connection.send(
                        Registered(
                            **frame_header(epoch),
                            control="registered",
                            payload=[],
                            recovery=None,
                            heartbeat_interval=self.heartbeat_interval,
                        )
                    )
                    registered = True

                if stopping is None and not starting:
                    stop = self.stops.pending(worker_id)
                    if stop is not None:
                        await begin_stop(stop)
                if current is None and stopping is None:
                    token = uuid4().hex
                    job = self.workers.assign_job(worker_id, token, epoch)
                    if job is not None:
                        prepared = await self._prepare_assignment(job, worker_id, token, epoch)
                        if isinstance(prepared, JobState):
                            if prepared is JobState.QUEUED:
                                self.workers.requeue_assignment(job.id, worker_id, token, epoch)
                            else:
                                self.workers.finish_assignment(
                                    job.id, worker_id, token, prepared, epoch
                                )
                            self.notify_jobs()
                            continue
                        if not self.stops.authorize_dispatch(job.id, worker_id, token, epoch):
                            raise ProtocolViolation(
                                "assignment changed before dispatch authorization"
                            )
                        current, starting = (job, token, prepared.task), True
                        await self._send(
                            connection,
                            epoch,
                            Assignment(
                                **call_header(execution_call_id(job.id, token)),
                                method="worker.execute",
                                arguments=prepared,
                            ),
                        )
                        receive_deadline = (
                            asyncio.get_running_loop().time() + self.heartbeat_timeout
                        )

                async with asyncio.timeout_at(stop_deadline) as deadline:
                    if current is None and stopping is None:
                        timeout = self.poll_interval
                    else:
                        timeout = receive_deadline - asyncio.get_running_loop().time()
                        if timeout <= 0:
                            raise TimeoutError("worker heartbeat timeout")
                    frame = await self._wait_for_message(connection, wake, timeout)
                    if frame is None:
                        continue
                    if frame.connection_epoch != epoch or isinstance(frame, Register):
                        raise ProtocolViolation("message does not match the connection epoch")
                    if isinstance(frame, Heartbeat):
                        if starting:
                            raise ProtocolViolation("assignment must be acknowledged with Started")
                        validate_active(frame.job_id, frame.assignment_token)
                        if stopping is None and not self.workers.heartbeat_assignment(
                            frame.job_id,
                            worker_id,
                            frame.assignment_token,
                            epoch,
                        ):
                            # T may already be committed while A is still in flight. The
                            # current tuple remains authorized, but its lease must not renew.
                            applied, _ = self._stored_result(
                                frame.job_id, worker_id, frame.assignment_token, epoch
                            )
                            if applied.result_applied_at is None or applied.state not in {
                                JobState.DONE,
                                JobState.FAILED,
                            }:
                                raise ProtocolViolation(
                                    "heartbeat belongs to an inactive assignment"
                                )
                    # The complete control is authorized before any item, including history bypass.
                    for item in frame.payload:
                        if frame.control is not None and connection.dispatch_item(item, frame):
                            continue
                        if starting and not isinstance(item, Started):
                            raise ProtocolViolation("assignment must be acknowledged with Started")
                        if not starting and stopping is None:
                            stop = self.stops.pending(worker_id)
                            if stop is not None:
                                await begin_stop(stop)
                                deadline.reschedule(stop_deadline)
                        await dispatch.dispatch(item)
                        deadline.reschedule(stop_deadline)
                    receive_deadline = asyncio.get_running_loop().time() + self.heartbeat_timeout
        except ResultPending as exc:
            disconnect_reason = str(exc)
            # The worker retains its journal and reconnects; recovery owns the accepted result.
            close_code = 1013
        except (WebSocketDisconnect, ConnectionError, TimeoutError) as exc:
            disconnect_reason = str(exc) or type(exc).__name__
            logger.bind(
                event="worker_disconnected", reason_code="connection_lost", worker_id=worker_id
            ).opt(exception=exc).info("")
        except (ProtocolViolation, SessionOverflow, ValidationError) as exc:
            invalid = isinstance(exc, ValidationError)
            disconnect_reason = "message failed protocol validation" if invalid else str(exc)
            close_code = 1008
            logger.warning("worker protocol violation: {}", disconnect_reason)
            try:
                error = ProtocolError(
                    **frame_header(connection_epoch),
                    control="error",
                    payload=[],
                    code="invalid_message" if invalid else "protocol_violation",
                    detail=disconnect_reason,
                )
                await connection.send(error)
            except (WebSocketDisconnect, ConnectionError):
                pass
        finally:
            try:
                if registration is not None:
                    self._wakes.pop(registration, None)
                    await self._peers.unregister(registration, close_code)
                else:
                    await connection.close(close_code)
            finally:
                if connected:
                    assert connection_epoch is not None
                    self.workers.worker_disconnected(
                        worker_id,
                        disconnect_reason,
                        connection_epoch=connection_epoch,
                    )

    async def _wait_for_message(
        self,
        connection: Session[WorkerMessage, ServerMessage],
        wake: asyncio.Event,
        timeout: float,
    ) -> WorkerMessage | None:
        """Wake for stops even while waiting for A, without extending the business deadline."""
        receive = asyncio.create_task(connection.receive())
        notified = asyncio.create_task(wake.wait())
        try:
            done, _ = await asyncio.wait(
                {receive, notified},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receive in done:
                return receive.result()
            return None
        finally:
            receive.cancel()
            notified.cancel()
            await asyncio.gather(receive, notified, return_exceptions=True)

    def notify_jobs(self) -> None:
        """Wake live sessions after an enqueue, stop or slot-state transition."""
        for registration, wake in self._wakes.items():
            if self._peers.get(registration.peer_id) is registration.session:
                wake.set()

    @staticmethod
    async def _send(
        connection: Session[WorkerMessage, ServerMessage],
        epoch: str,
        item: ServerPayload,
    ) -> bool:
        return await connection.send(
            ServerFrame(**frame_header(epoch), control=None, payload=[item])
        )

    def _validate_result_call(self, call: AcceptResult, worker_id: str, epoch: str) -> Job:
        """Validate A against the real durable inbox, not just its deterministic ID."""
        args = call.arguments
        job, terminal = self._stored_result(args.job_id, worker_id, args.assignment_token, epoch)
        result_id = terminal_id(terminal)
        if args.result_id != result_id or call.message_id != accept_result_id(result_id):
            raise ProtocolViolation("result acceptance has invalid result identity")
        return job

    def _stored_result(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        epoch: str,
    ) -> tuple[Job, TerminalPayload]:
        job = self.jobs.get_job(job_id)
        if (
            job is None
            or job.assigned_worker_id != worker_id
            or job.assignment_token != token
            or job.result_payload is None
            or job.result_digest is None
        ):
            raise ProtocolViolation("result acceptance has no matching durable result")
        try:
            inbox = decode_result_inbox(
                job.result_payload,
                job_id=job.id,
                assignment_token=job.assignment_token,
                result_digest=job.result_digest,
            )
        except ValueError as error:
            raise ProtocolViolation("result acceptance conflicts with durable inbox") from error
        if inbox.source != "worker" or self.workers.record_assignment_result(
            job.id,
            worker_id,
            token,
            epoch,
            inbox.terminal,
        ) in {"conflict", "rejected"}:
            raise ProtocolViolation("result acceptance has invalid assignment/result identity")
        return job, inbox.terminal

    async def _accept_result(
        self,
        connection: Session[WorkerMessage, ServerMessage],
        epoch: str,
        call: AcceptResult,
    ) -> bool:
        job = self.jobs.get_job(call.arguments.job_id)
        if (
            job is None
            or job.result_applied_at is None
            or job.state not in {JobState.DONE, JobState.FAILED}
        ):
            raise ProtocolViolation("result application has not committed")
        return await self._send(
            connection,
            epoch,
            ResultAccepted(
                **result_header(call.message_id),
                method="server.accept_result",
                result=AcceptedResult(
                    job_id=job.id,
                    assignment_token=call.arguments.assignment_token,
                    state="done" if job.state is JobState.DONE else "failed",
                ),
            ),
        )

    async def _recover(
        self,
        connection: Session[WorkerMessage, ServerMessage],
        worker_id: str,
        connection_epoch: str,
        register: Register,
    ) -> tuple[Job, str, ExecutionTask | None] | None:
        recovery = register.recovery
        assert recovery is not None

        async def reject(reason: str) -> None:
            await self._send(
                connection,
                connection_epoch,
                RecoveryRejected(
                    **call_header(),
                    method="worker.reject_recovery",
                    arguments=RecoveryRejectedArguments(
                        job_id=recovery.job_id,
                        reason=reason,
                        register_message_id=register.message_id,
                    ),
                ),
            )

        job = self.jobs.get_job(recovery.job_id)
        if (
            job is None
            or job.assigned_worker_id != worker_id
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
                worker_id=worker_id,
                epoch=connection_epoch,
            ).info("")
            # A retained acknowledged journal still requires ownership verification, but
            # without an actual A the server must not invent an acceptance response.
            if register.payload:
                call = register.payload[0]
                assert isinstance(call, AcceptResult)  # Register's strict static contract.
                self._validate_result_call(call, worker_id, connection_epoch)
                await self._accept_result(connection, connection_epoch, call)
            else:
                self._stored_result(job.id, worker_id, recovery.assignment_token, connection_epoch)
            return None
        if recovery.state == "received":
            requeued = self.workers.requeue_assignment(
                job.id,
                worker_id,
                recovery.assignment_token,
                connection_epoch,
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
            job.id,
            worker_id,
            recovery.assignment_token,
            connection_epoch,
        ):
            await reject("recovery deadline expired")
            return None
        await connection.send(
            Registered(
                **frame_header(connection_epoch),
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

    async def _reject_missing_journal(
        self, worker_id: str, connection_epoch: str, job_id: int
    ) -> None:
        job = self.jobs.get_job(job_id)
        if job is None or job.assignment_token is None:
            return
        if job.started_at is None and self.workers.requeue_assignment(
            job.id, worker_id, job.assignment_token, connection_epoch
        ):
            self.notify_jobs()
            return
        delivery = self.deliveries.get_delivery(job.flow_id)
        if delivery is not None and delivery.state in {DeliveryState.CLOSED, DeliveryState.MERGED}:
            action = "reopen" if delivery.state is DeliveryState.CLOSED else "archive"
            raise ProtocolViolation(
                f"{action} blocked: original worker assignment journal is missing"
            )
        # Dispatch without Started is still unknown execution: fail through normal recovery, never rerun.
        reason = "worker reconnected without its assignment journal"
        await self.coordinator.fail(job, reason)
