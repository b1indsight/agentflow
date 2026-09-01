"""Outbound WebSocket client for the single remote worker."""

from __future__ import annotations

import asyncio
from typing import Protocol

from loguru import logger
from websockets.asyncio.client import connect

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
    parse_server_message,
)


class Connection(Protocol):
    async def send(self, data: str) -> None: ...

    async def recv(self) -> str | bytes: ...


class TaskExecutor(Protocol):
    async def execute(self, task: ExecutionTask) -> ExecutionResult: ...


class WorkerProtocolError(RuntimeError):
    pass


class WorkerClient:
    """Maintain the worker-initiated connection and execute one assignment at a time."""

    def __init__(
        self,
        *,
        worker_id: str,
        worker_token: str,
        serve_ws_url: str,
        executor: TaskExecutor,
    ) -> None:
        self.worker_id = worker_id
        self.worker_token = worker_token
        self.serve_ws_url = serve_ws_url
        self.executor = executor
        self._stop = asyncio.Event()

    async def run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                async with connect(
                    self.serve_ws_url,
                    additional_headers={
                        "Authorization": f"Bearer {self.worker_token}",
                        "X-FlowTool-Worker-ID": self.worker_id,
                    },
                ) as connection:
                    delay = 1.0
                    await self.run_connection(connection)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("worker connection lost; retrying in {}s: {}", delay, exc)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    delay = min(delay * 2, 30.0)

    async def run_connection(self, connection: Connection) -> None:
        await connection.send(Register(worker_id=self.worker_id).model_dump_json())
        first = parse_server_message(await _receive_text(connection))
        if isinstance(first, ProtocolError):
            raise WorkerProtocolError(
                f"worker registration rejected: {first.code}: {first.detail}"
            )
        if not isinstance(first, Registered):
            raise WorkerProtocolError(
                f"expected Registered, received {type(first).__name__}"
            )
        logger.info("worker {} registered with control plane", self.worker_id)

        while not self._stop.is_set():
            message = parse_server_message(await _receive_text(connection))
            if isinstance(message, ProtocolError):
                raise WorkerProtocolError(
                    f"serve protocol error: {message.code}: {message.detail}"
                )
            if not isinstance(message, Assignment):
                raise WorkerProtocolError(
                    f"expected Assignment, received {type(message).__name__}"
                )
            await self._execute_assignment(connection, message, first.heartbeat_interval)

    async def _execute_assignment(
        self,
        connection: Connection,
        assignment: Assignment,
        heartbeat_interval: float,
    ) -> None:
        await connection.send(
            Started(
                job_id=assignment.job_id,
                assignment_token=assignment.assignment_token,
            ).model_dump_json()
        )
        execution = asyncio.create_task(self.executor.execute(assignment.task))
        heartbeat = asyncio.create_task(
            self._heartbeat(connection, assignment, heartbeat_interval)
        )
        try:
            completed, _ = await asyncio.wait(
                {execution, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in completed:
                heartbeat.result()
                raise WorkerProtocolError("heartbeat task stopped unexpectedly")

            try:
                result = execution.result()
                terminal: Succeeded | Failed = Succeeded(
                    job_id=assignment.job_id,
                    assignment_token=assignment.assignment_token,
                    result=result,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("remote job {} failed", assignment.job_id)
                terminal = Failed(
                    job_id=assignment.job_id,
                    assignment_token=assignment.assignment_token,
                    error=str(exc),
                )

            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await connection.send(terminal.model_dump_json())
            accepted = parse_server_message(await _receive_text(connection))
            if not isinstance(accepted, ResultAccepted) or accepted.job_id != assignment.job_id:
                raise RuntimeError("serve did not acknowledge the remote result")
        finally:
            execution.cancel()
            heartbeat.cancel()
            await asyncio.gather(execution, heartbeat, return_exceptions=True)

    async def _heartbeat(
        self,
        connection: Connection,
        assignment: Assignment,
        interval: float,
    ) -> None:
        while True:
            await asyncio.sleep(interval)
            await connection.send(
                Heartbeat(
                    job_id=assignment.job_id,
                    assignment_token=assignment.assignment_token,
                ).model_dump_json()
            )

    def stop(self) -> None:
        self._stop.set()


async def _receive_text(connection: Connection) -> str:
    raw = await connection.recv()
    return raw.decode() if isinstance(raw, bytes) else raw
