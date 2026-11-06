"""One connection-owned inference slot, observed without blocking the session driver."""

import asyncio
from collections.abc import Callable

from loguru import logger

from app.communication.dispatch import Dispatcher
from app.communication.session import Session
from app.contracts.protocol import (
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
from app.worker.execution.lifecycle import WorkerProtocolError


class WorkerInferenceCallbacks:
    """Cancel and join the owned native execution before the runtime reconnects."""

    def __init__(self, *, session: Session[ServerMessageUnion, WorkerMessageUnion],
                 inference: Inference, ready: Callable[[], bool]) -> None:
        self.session, self.inference, self.ready = session, inference, ready
        self._task: asyncio.Task[InferenceResult] | None = None

    def bind(self, routes: Dispatcher[ServerMessageUnion]) -> None:
        routes.register("worker.infer", InferenceRequest, self.on_infer)

    async def on_infer(self, call: InferenceRequest, message: ServerMessageUnion) -> None:
        if not self.ready():
            raise WorkerProtocolError("inference requires completed registration")
        if self._task is not None:
            await self._send(InferenceResult(**result_header(call.message_id), method="worker.infer",
                                            failure=InferenceFailure(code="busy")))
            return
        self._task = asyncio.create_task(self._run(call))
        self.session.watch("inference", self._outcome, self._finished)

    async def _run(self, call: InferenceRequest) -> InferenceResult:
        try:
            output = await self.inference.infer(call.arguments)
            return InferenceResult(**result_header(call.message_id), method="worker.infer", output=output)
        except InferenceError as error:
            failure = error.failure
        except Exception as error:  # noqa: BLE001 — keep native diagnostics off the wire.
            logger.warning("native inference failed: {}", type(error).__name__)
            failure = InferenceFailure(code="backend_error")
        return InferenceResult(**result_header(call.message_id), method="worker.infer", failure=failure)

    async def _outcome(self) -> InferenceResult:
        assert self._task is not None
        return await asyncio.shield(self._task)

    async def _send(self, result: InferenceResult) -> None:
        await self.session.send(WorkerMessage(**message_header(self.session.epoch),
                                              control=None, payload=[result]))

    async def _finished(self, result: InferenceResult) -> None:
        await self._send(result)
        self._task = None

    async def aclose(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    cancelled = True
        if not task.cancelled():
            task.result()
        self._task = None
        if cancelled:
            raise asyncio.CancelledError
