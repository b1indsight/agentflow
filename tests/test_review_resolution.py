"""A whole thread closes only for the exact human feedback versions delivered by a revision."""

from datetime import UTC, datetime

import pytest

from app.control.resolution import covered_thread
from app.domain.models import ReviewItemKind
from app.domain.review_models import ReviewFeedback
from app.github import ReviewComment, ReviewThread


def human(number=1, **updates):
    return ReviewComment(github_id=number, node_id=f"C{number}", author="alice", is_bot=False,
                         body="Fix the bound", updated_at=datetime(2026, 9, 7, tzinfo=UTC)
                         ).model_copy(update=updates)


def snapshot(*comments):
    return ReviewThread(thread_id="T1", repo="owner/repo", pr_number=17, path="app.py",
                        resolved=False, outdated=False, comments=tuple(comments or [human()]), complete=True)


def feedback(comment, *, kind=ReviewItemKind.INLINE):
    return ReviewFeedback(item_id=comment.github_id, version=1, kind=kind,
        github_id=comment.github_id, author=comment.author, body=comment.body,
        updated_at=comment.updated_at)


@pytest.mark.parametrize("change", ["new", "edit", "kind", "incomplete", "deleted", "omitted"])
def test_resolution_requires_complete_exact_human_coverage(change):
    before = human()
    current = snapshot(before)
    addressed = [feedback(before)]
    if change == "new":
        current = current.model_copy(update={"comments": (*current.comments, human(2))})
    elif change == "edit":
        current = current.model_copy(update={"comments": (human(body="Another issue"), *current.comments[1:])})
    elif change == "kind":
        addressed = [feedback(before, kind=ReviewItemKind.REVIEW_BODY)]
    elif change == "incomplete":
        current = current.model_copy(update={"complete": False})
    elif change == "deleted":
        addressed[0] = addressed[0].model_copy(update={"deleted": True})
    else:
        addressed = []
    assert not covered_thread(current, addressed)


def test_all_human_feedback_counts_even_on_outdated_or_manually_resolved_threads():
    current = snapshot(human(), human(2), human(3, is_bot=True))
    assert covered_thread(current, [feedback(human()), feedback(human(2))])
    current = current.model_copy(update={"outdated": True, "resolved": True})
    assert not covered_thread(current, [feedback(human())])
