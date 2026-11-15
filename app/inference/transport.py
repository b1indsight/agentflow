"""One connection-owned inference slot, observed without blocking the session driver."""

import asyncio
from collections.abc import Callable

from loguru import logger

from app.communication.dispatch import Dispatcher
from app.communication.session import Session
from app.contracts.protocol import (
    CancelJob,
    Heartbeat,
    InferenceRequest,
    InferenceResult,
    ServerMessageUnion,
    WorkerMessage,
    WorkerMessageUnion,
    message_header,
    result_header,
)
from app.domain.inference_models import InferenceError, InferenceFailure
from app.domain.ports import Inference


class InferenceProtocolError(RuntimeError):
    """A peer sent inference traffic without the required connection authority."""


class WorkerInferenceCallbacks:
    """Own one inference slot, shared by native workers and the infer-only local endpoint.

    Connection callbacks authorize epochs and cancellation controls; this callback owns
    provider execution and must be closed and joined before its connection is replaced.
    """

    def __init__(self, *, session: Session[ServerMessageUnion, WorkerMessageUnion],
                 inference: Inference, ready: Callable[[], bool]) -> None:
        self.session, self.inference, self.ready = session, inference, ready
        self._call: InferenceRequest | None = None
        self._task: asyncio.Task[InferenceResult] | None = None
        self._cancel_requested = False

    def bind(self, routes: Dispatcher[ServerMessageUnion]) -> None:
        routes.register("worker.infer", InferenceRequest, self.on_infer)

    async def on_infer(self, call: InferenceRequest, message: ServerMessageUnion) -> None:
        if not self.ready():
            raise InferenceProtocolError("inference requires completed registration")
        if self._task is not None:
            await self._send(InferenceResult(**result_header(call.message_id), method="worker.infer",
                                            job_id=call.job_id, assignment_token=call.assignment_token,
                                            failure=InferenceFailure(code="busy")))
            return
        self._call = call
        self._cancel_requested = False
        self._task = asyncio.create_task(self._run(call))
        self.session.watch("inference", self._outcome, self._finished)

    def cancel_job(self, message: CancelJob) -> None:
        """Signal only the matching slot; its observer replies after provider cleanup joins.

        Keep the driver available for coding messages while cancellation drains child IO.
        Duplicate controls must not interrupt a provider already performing cleanup.
        """
        call, task = self._call, self._task
        if call is None or task is None or (message.job_id, message.assignment_token) != (
            call.job_id, call.assignment_token
        ):
            return
        self._cancel_requested = True
        if not task.cancelling():
            task.cancel()

    def heartbeat(self) -> Heartbeat | None:
        """Keep an inference-only assignment alive without inventing a coding lease."""
        if self._call is None:
            return None
        return Heartbeat(
            **message_header(self.session.epoch), control="heartbeat", payload=[],
            job_id=self._call.job_id, assignment_token=self._call.assignment_token,
        )

    async def _run(self, call: InferenceRequest) -> InferenceResult:
        try:
            output = await self.inference.infer(call.arguments)
            return InferenceResult(
                **result_header(call.message_id), method="worker.infer", output=output,
                job_id=call.job_id, assignment_token=call.assignment_token,
            )
        except InferenceError as error:
            failure = error.failure
        except Exception as error:  # noqa: BLE001 — keep native diagnostics off the wire.
            logger.warning("native inference failed: {}", type(error).__name__)
            failure = InferenceFailure(code="backend_error")
        return InferenceResult(
            **result_header(call.message_id), method="worker.infer", failure=failure,
            job_id=call.job_id, assignment_token=call.assignment_token,
        )

    def _cancelled_result(self) -> InferenceResult:
        assert self._call is not None
        return InferenceResult(
            **result_header(self._call.message_id), method="worker.infer",
            job_id=self._call.job_id, assignment_token=self._call.assignment_token,
            failure=InferenceFailure(code="cancelled"),
        )

    async def _outcome(self) -> InferenceResult:
        assert self._task is not None
        try:
            return await asyncio.shield(self._task)
        except asyncio.CancelledError:
            if not self._task.cancelled():
                raise  # Cancelling an observer never owns the provider's lifetime.
            return self._cancelled_result()

    async def _send(self, result: InferenceResult) -> None:
        await self.session.send(WorkerMessage(**message_header(self.session.epoch),
                                              control=None, payload=[result]))

    async def _finished(self, result: InferenceResult) -> None:
        await self._send(self._cancelled_result() if self._cancel_requested else result)
        self._call = None
        self._task = None

    async def aclose(self) -> None:
        task = self._task
        if task is None:
            return
        if not task.cancelling():
            task.cancel()
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                caller = asyncio.current_task()
                if caller is not None and caller.cancelling():
                    cancelled = True
        if not task.cancelled():
            task.result()
        self._call = None
        self._task = None
        if cancelled:
            raise asyncio.CancelledError
