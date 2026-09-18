"""Authenticated multi-worker WebSocket registry and single-slot scheduler."""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import Mapping
from typing import Protocol
from uuid import uuid4

from fastapi import WebSocketDisconnect
from loguru import logger
from pydantic import ValidationError

from app.models import DeliveryState, FlowDelivery, FlowExecution, Job, JobKind, JobState
from app.protocol import (
    Assignment,
    AssignmentSecret,
    ExecutionResult,
    ExecutionTask,
    Failed,
    FlowToolRequest,
    FlowToolResponse,
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
    Succeeded,
    WorkerMessage,
    parse_worker_message,
    terminal_digest,
)
from app.result_models import ResultPending


class WorkerSocket(Protocol):
    @property
    def headers(self) -> Mapping[str, str]: ...

    async def accept(self) -> None: ...

    async def close(self, code: int = 1000) -> None: ...

    async def send_text(self, data: str) -> None: ...

    async def receive_text(self) -> str: ...


class GatewayStore(Protocol):
    def get_job(self, job_id: int) -> Job | None: ...

    def get_delivery(self, flow_id: int) -> FlowDelivery | None: ...

    def get_worker_execution(self, worker_id: str) -> FlowExecution | None: ...

    def worker_connected(
        self, worker_id: str, protocol_version: int, connection_epoch: str
    ) -> object: ...

    def worker_disconnected(
        self,
        worker_id: str,
        reason: str | None = None,
        connection_epoch: str = "",
    ) -> None: ...

    def assign_job(self, worker_id: str, token: str, connection_epoch: str) -> Job | None: ...

    def start_assignment(
        self,
        job_id: int,
        attempt_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str,
    ) -> bool: ...

    def heartbeat_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str
    ) -> bool: ...

    def resume_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str
    ) -> bool: ...

    def record_assignment_result(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str,
        payload: Mapping[str, object],
        digest: str,
    ) -> str: ...

    def unapplied_result_ceiling(self) -> int: ...

    def list_unapplied_results(
        self, *, after_id: int = 0, through_id: int | None = None, limit: int = 64,
    ) -> list[Job]: ...

    def finish_assignment(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        state: JobState,
        connection_epoch: str,
    ) -> bool: ...

    def requeue_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str
    ) -> bool: ...


class AssignmentCoordinator(Protocol):
    async def prepare(self, job: Job) -> ExecutionTask | JobState: ...

    async def assignment_secret(self, job: Job, task: ExecutionTask) -> AssignmentSecret | None: ...

    async def execute_tool(
        self, request: FlowToolRequest, *, worker_id: str
    ) -> FlowToolResponse: ...

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None: ...

    async def apply_result(self, job: Job, result: ExecutionResult) -> JobState: ...

    async def fail(self, job: Job, reason: str) -> None: ...


class ProtocolViolation(RuntimeError):
    pass


class WorkerGateway:
    """Serve one single-slot session per configured worker identity."""

    def __init__(
        self,
        store: GatewayStore,
        coordinator: AssignmentCoordinator,
        *,
        worker_tokens: Mapping[str, str],
        heartbeat_interval: float = 15.0,
        heartbeat_timeout: float = 45.0,
        poll_interval: float = 2.0,
        recovery_concurrency: int = 4,
        recovery_batch_size: int = 64,
    ) -> None:
        self.store = store
        self.coordinator = coordinator
        self.worker_tokens = dict(worker_tokens)
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.poll_interval = poll_interval
        self.recovery_concurrency = recovery_concurrency
        self.recovery_batch_size = recovery_batch_size
        self._recovery_cursor = 0
        self._recovery_ceiling = 0
        self._recovery_lock = asyncio.Lock()
        self._sessions: dict[str, tuple[asyncio.Event, WorkerSocket]] = {}
        self._registry_lock = asyncio.Lock()
        self._result_locks: dict[int, asyncio.Lock] = {}

    async def _prepare_assignment(
        self, job: Job, worker_id: str, token: str, epoch: str,
    ) -> tuple[ExecutionTask | JobState, AssignmentSecret | None]:
        """Keep the unstarted lease alive across both context and capability I/O; fence dispatch."""
        async def build() -> tuple[ExecutionTask | JobState, AssignmentSecret | None]:
            prepared = await self.coordinator.prepare(job)
            if isinstance(prepared, JobState):
                return prepared, None
            try:
                secret = await self.coordinator.assignment_secret(job, prepared)
            except Exception as exc:  # noqa: BLE001
                if not self.store.heartbeat_assignment(job.id, worker_id, token, epoch):
                    raise ProtocolViolation("assignment expired during capability preparation") from None
                await self.coordinator.fail(job, f"assignment capability failed: {type(exc).__name__}")
                return JobState.FAILED, None
            return prepared, secret

        preparing = asyncio.create_task(build())
        try:
            while True:
                if not self.store.heartbeat_assignment(job.id, worker_id, token, epoch):
                    raise ProtocolViolation("assignment expired during preparation")
                done, _ = await asyncio.wait({preparing}, timeout=self.heartbeat_interval)
                if done:
                    prepared, secret = preparing.result()
                    if not isinstance(prepared, JobState) and not self.store.heartbeat_assignment(
                        job.id, worker_id, token, epoch,
                    ):
                        raise ProtocolViolation("assignment changed before dispatch")
                    delivery = self.store.get_delivery(job.flow_id)
                    if (not isinstance(prepared, JobState) and delivery is not None
                            and delivery.supersedes(job.kind)):
                        if delivery.state is DeliveryState.CLOSED:
                            return JobState.DONE, None
                        await self.coordinator.fail(job, "PR merged before task dispatch")
                        return JobState.FAILED, None
                    return prepared, secret
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
        try:
            await socket.accept()
            register = parse_worker_message(await socket.receive_text())
            if not isinstance(register, Register) or register.worker_id != worker_id:
                raise ProtocolViolation("worker registration does not match authenticated identity")
            wake = await self._register_session(worker_id, socket)
            registered_session = True
            worker = self.store.worker_connected(
                worker_id, register.protocol_version, connection_epoch
            )
            connected = True
            if register.recovery is not None:
                current = await self._recover(
                    socket,
                    worker_id,
                    connection_epoch,
                    register.recovery,
                )
            if current is None:
                stale_job_id = getattr(worker, "current_job_id", None)
                if stale_job_id is not None and (
                    register.recovery is None or register.recovery.job_id != stale_job_id
                ):
                    # A crash before journaling a newly delivered command leaves the preceding
                    # acknowledged receipt. Recover the unstarted delivery behind that receipt.
                    await self._reject_missing_journal(worker_id, connection_epoch, stale_job_id)
                elif register.recovery is None and (
                    binding := self.store.get_worker_execution(worker_id)
                ) is not None:
                    delivery = self.store.get_delivery(binding.flow_id)
                    if delivery is not None and delivery.state is DeliveryState.MERGED:
                        raise ProtocolViolation("archive blocked: original worker journal is missing")
            if current is None:
                await socket.send_text(
                    Registered(
                        heartbeat_interval=self.heartbeat_interval,
                        connection_epoch=connection_epoch,
                    ).model_dump_json()
                )

            while True:
                if current is None:
                    assert wake is not None
                    wake.clear()
                    token = uuid4().hex
                    job = self.store.assign_job(worker_id, token, connection_epoch)
                    if job is None:
                        await self._wait_while_idle(socket, wake)
                        continue
                    prepared, secret = await self._prepare_assignment(job, worker_id, token, connection_epoch)
                    if isinstance(prepared, JobState):
                        if prepared is JobState.QUEUED:
                            self.store.requeue_assignment(job.id, worker_id, token, connection_epoch)
                        else:
                            self.store.finish_assignment(
                                job.id, worker_id, token, prepared, connection_epoch,
                            )
                        self.notify_jobs()
                        continue
                    current = (job, token, prepared)
                    await socket.send_text(
                        Assignment(
                            job_id=job.id,
                            flow_id=job.flow_id,
                            assignment_token=token,
                            connection_epoch=connection_epoch,
                            task=prepared,
                            secret=secret,
                        ).model_dump_json()
                    )

                    first = await self._receive(socket)
                    if not isinstance(first, Started):
                        raise ProtocolViolation("assignment must be acknowledged with Started")
                    self._validate_assignment_message(
                        first.job_id,
                        first.assignment_token,
                        first.connection_epoch,
                        current,
                        connection_epoch,
                    )
                    if not self.store.start_assignment(
                        job.id,
                        prepared.attempt_id,
                        worker_id,
                        token,
                        connection_epoch,
                    ):
                        raise ProtocolViolation("assignment is no longer active")
                    self.coordinator.mark_started(job, prepared, worker_id)

                while True:
                    job, token, _ = current
                    message = await self._receive(socket)
                    if isinstance(message, Heartbeat):
                        self._validate_assignment_message(
                            message.job_id,
                            message.assignment_token,
                            message.connection_epoch,
                            current,
                            connection_epoch,
                        )
                        if not self.store.heartbeat_assignment(
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
                        response = await self.coordinator.execute_tool(message, worker_id=worker_id)
                        await socket.send_text(response.model_dump_json())
                        continue
                    if isinstance(message, (Succeeded, Failed)):
                        self._validate_assignment_message(
                            message.job_id,
                            message.assignment_token,
                            message.connection_epoch,
                            current,
                            connection_epoch,
                        )
                        receipt = self.store.record_assignment_result(
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
                        state: JobState
                        if receipt == "applied":
                            persisted = self.store.get_job(job.id)
                            if persisted is None or persisted.state not in (
                                JobState.DONE,
                                JobState.FAILED,
                            ):
                                raise ProtocolViolation("applied result has no terminal job")
                            state = persisted.state
                        else:
                            async with self._result_lock(job.id):
                                persisted = self.store.get_job(job.id)
                                if (
                                    persisted is not None
                                    and persisted.result_applied_at is not None
                                ):
                                    state = persisted.state
                                else:
                                    state = await self._apply_terminal(job, message)
                                    applied = self.store.get_job(job.id)
                                    if applied is None or applied.result_applied_at is None:
                                        raise ProtocolViolation(
                                            "coordinator returned before committing the result"
                                        )
                        await socket.send_text(
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
            logger.info("worker {} disconnected: {}", worker_id, exc)
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
            if connected:
                self.store.worker_disconnected(
                    worker_id,
                    disconnect_reason,
                    connection_epoch=connection_epoch,
                )
            if registered_session:
                await self._unregister_session(worker_id, socket)

    async def _wait_while_idle(self, socket: WorkerSocket, wake: asyncio.Event) -> None:
        receive = asyncio.create_task(socket.receive_text())
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

    async def _receive(self, socket: WorkerSocket) -> WorkerMessage:
        raw = await asyncio.wait_for(socket.receive_text(), timeout=self.heartbeat_timeout)
        return parse_worker_message(raw)

    def _authenticated_identity(self, headers: Mapping[str, str]) -> str | None:
        identity = headers.get("x-flowtool-worker-id", "")
        authorization = headers.get("authorization", "")
        token = self.worker_tokens.get(identity)
        if token is None:
            return None
        expected_authorization = f"Bearer {token}"
        return identity if hmac.compare_digest(authorization, expected_authorization) else None

    async def _register_session(self, worker_id: str, socket: WorkerSocket) -> asyncio.Event:
        async with self._registry_lock:
            previous = self._sessions.get(worker_id)
            wake = asyncio.Event()
            self._sessions[worker_id] = (wake, socket)
        if previous is not None:
            _, previous_socket = previous
            try:
                await previous_socket.close(code=1012)
            except (WebSocketDisconnect, ConnectionError):
                pass
        return wake

    async def _unregister_session(self, worker_id: str, socket: WorkerSocket) -> None:
        async with self._registry_lock:
            current = self._sessions.get(worker_id)
            if current is not None and current[1] is socket:
                self._sessions.pop(worker_id, None)

    def notify_jobs(self) -> None:
        """Wake all idle sessions after an enqueue or slot-state transition."""
        for wake, _ in self._sessions.values():
            wake.set()

    async def recover_pending_results(self) -> None:
        """Recover one bounded page with fixed concurrency and no overlapping recovery sweeps.

        Advance past still-pending results before wrapping, so a permanently uncertain low ID
        cannot monopolize every batch. Live WebSocket results retain the same per-job lock.
        """
        async with self._recovery_lock:
            if not self._recovery_ceiling:
                self._recovery_ceiling = self.store.unapplied_result_ceiling()
            jobs = self.store.list_unapplied_results(
                after_id=self._recovery_cursor, through_id=self._recovery_ceiling,
                limit=self.recovery_batch_size,
            )
            if not jobs and self._recovery_cursor:
                self._recovery_ceiling = self.store.unapplied_result_ceiling()
                jobs = self.store.list_unapplied_results(
                    through_id=self._recovery_ceiling, limit=self.recovery_batch_size,
                )
            self._recovery_cursor = jobs[-1].id if jobs else 0
            if (len(jobs) < self.recovery_batch_size
                    or self._recovery_cursor >= self._recovery_ceiling):
                self._recovery_cursor = self._recovery_ceiling = 0
            pending = iter(jobs)

            async def drain() -> None:
                # next() does not yield: each job belongs to exactly one pool task.
                for job in pending:
                    await self._recover_result(job.id)

            async with asyncio.TaskGroup() as group:
                for _ in range(min(self.recovery_concurrency, len(jobs))):
                    group.create_task(drain())
        self.notify_jobs()

    async def _recover_result(self, job_id: int) -> None:
        """Apply one durable message, isolating operational errors from other Flows."""
        async with self._result_lock(job_id):
            try:
                current = self.store.get_job(job_id)
                if current is None or current.result_applied_at is not None:
                    return
                payload = current.result_payload
                if payload is None:
                    return
                message = parse_worker_message(json.dumps(payload))
                if not isinstance(message, (Succeeded, Failed)):
                    raise ProtocolViolation("persisted result is not terminal")
                await self._apply_terminal(current, message)
            except ResultPending:
                return
            except Exception:  # noqa: BLE001 -- one corrupt result must not stop the recovery pool
                logger.exception("persisted result recovery failed for job {}", job_id)
                return
            logger.info("recovered persisted result for job {}", job_id)

    def _result_lock(self, job_id: int) -> asyncio.Lock:
        return self._result_locks.setdefault(job_id, asyncio.Lock())

    async def _apply_terminal(self, job: Job, message: Succeeded | Failed) -> JobState:
        if isinstance(message, Succeeded):
            return await self.coordinator.apply_result(job, message.result)
        await self.coordinator.fail(job, message.error)
        return JobState.FAILED

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
        socket: WorkerSocket,
        worker_id: str,
        connection_epoch: str,
        recovery: RecoveryDescriptor,
    ) -> tuple[Job, str, ExecutionTask | None] | None:
        job = self.store.get_job(recovery.job_id)
        if (
            job is None
            or job.assigned_worker_id != worker_id
            or job.assignment_token != recovery.assignment_token
        ):
            await socket.send_text(
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
            await socket.send_text(
                ResultAccepted(
                    job_id=job.id,
                    state=job.state,
                    assignment_token=recovery.assignment_token,
                    connection_epoch=connection_epoch,
                ).model_dump_json()
            )
            return None
        if recovery.state == "received":
            requeued = self.store.requeue_assignment(
                job.id,
                worker_id,
                recovery.assignment_token,
                connection_epoch,
            )
            reason = "unstarted assignment abandoned"
            if not requeued:
                reason = "worker lost executor during assignment start"
                await self.coordinator.fail(job, reason)
            await socket.send_text(RecoveryRejected(job_id=job.id, reason=reason).model_dump_json())
            self.notify_jobs()
            return None
        if (recovery.state == "started" and not recovery.executor_active
                and not (job.kind is JobKind.ARCHIVE and recovery.archive_resume)):
            reason = "worker restarted after remote execution started"
            delivery = self.store.get_delivery(job.flow_id)
            if delivery is not None and delivery.supersedes(job.kind):
                raise ProtocolViolation("archive blocked: original executor exit cannot be confirmed")
            await self.coordinator.fail(job, reason)
            await socket.send_text(RecoveryRejected(job_id=job.id, reason=reason).model_dump_json())
            return None
        if not self.store.resume_assignment(
            job.id,
            worker_id,
            recovery.assignment_token,
            connection_epoch,
        ):
            await socket.send_text(
                RecoveryRejected(
                    job_id=job.id, reason="recovery deadline expired"
                ).model_dump_json()
            )
            return None
        if recovery.state in ("succeeded", "failed"):
            await socket.send_text(
                ReplayResult(
                    job_id=job.id,
                    assignment_token=recovery.assignment_token,
                    connection_epoch=connection_epoch,
                    heartbeat_interval=self.heartbeat_interval,
                ).model_dump_json()
            )
        else:
            await socket.send_text(
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
        job = self.store.get_job(job_id)
        if job is None or job.assignment_token is None:
            return
        if job.started_at is None:
            self.store.requeue_assignment(job.id, worker_id, job.assignment_token, connection_epoch)
            self.notify_jobs()
            return
        delivery = self.store.get_delivery(job.flow_id)
        if delivery is not None and delivery.state in {DeliveryState.CLOSED, DeliveryState.MERGED}:
            action = "reopen" if delivery.state is DeliveryState.CLOSED else "archive"
            raise ProtocolViolation(f"{action} blocked: original worker assignment journal is missing")
        reason = "worker reconnected without its started assignment journal"
        await self.coordinator.fail(job, reason)
