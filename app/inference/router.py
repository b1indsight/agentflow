"""Choose one execution location and own all controller-level inference retries."""

import asyncio
from collections.abc import Callable
from typing import Protocol
from uuid import uuid4

from app.domain.inference_models import (
    InferenceCallFailed,
    InferenceError,
    InferenceFailure,
    InferenceOutput,
    InferenceSummary,
    InferenceTask,
    StopReason,
)
from app.domain.models import utcnow
from app.domain.ports import Inference

OutputValidator = Callable[[InferenceOutput], object]


class WorkerInference(Protocol):
    async def infer(self, task: InferenceTask, *, avoid: frozenset[str],
                    excluded: frozenset[str], validate: OutputValidator | None) -> InferenceOutput: ...


def validate_output(output: InferenceOutput, validate: OutputValidator | None) -> None:
    """Keep caller-owned schema rejection inside the attempt's failure accounting."""
    if validate is not None:
        try:
            validate(output)
        except ValueError:
            raise InferenceError(InferenceFailure(code="invalid_response"),
                                 backend=output.adapter_version) from None


class InferenceRouter:
    """Retry at most three attempts against a single monotonic total deadline.

    ``local=None`` means bub is unconfigured, not broken. Local errors never switch
    to workers. Unknown remote outcomes exclude that worker for this logical call;
    retries can overlap remote work lost to a partition and are not exactly-once.
    """

    def __init__(self, *, local: Inference | None, workers: WorkerInference) -> None:
        self.local, self.workers = local, workers

    async def infer(self, task: InferenceTask, *,
                    validate: OutputValidator | None = None) -> InferenceOutput:
        frozen = InferenceTask.model_validate_json(task.model_dump_json())
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + frozen.timeout_seconds
        attempts = 0
        avoid: set[str] = set()
        excluded: set[str] = set()
        last = InferenceError(InferenceFailure(code="execution_timeout"))
        reason: StopReason = "deadline"
        while attempts < 3:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            request = frozen.model_copy(update={"request_id": uuid4().hex,
                                                "timeout_seconds": remaining})
            attempts += 1
            try:
                if self.local is not None:
                    output = await self.local.infer(request)
                    output.validate_for(request)
                    validate_output(output, validate)
                else:
                    output = await self.workers.infer(request, avoid=frozenset(avoid),
                                                      excluded=frozenset(excluded), validate=validate)
                if loop.time() >= deadline:
                    raise InferenceError(InferenceFailure(code="execution_timeout"),
                                         backend=output.adapter_version)
                output.validate_for(request)
                return output
            except InferenceError as error:
                if error.failure.code == "unavailable":
                    attempts -= 1  # Selection failed; no worker accepted or received an attempt.
                    if not attempts:
                        last = error
                    reason = "unavailable"
                    break
                last = error
            if not last.failure.recoverable:
                reason = "non_retryable"
                break
            if attempts == 3:
                reason = "max_attempts"
                break
            if last.worker_id is not None:
                avoid.add(last.worker_id)
                if last.failure.code in {"busy", "connection_closed", "rpc_timeout"}:
                    excluded.add(last.worker_id)
            delay = last.failure.retry_after_seconds
            if delay is None:
                delay = 0.25 * attempts if last.failure.code in {
                    "rate_limited", "backend_unavailable",
                } else 0
            if delay >= deadline - loop.time():
                reason = "deadline"
                break
            if delay:
                await asyncio.sleep(delay)
        summary = InferenceSummary(
            logical_call_id=frozen.request_id, attempts=attempts, stop_reason=reason,
            last_failure_code=last.failure.code, last_worker_id=last.worker_id,
            backend=last.backend, finished_at=utcnow(),
            elapsed_ms=max(0, int((loop.time() - started) * 1000)),
        )
        raise InferenceCallFailed(last, summary) from None
