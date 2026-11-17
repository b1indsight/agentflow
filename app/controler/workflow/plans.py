"""Shared result-plan construction, without effect execution or transaction ownership."""

from collections.abc import Sequence
from typing import Literal

from app.contracts.result_models import Effect, LabelEffect, ResultPlan, ReviewersEffect
from app.domain.models import Flow, FlowState, JobState
from app.persistence.deliveries import DeliveryStore


def failed_plan(base: ResultPlan, reason: str) -> ResultPlan:
    """Block the Flow while preserving the result's frozen identity and execution facts."""
    return base.model_copy(
        update={
            "state": JobState.FAILED,
            "flow_state": FlowState.BLOCKED,
            "error": reason,
        }
    )


def review_effects(
    flow: Flow, number: int | Literal["created_pr"], *,
    in_review_label: str, reviewers: Sequence[str],
) -> list[Effect]:
    """Freeze the ordered review notification for a newly published delivery."""
    effects: list[Effect] = [
        LabelEffect(
            key="labels",
            installation_id=installation_id(flow),
            repo=flow.repo,
            number=number,
            label=in_review_label,
            present=True,
        )
    ]
    if reviewers:
        effects.append(
            ReviewersEffect(
                key="reviewers",
                installation_id=installation_id(flow),
                repo=flow.repo,
                number=number,
                reviewers=list(reviewers),
            )
        )
    return effects


def installation_id(flow: Flow) -> int:
    """Require the installation that authorizes a frozen GitHub effect."""
    if flow.installation_id is None:
        raise ValueError("Flow has no GitHub installation")
    return flow.installation_id


def delivered_pr_number(flow: Flow, *, deliveries: DeliveryStore) -> int:
    """Read the PR identity required by result plans and post-commit notifications."""
    delivery = deliveries.get_delivery(flow.id)
    if delivery is None or delivery.pr_number is None:
        raise ValueError("Flow has no delivered PR")
    return delivery.pr_number
