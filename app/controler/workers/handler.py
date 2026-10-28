"""Server-owned worker scheduling, recovery and durable protocol effects."""

from __future__ import annotations

import asyncio
import hmac
from uuid import uuid4

from fastapi import WebSocketDisconnect
from loguru import logger
from pydantic import ValidationError

from app.communication.gateway import Gateway, Registration
from app.communication.session import Session, SessionOverflow
from app.contracts.protocol import (
    Assignment,
    ExecutionStopped,
    ExecutionTask,
    Failed,
    FlowToolRequest,
    Heartbeat,
    ProtocolError,
    RecoveryDescriptor,
    RecoveryRejected,
    Register,
    Registered,
    ReplayResult,
    ResultAccepted,
    ResumeAssignment,
    ServerMessage,
    Started,
    StopAccepted,
    Succeeded,
    WorkerMessage,
    terminal_digest,
)
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
        self, job: Job, worker_id: str, token: str, epoch: str,
    ) -> Assignment | PreparationState:
        """Keep the unstarted lease alive across both context and capability I/O; fence dispatch."""
        async def build() -> Assignment | PreparationState:
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
        self, worker_id: str, register: Register,
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
            registration = await self._peers.register(worker_id, connection, replace=True)
            wake = self._wakes[registration] = asyncio.Event()
            # No await after publication: a superseded candidate cannot update DB presence.
            worker = self.workers.worker_connected(
                worker_id, register.protocol_version, connection_epoch
            )
            connected = True
            stop = self.stops.pending(worker_id)
            if (stop is None and register.recovery is not None
                    and register.recovery.stop_request_id is not None):
                stop = self.stops.receipt_request(worker_id, register.recovery.stop_request_id)
            if stop is not None:
                await self._stop_worker(connection, worker_id, connection_epoch, stop)
            elif register.recovery is not None:
                current = await self._recover(
                    connection,
                    worker_id,
                    connection_epoch,
                    register.recovery,
                )
            if current is None:
                stale_job_id = worker.current_job_id
                if stop is not None and stale_job_id == stop.job_id:
                    stale_job_id = None
                if stale_job_id is not None and (
                    register.recovery is None or register.recovery.job_id != stale_job_id
                ):
                    # A crash before journaling a newly delivered command leaves the preceding
                    # acknowledged receipt. Recover the unstarted delivery behind that receipt.
                    await self._reject_missing_journal(worker_id, connection_epoch, stale_job_id)
                elif register.recovery is None and (
                    binding := self.workers.get_worker_execution(worker_id)
                ) is not None:
                    delivery = self.deliveries.get_delivery(binding.flow_id)
                    if delivery is not None and delivery.state is DeliveryState.MERGED:
                        raise ProtocolViolation("archive blocked: original worker journal is missing")
            if current is None:
                await connection.send(
                    Registered(
                        heartbeat_interval=self.heartbeat_interval,
                        connection_epoch=connection_epoch,
                    )
                )

            while True:
                if current is None:
                    wake.clear()
                    stop = self.stops.pending(worker_id)
                    if stop is not None:
                        await self._stop_worker(connection, worker_id, connection_epoch, stop)
                    token = uuid4().hex
                    job = self.workers.assign_job(worker_id, token, connection_epoch)
                    if job is None:
                        await self._wait_while_idle(connection, wake)
                        continue
                    prepared = await self._prepare_assignment(job, worker_id, token, connection_epoch)
                    if isinstance(prepared, JobState):
                        if prepared is JobState.QUEUED:
                            self.workers.requeue_assignment(job.id, worker_id, token, connection_epoch)
                        else:
                            self.workers.finish_assignment(
                                job.id, worker_id, token, prepared, connection_epoch,
                            )
                        self.notify_jobs()
                        continue
                    if not self.stops.authorize_dispatch(job.id, worker_id, token, connection_epoch):
                        raise ProtocolViolation("assignment changed before dispatch authorization")
                    current = (job, token, prepared.task)
                    await connection.send(prepared)

                    first = await self._receive(connection)
                    if not isinstance(first, Started):
                        raise ProtocolViolation("assignment must be acknowledged with Started")
                    self._validate_assignment_message(
                        first.job_id,
                        first.assignment_token,
                        first.connection_epoch,
                        current,
                        connection_epoch,
                    )
                    if not self.workers.start_assignment(
                        job.id,
                        prepared.task.attempt_id,
                        worker_id,
                        token,
                        connection_epoch,
                    ):
                        raise ProtocolViolation("assignment is no longer active")
                    self.coordinator.mark_started(job, prepared.task, worker_id)

                while True:
                    job, token, _ = current
                    stop = self.stops.pending(worker_id)
                    if stop is not None:
                        await self._stop_worker(connection, worker_id, connection_epoch, stop)
                        current = None
                        break
                    message = await self._receive(connection)
                    if isinstance(message, Heartbeat):
                        self._validate_assignment_message(
                            message.job_id,
                            message.assignment_token,
                            message.connection_epoch,
                            current,
                            connection_epoch,
                        )
                        if not self.workers.heartbeat_assignment(
                            job.id, worker_id, token, connection_epoch
                        ):
                            raise ProtocolViolation("heartbeat belongs to an inactive assignment")
                        continue
                    if isinstance(message, FlowToolRequest):
                        self._validate_assignment_message(
                            message.job_id,
                            message.assignment_token,
                            message.connection_epoch,
                            current,
                            connection_epoch,
                        )
                        if message.flow_id != job.flow_id:
                            raise ProtocolViolation("Flow tool call belongs to another Flow")
                        response = await self.tools.execute(message, worker_id=worker_id)
                        await connection.send(response)
                        continue
                    if isinstance(message, (Succeeded, Failed)):
                        self._validate_assignment_message(
                            message.job_id,
                            message.assignment_token,
                            message.connection_epoch,
                            current,
                            connection_epoch,
                        )
                        receipt = self.workers.record_assignment_result(
                            job.id,
                            worker_id,
                            token,
                            connection_epoch,
                            message.model_dump(mode="json"),
                            terminal_digest(message),
                        )
                        if receipt == "conflict":
                            raise ProtocolViolation("result conflicts with the persisted result")
                        if receipt == "rejected":
                            raise ProtocolViolation("result belongs to an inactive assignment")
                        state = await self.results.apply(job.id)
                        applied = self.jobs.get_job(job.id)
                        if (applied is None or applied.result_applied_at is None
                                or applied.state not in {JobState.DONE, JobState.FAILED}):
                            raise ProtocolViolation("result application has not committed")
                        await connection.send(
                            ResultAccepted(
                                job_id=job.id,
                                state=state,
                                assignment_token=token,
                                connection_epoch=connection_epoch,
                            )
                        )
                        current = None
                        self.notify_jobs()
                        break
                    raise ProtocolViolation(
                        f"unexpected assignment message: {type(message).__name__}"
                    )
        except ResultPending as exc:
            disconnect_reason = str(exc)
            # The worker retains its journal and reconnects; recovery owns the accepted result.
            close_code = 1013
        except (WebSocketDisconnect, ConnectionError, TimeoutError) as exc:
            disconnect_reason = str(exc) or type(exc).__name__
            logger.bind(event="worker_disconnected", reason_code="connection_lost",
                        worker_id=worker_id).opt(exception=exc).info("")
        except (ProtocolViolation, SessionOverflow, ValidationError) as exc:
            invalid = isinstance(exc, ValidationError)
            disconnect_reason = "message failed protocol validation" if invalid else str(exc)
            close_code = 1008
            logger.warning("worker protocol violation: {}", disconnect_reason)
            try:
                error = ProtocolError(
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
                        worker_id, disconnect_reason, connection_epoch=connection_epoch,
                    )

    async def _stop_worker(self, connection: Session[WorkerMessage, ServerMessage], worker_id: str, epoch: str, stop: StopIntent) -> None:
        await connection.send(stop.message(epoch))
        # An in-flight heartbeat/tool/result can precede the stopped receipt on this stream.
        async with asyncio.timeout(20):
            while True:
                message = await self._receive(connection)
                if not isinstance(message, (Heartbeat, FlowToolRequest, Succeeded, Failed, ExecutionStopped)):
                    raise ProtocolViolation("unexpected message while stopping")
                if (message.job_id, message.assignment_token, message.connection_epoch) != (
                        stop.job_id, stop.assignment_token, epoch):
                    raise ProtocolViolation("stop reply has stale assignment credentials")
                if isinstance(message, (Succeeded, Failed)):
                    job = self.jobs.get_job(stop.job_id)
                    if job is None:
                        raise ProtocolViolation("stop result job is missing")
                    receipt = self.workers.record_assignment_result(job.id, worker_id, stop.assignment_token,
                                                                 epoch, message.model_dump(mode="json"), terminal_digest(message))
                    if receipt in {'conflict', 'rejected'}:
                        raise ProtocolViolation("stop result conflicts with persisted facts")
                    if receipt != 'applied':
                        await self.results.apply(job.id)
                if isinstance(message, ExecutionStopped):
                    if message.request_id != stop.request_id or not self.stops.confirm(worker_id, message):
                        raise ProtocolViolation("stop blocked: receipt or external effects not converged")
                    await connection.send(StopAccepted(**stop.message(epoch).model_dump(exclude={'type'})))
                    self.notify_jobs()
                    return

    async def _wait_while_idle(self, connection: Session[WorkerMessage, ServerMessage], wake: asyncio.Event) -> None:
        receive = asyncio.create_task(connection.receive())
        notified = asyncio.create_task(wake.wait())
        try:
            done, _ = await asyncio.wait(
                {receive, notified},
                timeout=self.poll_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receive in done:
                message = receive.result()
                raise ProtocolViolation(f"unexpected idle message: {type(message).__name__}")
        finally:
            receive.cancel()
            notified.cancel()
            await asyncio.gather(receive, notified, return_exceptions=True)

    async def _receive(self, connection: Session[WorkerMessage, ServerMessage]) -> WorkerMessage:
        return await asyncio.wait_for(connection.receive(), timeout=self.heartbeat_timeout)

    def notify_jobs(self) -> None:
        """Wake all idle sessions after an enqueue or slot-state transition."""
        for registration, wake in self._wakes.items():
            if self._peers.get(registration.peer_id) is registration.session:
                wake.set()

    @staticmethod
    def _validate_assignment_message(
        job_id: int,
        token: str,
        message_epoch: str,
        current: tuple[Job, str, ExecutionTask | None],
        connection_epoch: str,
    ) -> None:
        job, expected_token, _ = current
        if (
            job_id != job.id
            or not hmac.compare_digest(token, expected_token)
            or not hmac.compare_digest(message_epoch, connection_epoch)
        ):
            raise ProtocolViolation("message does not match the active assignment")

    async def _recover(
        self,
        connection: Session[WorkerMessage, ServerMessage],
        worker_id: str,
        connection_epoch: str,
        recovery: RecoveryDescriptor,
    ) -> tuple[Job, str, ExecutionTask | None] | None:
        job = self.jobs.get_job(recovery.job_id)
        if (
            job is None
            or job.assigned_worker_id != worker_id
            or job.assignment_token != recovery.assignment_token
        ):
            await connection.send(
                RecoveryRejected(
                    job_id=recovery.job_id, reason="assignment fence expired"
                )
            )
            return None
        if (
            job.result_applied_at is not None
            and recovery.result_digest == job.result_digest
            and job.state in (JobState.DONE, JobState.FAILED)
        ):
            if self.metrics is not None:
                self.metrics.replays.inc()
            logger.bind(event="job_result", reason_code="ok", job_id=job.id,
                        flow_id=job.flow_id, worker_id=worker_id, epoch=connection_epoch).info("")
            await connection.send(
                ResultAccepted(
                    job_id=job.id,
                    state=job.state,
                    assignment_token=recovery.assignment_token,
                    connection_epoch=connection_epoch,
                )
            )
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
            await connection.send(RecoveryRejected(job_id=job.id, reason=reason))
            self.notify_jobs()
            return None
        if (recovery.state == "started" and not recovery.executor_active
                and not (job.kind is JobKind.ARCHIVE and recovery.archive_resume)):
            reason = "worker restarted after remote execution started"
            delivery = self.deliveries.get_delivery(job.flow_id)
            if delivery is not None and delivery.supersedes(job.kind):
                raise ProtocolViolation("archive blocked: original executor exit cannot be confirmed")
            await self.coordinator.fail(job, reason)
            await connection.send(RecoveryRejected(job_id=job.id, reason=reason))
            return None
        if not self.workers.resume_assignment(
            job.id,
            worker_id,
            recovery.assignment_token,
            connection_epoch,
        ):
            await connection.send(
                RecoveryRejected(
                    job_id=job.id, reason="recovery deadline expired"
                )
            )
            return None
        if recovery.state in ("succeeded", "failed"):
            await connection.send(
                ReplayResult(
                    job_id=job.id,
                    assignment_token=recovery.assignment_token,
                    connection_epoch=connection_epoch,
                    heartbeat_interval=self.heartbeat_interval,
                )
            )
        else:
            await connection.send(
                ResumeAssignment(
                    job_id=job.id,
                    assignment_token=recovery.assignment_token,
                    connection_epoch=connection_epoch,
                    heartbeat_interval=self.heartbeat_interval,
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
            raise ProtocolViolation(f"{action} blocked: original worker assignment journal is missing")
        # Dispatch without Started is still unknown execution: fail through normal recovery, never rerun.
        reason = "worker reconnected without its assignment journal"
        await self.coordinator.fail(job, reason)
