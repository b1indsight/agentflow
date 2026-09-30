"""Pause delivery intent and verify the original repository before resuming a closed PR."""

import json
from dataclasses import dataclass

from app.contracts.protocol import InitialResult, RevisionResult, Succeeded, parse_worker_message
from app.control.initial import initial_delivery_mismatch
from app.control.revisions import frozen_revision, revision_mismatch, verify_revision_remote
from app.domain.github_models import PullRequestStatus
from app.domain.models import DeliveryState, Flow, FlowDelivery, Job
from app.domain.ports import GitHubClient
from app.persistence.store import Store


@dataclass(frozen=True)
class ReopenProof:
    head_sha: str
    session_id: str | None = None
    session_generation: int | None = None


def pause_delivery(tx: Store, delivery: FlowDelivery) -> None:
    """Runs under the Flow lock; leave running executors and the binding untouched."""
    if delivery.state in {DeliveryState.CLOSED, DeliveryState.MERGED}:
        return
    delivery.state = DeliveryState.CLOSED
    delivery.version += 1
    tx.update_delivery(delivery)
    tx.cancel_jobs(delivery.flow_id, reason="PR closed or baseline changed; queued work retired")


async def prove_reopen(
    store: Store, github: GitHubClient, flow: Flow, delivery: FlowDelivery,
    remote: PullRequestStatus, job: Job | None,
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
        if isinstance(result, InitialResult):
            attempt = store.get_current_attempt(flow)
            call = store.get_tool_call(result.open_pr_call_id) if result.open_pr_call_id else None
            if (attempt is None or result.exit_code != 0 or not result.session_id
                    or initial_delivery_mismatch(job, attempt, result,
                                                delivery.model_copy(update={"state": DeliveryState.OPEN}), call)):
                return None, "INITIAL delivery incomplete; manual reconciliation required"
            proof = ReopenProof(delivery.head_sha, result.session_id)
        elif isinstance(result, RevisionResult):
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
            github, flow, delivery.model_copy(update={"state": DeliveryState.OPEN}), proof.head_sha,
        )
        if mismatch:
            return None, mismatch
    else:
        ref = await github.get_ref(flow.installation_id, flow.repo, delivery.branch)
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
