"""Own delivery identity, PR creation adoption, close/reopen and merge transitions."""

import json
from dataclasses import dataclass

from app.contracts.protocol import (
    InitialTaskResult,
    RevisionTaskResult,
    Succeeded,
    parse_worker_message,
)
from app.contracts.result_models import ResultPending
from app.control.initial import initial_delivery_mismatch
from app.control.revisions import frozen_revision, revision_mismatch, verify_revision_remote
from app.domain.models import (
    TERMINAL_STATES,
    DeliveryState,
    Flow,
    FlowDelivery,
    FlowState,
    Job,
    JobKind,
)
from app.github import PullRequest, PullRequestMerge, Repository
from app.persistence.store import Store


@dataclass(frozen=True)
class ReopenProof:
    head_sha: str
    session_id: str | None = None
    session_generation: int | None = None


class DeliveryConflict(RuntimeError):
    """The observed PR cannot fulfill this delivery intent."""


def validate_open_pull_request(
    remote: PullRequest, branch: str, base: str, head_sha: str,
) -> None:
    """Require an open PR with the exact prepared branch, base and commit."""
    if remote.state.casefold() != "open":
        raise DeliveryConflict(f"pull request is {remote.state}, expected open")
    if remote.head_branch != branch or remote.base_branch != base or remote.head_sha != head_sha:
        raise DeliveryConflict("pull request head, base, or SHA conflicts with Flow delivery")


def validate_created_pull_request(
    remote: PullRequest, repo: str, number: int, expected: FlowDelivery,
) -> None:
    """Closed/cancelled creation recovery must prove repository ownership too."""
    if (remote.repo != repo or remote.number != number or remote.head_repository != repo
            or remote.head_branch != expected.branch or remote.base_branch != expected.base_branch
            or remote.head_sha != expected.head_sha):
        raise DeliveryConflict("INITIAL PR identity changed during recovery")


def adopt_created_pull_request(
    store: Store, attempt_id: int | None, expected: FlowDelivery, remote: PullRequest,
) -> None:
    """Bind a verified creation receipt and apply its lifecycle under the unchanged intent."""
    with store.transaction() as tx:
        flow = tx.lock_workflow(expected.flow_id)
        current = tx.get_delivery(expected.flow_id)
        if (flow is None or current is None or flow.current_attempt_id != attempt_id
                or current.version != expected.version or current.head_sha != expected.head_sha
                or current.pr_number not in {None, remote.number}):
            raise ResultPending("INITIAL delivery changed during PR verification")
        cancelled = flow.state is FlowState.CANCELLED
        if cancelled and (current.state is not DeliveryState.OPENING or current.pr_number is not None
                          or current.branch != expected.branch):
            raise ResultPending("cancelled delivery changed during recovery")
        if not cancelled and remote.state != "closed":
            raise ResultPending("active PR creation has not closed")
        current.pr_number = remote.number
        current.url = f"https://github.com/{flow.repo}/pull/{remote.number}"
        if remote.merged:
            record_merge(tx, flow, current, _merge_observation(remote))
        elif cancelled:
            current.state = DeliveryState.CLOSED if remote.state == "closed" else DeliveryState.OPEN
            current.version += 1
            tx.update_delivery(current)
        else:
            pause_delivery(tx, current)


def _merge_observation(remote: PullRequest) -> PullRequestMerge:
    assert remote.merged_at is not None and remote.merge_commit_sha is not None
    return PullRequestMerge(number=remote.number, merged=True, merged_at=remote.merged_at,
                            merge_commit_sha=remote.merge_commit_sha)


def pause_delivery(tx: Store, delivery: FlowDelivery) -> None:
    """Runs under the Flow lock; leave running executors and the binding untouched."""
    if delivery.state in {DeliveryState.CLOSED, DeliveryState.MERGED}:
        return
    delivery.state = DeliveryState.CLOSED
    delivery.version += 1
    tx.update_delivery(delivery)
    tx.cancel_jobs(delivery.flow_id, reason="PR closed or baseline changed; queued work retired")


async def prove_reopen(
    store: Store, repo: Repository, flow: Flow, delivery: FlowDelivery,
    remote: PullRequest, job: Job | None,
) -> tuple[ReopenProof | None, str | None]:
    """Only an exact old head or a verified frozen worker delivery may resume this PR."""
    if (not delivery.head_sha or not delivery.base_branch or flow.installation_id is None
            or remote.head_branch != delivery.branch or remote.base_branch != delivery.base_branch):
        return None, "PR head/base requires manual reconciliation"
    proof = ReopenProof(delivery.head_sha)
    revision = None
    if job is not None:
        if job.result_payload is None or job.result_applied_at is None:
            return None, "old executor or result is not settled"
        message = parse_worker_message(json.dumps(job.result_payload))
        if not isinstance(message, Succeeded):
            return None, "old coding execution failed; repository requires manual reconciliation"
        result = message.result
        if isinstance(result, InitialTaskResult):
            attempt = store.get_current_attempt(flow)
            call = store.get_tool_call(result.open_pr_call_id) if result.open_pr_call_id else None
            if (attempt is None or result.exit_code != 0 or not result.session_id
                    or initial_delivery_mismatch(job, attempt, result,
                                                delivery.model_copy(update={"state": DeliveryState.OPEN}), call)):
                return None, "INITIAL delivery incomplete; manual reconciliation required"
            proof = ReopenProof(delivery.head_sha, result.session_id)
        elif isinstance(result, RevisionTaskResult):
            task = frozen_revision(job)
            if (revision_mismatch(task, result) or task.attempt_id != flow.current_attempt_id
                    or task.repo != flow.repo or task.pr_number != delivery.pr_number
                    or task.branch != delivery.branch or task.base_branch != delivery.base_branch
                    or delivery.head_sha not in {task.expected_head_sha, result.commit_sha}):
                return None, "old revision does not prove the frozen delivery"
            assert result.commit_sha is not None  # canonical mismatch validation requires this evidence
            proof = ReopenProof(result.commit_sha, result.session_id, result.session_generation)
            revision = result
        else:
            return None, "unexpected coding result; manual reconciliation required"
    if remote.head_sha != proof.head_sha:
        return None, "PR head/base changed; verified delivery is unchanged"
    if revision is not None:
        mismatch = await verify_revision_remote(
            repo, flow, delivery.model_copy(update={"state": DeliveryState.OPEN}), proof.head_sha,
        )
        if mismatch:
            return None, mismatch
    else:
        ref = await repo.fetch_ref(delivery.branch)
        if ref.branch != delivery.branch or ref.sha != proof.head_sha:
            return None, "PR head/base and remote branch disagree"
    return proof, None


def resume_delivery(
    tx: Store, flow: Flow, delivery: FlowDelivery, proof: ReopenProof, settle_seconds: float,
) -> None:
    """Apply a freshly fenced proof; old batches stay cancelled and feedback is refrozen."""
    # Lease/reconnect recovery may have requeued an unstarted job after the initial pause.
    tx.cancel_jobs(flow.id, reason="PR reopened; stale unstarted work retired before refreezing")
    delivery.state, delivery.head_sha = DeliveryState.OPEN, proof.head_sha
    delivery.version += 1
    tx.update_delivery(delivery)
    if proof.session_id is not None:
        attempt = tx.get_current_attempt(flow)
        if attempt is None:
            raise ValueError("reopen attempt disappeared")
        attempt.codex_session_id = proof.session_id
        if proof.session_generation is not None:
            attempt.session_generation = proof.session_generation
        tx.update_attempt(attempt)
    tx.reviews.reopened(flow.id, settle_seconds)


def record_merge(tx: Store, flow: Flow, delivery: FlowDelivery, merge: PullRequestMerge) -> bool:
    """Apply verified merge facts under the caller's workflow lock, for webhook or poll."""
    if delivery.flow_id != flow.id or delivery.pr_number != merge.number:
        raise ValueError("merge does not belong to delivery")
    if ((delivery.merged_at is not None and delivery.merged_at != merge.merged_at)
            or (delivery.merge_commit_sha is not None
                and delivery.merge_commit_sha != merge.merge_commit_sha)):
        raise ValueError("merge event conflicts with recorded merge facts")
    first_merge = delivery.state is not DeliveryState.MERGED
    if first_merge or delivery.merged_at is None or delivery.merge_commit_sha is None:
        delivery.state = DeliveryState.MERGED
        delivery.merged_at = merge.merged_at
        delivery.merge_commit_sha = merge.merge_commit_sha
        delivery.version += 1
        tx.update_delivery(delivery)
    binding = tx.get_flow_execution(flow.id)
    if (first_merge and flow.state is FlowState.CANCELLED and binding is not None
            and binding.released_at is None and tx.stops.has_pending(flow.id)
            and tx.get_active_flow(flow.repo, flow.issue_number) is None):
        # Merge facts supersede cancellation only for archive on the retained original binding.
        flow.state = FlowState.ACTIVE
        tx.update_flow(flow)
    if first_merge and flow.state not in TERMINAL_STATES:
        tx.cancel_jobs(flow.id, reason="PR merged; queued work retired")
        tx.enqueue_job(flow.id, JobKind.ARCHIVE)
    return first_merge
