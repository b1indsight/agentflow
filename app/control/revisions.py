"""Read-only revision delivery checks shared by assignment and result application."""

from app.models import DeliveryState, Flow, FlowDelivery, Job
from app.ports import GitHubClient
from app.protocol import RevisionResult, RevisionTask


def frozen_revision(job: Job) -> RevisionTask:
    """Read the authoritative assignment, never reconstruct a baseline from worker claims."""
    return RevisionTask.model_validate((job.payload or {}).get("revision"))


def revision_mismatch(task: RevisionTask, result: RevisionResult) -> str | None:
    """Validate worker evidence before any remote effect or local acknowledgement."""
    if (result.flow_id, result.attempt_id, result.batch_id) != (
        task.flow_id, task.attempt_id, task.batch_id,
    ):
        return "revision identity does not match the frozen assignment"
    if [(f.item_id, f.version) for f in result.feedback_refs] != [
        (f.item_id, f.version) for f in task.feedback
    ]:
        return "revision feedback does not match the frozen assignment"
    if (result.exit_code != 0 or result.output is None or not result.output.summary.strip()
            or result.failure_stage is not None or not result.clean_worktree
            or result.branch != task.branch or not result.commit_sha
            or result.commit_sha != result.remote_sha
            or result.vcs_backend != result.output.vcs_backend_used):
        return "revision lacks verified clean repository and remote delivery evidence"
    if result.previous_session_id != task.session_id or not result.session_id:
        return "revision session does not match the frozen assignment"
    if result.mode == "resume":
        if (result.session_id != task.session_id
                or result.session_generation != task.session_generation
                or len(result.invocations) != 1):
            return "invalid resumed session evidence"
    else:
        if (result.session_generation != task.session_generation + 1
                or result.session_id == task.session_id
                or len(result.invocations) != (2 if task.session_id else 1)):
            return "invalid fallback session evidence"
        if task.session_id:
            first = result.invocations[0]
            if (first.mode != "resume" or first.session_id != task.session_id
                    or first.failure != "session_unavailable" or first.exit_code == 0):
                return "fallback did not follow a proven session rejection"
    last = result.invocations[-1]
    if (last.mode != result.mode or last.session_id != result.session_id
            or last.exit_code != 0 or last.failure is not None):
        return "revision invocation does not match accepted session"
    return None


async def verify_revision_remote(
    github: GitHubClient, flow: Flow, delivery: FlowDelivery, head_sha: str,
) -> str | None:
    """Require the original open PR and exact branch/ref SHA; API errors remain retryable."""
    if (flow.installation_id is None or delivery.state is not DeliveryState.OPEN
            or delivery.pr_number is None or not delivery.base_branch or not delivery.head_sha):
        return "revision has no open verified delivery"
    pr = await github.get_review_pull_request(flow.installation_id, flow.repo, delivery.pr_number)
    ref = await github.get_ref(flow.installation_id, flow.repo, delivery.branch)
    if (pr.number != delivery.pr_number or pr.state != "open" or pr.merged
            or pr.head_repository != flow.repo
            or pr.head_branch != delivery.branch or pr.base_branch != delivery.base_branch
            or ref.branch != delivery.branch or pr.head_sha != head_sha or ref.sha != head_sha):
        return "original PR or remote branch changed; revision paused"
    if head_sha != delivery.head_sha and not await github.compare_commits(
        flow.installation_id, flow.repo, delivery.head_sha, head_sha,
    ):
        return "revision commit does not advance the frozen delivery head"
    return None
