"""Pure review context and judge decision data for external-boundary test fixtures."""

from app.domain.context_models import ReviewContext
from app.domain.judge_models import JudgeDecision
from app.domain.models import TriageVerdict
from app.github import PullRequest


def review_context(batch_id: int = 1) -> ReviewContext:
    return ReviewContext(
        batch_id=batch_id, repo="owner/repo", issue_number=8, pr_number=17,
        pull_request=PullRequest(repo="owner/repo", merged=False,
            number=17, state="open", head_branch="flow/8", head_sha="abc",
            head_repository="owner/repo",
            merged_at=None, merge_commit_sha=None,
            base_branch="main", base_sha="def", title="Bound retries", body="Use 10 retries",
        ),
    )


def judge_decision(verdict: TriageVerdict, context: ReviewContext | None = None) -> JudgeDecision:
    return JudgeDecision(context_digest=(context or review_context()).digest,
                         session_id="test-session", model="fake", verdict=verdict)
