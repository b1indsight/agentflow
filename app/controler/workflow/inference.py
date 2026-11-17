"""Submit/read inference jobs without owning workers, retries or a second scheduler."""

from collections.abc import Callable
from uuid import uuid4

from pydantic import JsonValue

from app.contracts.protocol import AgentResult
from app.domain.inference_models import (
    MAX_OUTPUT_BYTES,
    InferenceError,
    InferenceFailure,
)
from app.domain.models import CodingExecutionResult, JobState
from app.persistence.database import Database
from app.persistence.inference import InferenceStore


class InferencePending(Exception):
    """The durable call is queued/running; the caller can resume on its next sweep."""

    def __init__(self, job_id: int) -> None:
        super().__init__(f"inference job {job_id} is pending")
        self.job_id = job_id


class JobInference:
    """One caller's durable inference boundary, optionally attributed to a review batch.

    Repeating start on this logical call returns its saved result. Review callers
    resume by batch identity, so rebuilding a prompt cannot reset an active job's budget.
    Pending work yields immediately; it must not hold the batch scanner or a Session driver.
    """

    def __init__(self, database: Database, notify_jobs: Callable[[], None], *,
                 flow_id: int | None = None, batch_id: int | None = None,
                 request_id: str | None = None) -> None:
        self.inference = InferenceStore(database)
        self.notify_jobs = notify_jobs
        self.flow_id, self.batch_id = flow_id, batch_id
        self.request_id = request_id or uuid4().hex

    async def start(
        self, task: str, *, output_schema: dict[str, JsonValue] | None = None,
        model: str | None = None, effort: str | None = None,
        max_output_bytes: int = MAX_OUTPUT_BYTES, timeout_seconds: float = 60,
    ) -> CodingExecutionResult:
        """Queue ordinary agent arguments; placement supplies cwd, never the caller."""
        job = self.inference.enqueue_inference(
            task, request_id=self.request_id, output_schema=output_schema, model=model, effort=effort,
            max_output_bytes=max_output_bytes, timeout_seconds=timeout_seconds,
            flow_id=self.flow_id, batch_id=self.batch_id,
            validation="review" if self.batch_id is not None else "text",
        )
        state = job.inference
        assert state is not None
        if job.state in {JobState.QUEUED, JobState.RUNNING}:
            self.notify_jobs()
            raise InferencePending(job.id)
        if job.state is JobState.FAILED:
            summary = state.summary
            if summary is None:
                raise InferenceError(InferenceFailure(code="cancelled"))
            self.inference.consume_inference(job.id)
            raise InferenceError(
                InferenceFailure(code=summary.last_failure_code), backend=summary.backend,
            )
        result = AgentResult.model_validate(job.result_payload)
        assert result.result is not None
        return result.result
