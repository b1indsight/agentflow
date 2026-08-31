"""Authenticated single-worker WebSocket gateway."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Mapping
from typing import Protocol
from uuid import uuid4

from fastapi import WebSocketDisconnect
from loguru import logger
from pydantic import ValidationError

from app.models import Job, JobState
from app.protocol import (
    Assignment,
    ExecutionResult,
    ExecutionTask,
    Failed,
    Heartbeat,
    ProtocolError,
    Register,
    Registered,
    ResultAccepted,
    Started,
    Succeeded,
    WorkerMessage,
    parse_worker_message,
)


class WorkerSocket(Protocol):
    @property
    def headers(self) -> Mapping[str, str]: ...

    async def accept(self) -> None: ...

    async def close(self, code: int = 1000) -> None: ...

    async def send_text(self, data: str) -> None: ...

    async def receive_text(self) -> str: ...


class GatewayStore(Protocol):
    def assign_job(self, worker_id: str, token: str) -> Job | None: ...

    def start_assignment(self, job_id: int, worker_id: str, token: str) -> bool: ...

    def heartbeat_assignment(self, job_id: int, worker_id: str, token: str) -> bool: ...

    def assignment_active(self, job_id: int, worker_id: str, token: str) -> bool: ...

    def finish_assignment(
        self, job_id: int, worker_id: str, token: str, state: JobState
    ) -> bool: ...

    def requeue_assignment(self, job_id: int, worker_id: str, token: str) -> bool: ...

    def fail_assignment(self, job_id: int, worker_id: str, token: str, reason: str) -> bool: ...


class AssignmentCoordinator(Protocol):
    async def prepare(self, job: Job) -> ExecutionTask | JobState: ...

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None: ...

    async def apply_result(self, job: Job, result: ExecutionResult) -> JobState: ...

    async def fail(self, job: Job, reason: str) -> None: ...


class ProtocolViolation(RuntimeError):
    pass


class SingleWorkerGateway:
    """Serve exactly one configured worker identity and one assignment at a time."""

    def __init__(
        self,
        store: GatewayStore,
        coordinator: AssignmentCoordinator,
        *,
        worker_id: str,
        worker_token: str,
        heartbeat_interval: float = 15.0,
        heartbeat_timeout: float = 45.0,
        poll_interval: float = 2.0,
    ) -> None:
        self.store = store
        self.coordinator = coordinator
        self.worker_id = worker_id
        self.worker_token = worker_token
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.poll_interval = poll_interval
        self._connected = False

    async def handle(self, socket: WorkerSocket) -> None:
        if not self._authenticated(socket.headers):
            await socket.close(code=1008)
            return
        if self._connected:
            await socket.close(code=1008)
            return

        self._connected = True
        current: tuple[Job, str, ExecutionTask] | None = None
        started = False
        try:
            await socket.accept()
            register = parse_worker_message(await socket.receive_text())
            if not isinstance(register, Register) or register.worker_id != self.worker_id:
                raise ProtocolViolation("worker registration does not match authenticated identity")
            await socket.send_text(
                Registered(heartbeat_interval=self.heartbeat_interval).model_dump_json()
            )

            while True:
                token = uuid4().hex
                job = self.store.assign_job(self.worker_id, token)
                if job is None:
                    await self._wait_while_idle(socket)
                    continue
                prepared = await self.coordinator.prepare(job)
                if isinstance(prepared, JobState):
                    self.store.finish_assignment(
                        job.id, self.worker_id, token, prepared
                    )
                    continue
                current = (job, token, prepared)
                started = False
                await socket.send_text(
                    Assignment(
                        job_id=job.id,
                        flow_id=job.flow_id,
                        assignment_token=token,
                        task=prepared,
                    ).model_dump_json()
                )

                first = await self._receive(socket)
                if not isinstance(first, Started):
                    raise ProtocolViolation("assignment must be acknowledged with Started")
                self._validate_assignment_message(first.job_id, first.assignment_token, current)
                if not self.store.start_assignment(job.id, self.worker_id, token):
                    raise ProtocolViolation("assignment is no longer active")
                self.coordinator.mark_started(job, prepared, self.worker_id)
                started = True

                while True:
                    message = await self._receive(socket)
                    if isinstance(message, Heartbeat):
                        self._validate_assignment_message(
                            message.job_id, message.assignment_token, current
                        )
                        if not self.store.heartbeat_assignment(
                            job.id, self.worker_id, token
                        ):
                            raise ProtocolViolation("heartbeat belongs to an inactive assignment")
                        continue
                    if isinstance(message, Succeeded):
                        self._validate_assignment_message(
                            message.job_id, message.assignment_token, current
                        )
                        if not self.store.assignment_active(job.id, self.worker_id, token):
                            raise ProtocolViolation("result belongs to an inactive assignment")
                        state = await self.coordinator.apply_result(job, message.result)
                        if not self.store.finish_assignment(
                            job.id, self.worker_id, token, state
                        ):
                            raise ProtocolViolation("result lost its assignment fence")
                        await socket.send_text(
                            ResultAccepted(job_id=job.id, state=state).model_dump_json()
                        )
                        current = None
                        started = False
                        break
                    if isinstance(message, Failed):
                        self._validate_assignment_message(
                            message.job_id, message.assignment_token, current
                        )
                        await self.coordinator.fail(job, message.error)
                        if not self.store.fail_assignment(
                            job.id, self.worker_id, token, message.error
                        ):
                            raise ProtocolViolation("failure lost its assignment fence")
                        await socket.send_text(
                            ResultAccepted(job_id=job.id, state=JobState.FAILED).model_dump_json()
                        )
                        current = None
                        started = False
                        break
                    raise ProtocolViolation(
                        f"unexpected assignment message: {type(message).__name__}"
                    )
        except (WebSocketDisconnect, ConnectionError, TimeoutError) as exc:
            logger.info("worker {} disconnected: {}", self.worker_id, exc)
        except ProtocolViolation as exc:
            logger.warning("worker protocol violation: {}", exc)
            try:
                await socket.send_text(
                    ProtocolError(code="protocol_violation", detail=str(exc)).model_dump_json()
                )
                await socket.close(code=1008)
            except (WebSocketDisconnect, ConnectionError):
                pass
        except ValidationError:
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
            if current is not None:
                job, token, _ = current
                if started:
                    reason = "worker disconnected after remote execution started"
                    await self.coordinator.fail(job, reason)
                    self.store.fail_assignment(job.id, self.worker_id, token, reason)
                else:
                    self.store.requeue_assignment(job.id, self.worker_id, token)
            self._connected = False

    async def _wait_while_idle(self, socket: WorkerSocket) -> None:
        try:
            raw = await asyncio.wait_for(socket.receive_text(), timeout=self.poll_interval)
        except TimeoutError:
            return
        message = parse_worker_message(raw)
        raise ProtocolViolation(f"unexpected idle message: {type(message).__name__}")

    async def _receive(self, socket: WorkerSocket) -> WorkerMessage:
        raw = await asyncio.wait_for(socket.receive_text(), timeout=self.heartbeat_timeout)
        return parse_worker_message(raw)

    def _authenticated(self, headers: Mapping[str, str]) -> bool:
        identity = headers.get("x-flowtool-worker-id", "")
        authorization = headers.get("authorization", "")
        expected_authorization = f"Bearer {self.worker_token}"
        return hmac.compare_digest(identity, self.worker_id) and hmac.compare_digest(
            authorization, expected_authorization
        )

    @staticmethod
    def _validate_assignment_message(
        job_id: int,
        token: str,
        current: tuple[Job, str, ExecutionTask],
    ) -> None:
        job, expected_token, _ = current
        if job_id != job.id or not hmac.compare_digest(token, expected_token):
            raise ProtocolViolation("message does not match the active assignment")
