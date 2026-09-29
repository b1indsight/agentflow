"""Batch decisions use frozen feedback and cannot revive retired strong signals."""

from datetime import UTC, datetime

import pytest

from app.models import JobKind, ReviewItemKind
from app.review_models import ReviewFeedback
from app.review_policy import decide_review_batch


def feedback(body: str, item_id: int = 1) -> ReviewFeedback:
    return ReviewFeedback(item_id=item_id, version=1, kind=ReviewItemKind.REVIEW_BODY,
                          github_id=item_id, author="alice", body=body,
                          updated_at=datetime(2026, 9, 29, tzinfo=UTC))


@pytest.mark.parametrize("unused,forced,kind,trigger,instruction", [
    (True, False, JobKind.REVISION, "strong", "alice: Fix A\nalice: Fix B"),
    (True, True, JobKind.TRIAGE, "discussion", None),
    (False, False, JobKind.TRIAGE, "discussion", None),
])
def test_batch_kind_respects_current_signal_authority(unused, forced, kind, trigger, instruction):
    # A reopened PR may still contain request-changes text, but forced triage
    # and retired signals must prevent another directly authorized revision.
    decision = decide_review_batch(
        [feedback("Fix A"), feedback("", 2), feedback("Fix B", 3)],
        has_unused_signal=unused, force_triage=forced,
        previous_open_points=["A", "B", "A", "C"],
    )
    assert decision is not None
    assert (decision.kind, decision.trigger, decision.instruction) == (kind, trigger, instruction)
    assert decision.open_points == ("A", "B", "C")


@pytest.mark.parametrize("items", [[], [feedback(" \n")], [feedback("")]])
def test_empty_feedback_cannot_derive_work(items):
    assert decide_review_batch(items, has_unused_signal=True, force_triage=False,
                               previous_open_points=["still waiting"]) is None
