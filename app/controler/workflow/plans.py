"""Shared result-plan construction, without effect execution or transaction ownership."""

import re
from collections.abc import Sequence
from typing import Literal

from app.contracts.protocol import InitialTaskResult, RevisionTaskResult
from app.contracts.result_models import Effect, LabelEffect, ResultPlan, ReviewersEffect
from app.domain.models import Flow, FlowState, JobState
from app.persistence.deliveries import DeliveryStore

_PUBLIC_STAGES = {
    "baseline", "binding", "bootstrap", "branch", "commit", "delivery", "discovery",
    "execution", "push", "result", "session", "workspace",
}
# Only fixed Worker diagnostics (or the strictly numeric timeout) belong in chat or retry prompts.
_PUBLIC_DETAILS = {
    "local branch has unpublished commits", "checkout does not match the fetched coding baseline",
    "local VCS state changed after revision preparation", "remote changed after revision preparation",
    "unregistered workspace unexpectedly exists", "repository has uncommitted changes",
    "remote branch is missing", "local commit does not match the remote branch",
    "original coding session is missing", "original coding session is unavailable",
    "missing strict revision output or session", "Amp execution output exceeded byte limit",
    "Codex execution output exceeded byte limit",
}


def coding_failure_status(result: InitialTaskResult | RevisionTaskResult) -> str:
    """Describe a coding outcome without copying command stderr, URLs or native final messages."""
    status = f"运行退出码 {result.exit_code}。"
    stage, detail = result.failure_stage, result.failure_detail or ""
    if stage is None or stage not in _PUBLIC_STAGES:
        return status
    if detail in _PUBLIC_DETAILS or re.fullmatch(
        r"(?:Amp|Codex): no new stdout/stderr for [0-9]{1,6}(?:\.[0-9]{1,6})?s; execution timed out", detail,
    ):
        return f"{status}{stage}: {detail}"
    return status + stage


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
