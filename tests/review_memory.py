"""Pure review context and judge decision data for external-boundary test fixtures."""

from app.domain.context_models import ReviewContext
from app.domain.judge_models import JudgeDecision
from app.domain.models import ReviewItemKind, TriageVerdict
from app.domain.review_models import ReviewFeedback
from app.github import Batch, PullRequest, ReviewComment, ReviewThread


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


def feedback_threads(feedback: list[ReviewFeedback]) -> list[ReviewThread]:
    """Represent each supplied inline input as a complete, independent remote thread."""
    return [ReviewThread(
        thread_id=f"T{item.github_id}", repo="owner/repo", pr_number=17, path=item.path or "app.py",
        resolved=False, outdated=False, complete=True, comments=Batch[ReviewComment](items=(
            ReviewComment(github_id=item.github_id, node_id=f"C{item.github_id}", author=item.author,
                          is_bot=False, body=item.body, updated_at=item.updated_at),
        )),
    ) for item in feedback if item.kind is ReviewItemKind.INLINE]
