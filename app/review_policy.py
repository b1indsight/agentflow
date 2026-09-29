"""Choose review work from frozen feedback; persistence owns signal authority and locking."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.models import JobKind
from app.review_models import ReviewFeedback


@dataclass(frozen=True)
class ReviewBatchDecision:
    """Content to freeze in a new batch; no database identity exists yet."""

    kind: JobKind
    trigger: Literal["strong", "discussion"]
    instruction: str | None
    open_points: tuple[str, ...]


def decide_review_batch(
    feedback: Sequence[ReviewFeedback], *, has_unused_signal: bool, force_triage: bool,
    previous_open_points: Sequence[str],
) -> ReviewBatchDecision | None:
    """Preserve feedback order and authorize direct revision only from a current strong signal.

    The caller supplies signal validity for exactly this selected feedback, not
    all pending items. Empty feedback leaves earlier waiting batches untouched.
    """
    if not any(item.body.strip() for item in feedback):
        return None
    strong = has_unused_signal and not force_triage
    return ReviewBatchDecision(
        kind=JobKind.REVISION if strong else JobKind.TRIAGE,
        trigger="strong" if strong else "discussion",
        instruction="\n".join(f"{item.author}: {item.body}" for item in feedback if item.body)
        if strong else None,
        open_points=tuple(dict.fromkeys(previous_open_points)),
    )
