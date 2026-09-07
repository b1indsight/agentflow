"""Frozen batch fixture for control-effect tests; PostgreSQL tests exercise real ingestion/locking."""

from datetime import UTC, datetime

from app.context_models import ReviewContext, ReviewPullRequest
from app.judge_models import JudgeDecision
from app.models import FeedbackRef, Job, ReviewItemKind, TriageVerdict
from app.review_models import BatchState, ReviewBatch, ReviewFeedback


def review_context(batch_id: int = 1) -> ReviewContext:
    return ReviewContext(
        batch_id=batch_id, repo="owner/repo", issue_number=8, pr_number=17,
        pull_request=ReviewPullRequest(
            number=17, state="open", head_branch="flow/8", head_sha="abc",
            base_branch="main", base_sha="def", title="Bound retries", body="Use 10 retries",
        ),
    )


def judge_decision(verdict: TriageVerdict, context: ReviewContext | None = None) -> JudgeDecision:
    return JudgeDecision(context_digest=(context or review_context()).digest,
                         session_id="test-session", model="fake", verdict=verdict)


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
            context=review_context(),
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

    def freeze_context(self, job: Job, context: ReviewContext) -> ReviewContext:
        if self.batch.context is None:
            self.batch.context = context
        return self.batch.context

    def record_decision(self, job: Job, decision: JudgeDecision) -> None:
        self.batch.decision = decision
        self.batch.open_points = decision.verdict.open_points

    def mark_context_stale(self, job: Job) -> None:
        self.batch.context_stale = True

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
