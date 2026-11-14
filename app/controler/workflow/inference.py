"""Submit/read inference jobs without owning workers, retries or a second scheduler."""

from collections.abc import Callable

from app.contracts.protocol import InferenceResult
from app.domain.inference_models import (
    InferenceError,
    InferenceFailure,
    InferenceOutput,
    InferenceTask,
)
from app.domain.models import JobState
from app.persistence.database import Database
from app.persistence.jobs import JobStore


class InferencePending(Exception):
    """The durable call is queued/running; the caller can resume on its next sweep."""

    def __init__(self, job_id: int) -> None:
        super().__init__(f"inference job {job_id} is pending")
        self.job_id = job_id


class JobInference:
    """One caller's durable inference boundary, optionally attributed to a review batch.

    Repeating infer with the same logical request returns its saved result. Review callers
    resume by batch identity, so rebuilding a prompt cannot reset an active job's budget.
    Pending work yields immediately; it must not hold the batch scanner or a Session driver.
    """

    def __init__(self, database: Database, notify_jobs: Callable[[], None], *,
                 flow_id: int | None = None, batch_id: int | None = None) -> None:
        self.jobs = JobStore(database)
        self.notify_jobs = notify_jobs
        self.flow_id, self.batch_id = flow_id, batch_id

    async def infer(self, task: InferenceTask) -> InferenceOutput:
        job = self.jobs.enqueue_inference(task, flow_id=self.flow_id, batch_id=self.batch_id,
                                          validation="review" if self.batch_id is not None else "text")
        state = job.inference
        assert state is not None
        if job.state in {JobState.QUEUED, JobState.RUNNING}:
            self.notify_jobs()
            raise InferencePending(job.id)
        if job.state is JobState.FAILED:
            summary = state.summary
            if summary is None:
                raise InferenceError(InferenceFailure(code="cancelled"))
            self.jobs.consume_inference(job.id)
            raise InferenceError(
                InferenceFailure(code=summary.last_failure_code), backend=summary.backend,
            )
        result = InferenceResult.model_validate(job.result_payload)
        assert result.output is not None
        return result.output
