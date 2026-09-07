"""Frozen batch fixture for control-effect tests; PostgreSQL tests exercise real ingestion/locking."""

from datetime import UTC, datetime

from app.models import FeedbackRef, Job, ReviewItemKind
from app.review_models import BatchState, ReviewBatch, ReviewFeedback


class MemoryReviews:
    def __init__(self) -> None:
        self.batch = ReviewBatch(
            id=1,
            flow_id=1,
            attempt_id=1,
            generation=1,
            state=BatchState.READY,
            trigger="discussion",
            job_id=None,
            open_points=[],
            created_at=datetime.now(UTC),
            feedback=[
                ReviewFeedback(
                    item_id=101,
                    version=1,
                    kind=ReviewItemKind.INLINE,
                    github_id=500,
                    author="reviewer",
                    body="Please fix it.",
                    updated_at=datetime.now(UTC),
                )
            ],
        )
        self.consumed: list[FeedbackRef] = []

    def for_job(self, job: Job) -> ReviewBatch:
        return self.batch.model_copy(update={"job_id": job.id})

    def started(self, job: Job) -> bool:
        first = self.batch.state is BatchState.READY
        self.batch.state = BatchState.PROCESSING
        return first

    def advance(self, batch_id: int, job: Job) -> None:
        self.batch.job_id, self.batch.state = job.id, BatchState.READY

    def retry(self, previous: Job, replacement: Job) -> None:
        self.advance(self.batch.id, replacement)

    def complete(self, batch_id: int, state: BatchState, consumed, open_points) -> None:
        self.batch.state, self.batch.open_points = state, open_points
        self.consumed = list(consumed)

    def cancel(self, flow_id: int) -> None:
        self.batch.state = BatchState.CANCELLED
