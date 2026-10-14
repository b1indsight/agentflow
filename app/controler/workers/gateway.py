"""Authenticated multi-worker WebSocket registry and single-slot scheduler."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Mapping
from uuid import uuid4

from fastapi import WebSocketDisconnect
from loguru import logger
from pydantic import ValidationError

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
    Started,
    StopAccepted,
    Succeeded,
    WorkerMessage,
    parse_worker_message,
    terminal_digest,
)
from app.contracts.result_models import ResultPending
from app.controler.workers.connection import ProtocolViolation, WorkerConnection, WorkerSocket
from app.controler.workers.conversation import ConversationBroker
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


class WorkerGateway:
    """Serve one single-slot session per configured worker identity."""

    def __init__(
        self,
        coordinator: JobCoordinator,
        *,
        worker_tokens: Mapping[str, str],
        heartbeat_interval: float = 15.0,
        heartbeat_timeout: float = 45.0,
        poll_interval: float = 2.0,
        metrics: Metrics | None = None,
        database: Database,
        tools: FlowToolService,
        results: ResultApplication,
        conversations: ConversationBroker,
    ) -> None:
        self.deliveries = DeliveryStore(database)
        self.jobs = JobStore(database)
        self.stops = StopStore(database)
        self.workers = WorkerStore(database)
        self.metrics = metrics
        self.coordinator = coordinator
        self.tools = tools
        self.results = results
        self.worker_tokens = dict(worker_tokens)
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.poll_interval = poll_interval
        self._sessions: dict[str, tuple[asyncio.Event, WorkerConnection]] = {}
        self._registry_lock = asyncio.Lock()
        self.conversations = conversations

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

    async def handle(self, socket: WorkerSocket) -> None:
        worker_id = self._authenticated_identity(socket.headers)
        if worker_id is None:
            await socket.close(code=1008)
            return

        current: tuple[Job, str, ExecutionTask | None] | None = None
        connected = False
        registered_session = False
        connection_epoch = uuid4().hex
        wake: asyncio.Event | None = None
        disconnect_reason: str | None = None
        connection: WorkerConnection | None = None
        try:
            await socket.accept()
            register = parse_worker_message(await socket.receive_text())
            if not isinstance(register, Register) or register.worker_id != worker_id:
                raise ProtocolViolation("worker registration does not match authenticated identity")
            connection = WorkerConnection(socket, self.conversations, worker_id, connection_epoch)
            wake = await self._register_session(worker_id, connection)
            registered_session = True
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
                stale_job_id = None if stop is not None else worker.current_job_id
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
                await connection.send_text(
                    Registered(
                        heartbeat_interval=self.heartbeat_interval,
                        connection_epoch=connection_epoch,
                    ).model_dump_json()
                )

            while True:
                if current is None:
                    assert wake is not None
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
                    await connection.send_text(prepared.model_dump_json())

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
                        await connection.send_text(response.model_dump_json())
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
                        await connection.send_text(
                            ResultAccepted(
                                job_id=job.id,
                                state=state,
                                assignment_token=token,
                                connection_epoch=connection_epoch,
                            ).model_dump_json()
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
            await socket.close(code=1013)
        except (WebSocketDisconnect, ConnectionError, TimeoutError) as exc:
            disconnect_reason = str(exc) or type(exc).__name__
            logger.bind(event="worker_disconnected", reason_code="connection_lost",
                        worker_id=worker_id).opt(exception=exc).info("")
        except ProtocolViolation as exc:
            disconnect_reason = str(exc)
            logger.warning("worker protocol violation: {}", exc)
            try:
                await socket.send_text(
                    ProtocolError(code="protocol_violation", detail=str(exc)).model_dump_json()
                )
                await socket.close(code=1008)
            except (WebSocketDisconnect, ConnectionError):
                pass
        except ValidationError:
            disconnect_reason = "message failed protocol validation"
            logger.warning("worker sent a message that failed protocol validation")
            try:
                await socket.send_text(
                    ProtocolError(
                        code="invalid_message",
                        detail="message failed protocol validation",
                    ).model_dump_json()
                )
                await socket.close(code=1008)
            except (WebSocketDisconnect, ConnectionError):
                pass
        finally:
            if connection is not None:
                await connection.stop()
            if connected:
                self.workers.worker_disconnected(
                    worker_id,
                    disconnect_reason,
                    connection_epoch=connection_epoch,
                )
            if registered_session:
                assert connection is not None
                await self._unregister_session(worker_id, connection)

    async def _stop_worker(self, connection: WorkerConnection, worker_id: str, epoch: str, stop: StopIntent) -> None:
        await connection.send_text(stop.message(epoch).model_dump_json())
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
                    await connection.send_text(StopAccepted(**stop.message(epoch).model_dump(exclude={'type'})).model_dump_json())
                    self.notify_jobs()
                    return

    async def _wait_while_idle(self, connection: WorkerConnection, wake: asyncio.Event) -> None:
        receive = asyncio.create_task(connection.receive_text())
        notified = asyncio.create_task(wake.wait())
        try:
            done, _ = await asyncio.wait(
                {receive, notified},
                timeout=self.poll_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receive in done:
                message = parse_worker_message(receive.result())
                raise ProtocolViolation(f"unexpected idle message: {type(message).__name__}")
        finally:
            receive.cancel()
            notified.cancel()
            await asyncio.gather(receive, notified, return_exceptions=True)

    async def _receive(self, connection: WorkerConnection) -> WorkerMessage:
        raw = await asyncio.wait_for(connection.receive_text(), timeout=self.heartbeat_timeout)
        return parse_worker_message(raw)

    def _authenticated_identity(self, headers: Mapping[str, str]) -> str | None:
        identity = headers.get("x-flowtool-worker-id", "")
        authorization = headers.get("authorization", "")
        token = self.worker_tokens.get(identity)
        if token is None:
            return None
        expected_authorization = f"Bearer {token}"
        return identity if hmac.compare_digest(authorization, expected_authorization) else None

    async def _register_session(self, worker_id: str, connection: WorkerConnection) -> asyncio.Event:
        async with self._registry_lock:
            previous = self._sessions.get(worker_id)
            wake = asyncio.Event()
            self._sessions[worker_id] = (wake, connection)
        if previous is not None:
            _, previous_connection = previous
            try:
                await previous_connection.close(code=1012)
            except (WebSocketDisconnect, ConnectionError):
                pass
        return wake

    async def _unregister_session(self, worker_id: str, connection: WorkerConnection) -> None:
        self.conversations.disconnect(connection)
        async with self._registry_lock:
            current = self._sessions.get(worker_id)
            if current is not None and current[1] is connection:
                self._sessions.pop(worker_id, None)

    def notify_jobs(self) -> None:
        """Wake all idle sessions after an enqueue or slot-state transition."""
        for wake, _ in self._sessions.values():
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
        connection: WorkerConnection,
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
            await connection.send_text(
                RecoveryRejected(
                    job_id=recovery.job_id, reason="assignment fence expired"
                ).model_dump_json()
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
            await connection.send_text(
                ResultAccepted(
                    job_id=job.id,
                    state=job.state,
                    assignment_token=recovery.assignment_token,
                    connection_epoch=connection_epoch,
                ).model_dump_json()
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
            await connection.send_text(RecoveryRejected(job_id=job.id, reason=reason).model_dump_json())
            self.notify_jobs()
            return None
        if (recovery.state == "started" and not recovery.executor_active
                and not (job.kind is JobKind.ARCHIVE and recovery.archive_resume)):
            reason = "worker restarted after remote execution started"
            delivery = self.deliveries.get_delivery(job.flow_id)
            if delivery is not None and delivery.supersedes(job.kind):
                raise ProtocolViolation("archive blocked: original executor exit cannot be confirmed")
            await self.coordinator.fail(job, reason)
            await connection.send_text(RecoveryRejected(job_id=job.id, reason=reason).model_dump_json())
            return None
        if not self.workers.resume_assignment(
            job.id,
            worker_id,
            recovery.assignment_token,
            connection_epoch,
        ):
            await connection.send_text(
                RecoveryRejected(
                    job_id=job.id, reason="recovery deadline expired"
                ).model_dump_json()
            )
            return None
        if recovery.state in ("succeeded", "failed"):
            await connection.send_text(
                ReplayResult(
                    job_id=job.id,
                    assignment_token=recovery.assignment_token,
                    connection_epoch=connection_epoch,
                    heartbeat_interval=self.heartbeat_interval,
                ).model_dump_json()
            )
        else:
            await connection.send_text(
                ResumeAssignment(
                    job_id=job.id,
                    assignment_token=recovery.assignment_token,
                    connection_epoch=connection_epoch,
                    heartbeat_interval=self.heartbeat_interval,
                ).model_dump_json()
            )
        return job, recovery.assignment_token, None

    async def _reject_missing_journal(
        self, worker_id: str, connection_epoch: str, job_id: int
    ) -> None:
        job = self.jobs.get_job(job_id)
        if job is None or job.assignment_token is None:
            return
        if job.started_at is None:
            self.workers.requeue_assignment(job.id, worker_id, job.assignment_token, connection_epoch)
            self.notify_jobs()
            return
        delivery = self.deliveries.get_delivery(job.flow_id)
        if delivery is not None and delivery.state in {DeliveryState.CLOSED, DeliveryState.MERGED}:
            action = "reopen" if delivery.state is DeliveryState.CLOSED else "archive"
            raise ProtocolViolation(f"{action} blocked: original worker assignment journal is missing")
        reason = "worker reconnected without its started assignment journal"
        await self.coordinator.fail(job, reason)
