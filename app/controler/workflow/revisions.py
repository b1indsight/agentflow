"""Validate frozen revision ownership and current remote delivery evidence."""

from app.contracts.protocol import RevisionTask, RevisionTaskResult
from app.domain.models import Flow, Job
from app.github import Repository


def frozen_revision(job: Job) -> RevisionTask:
    """Read the authoritative assignment, never reconstruct a baseline from worker claims."""
    return RevisionTask.model_validate((job.payload or {}).get("revision"))


def revision_mismatch(task: RevisionTask, result: RevisionTaskResult) -> str | None:
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
            or result.failure_stage is not None or not result.commit_sha
            or result.vcs_backend != result.output.vcs_backend_used):
        return "revision lacks successful worker execution evidence"
    if task.purpose == "revise" and (not result.clean_worktree or result.branch != task.branch
            or result.commit_sha != result.remote_sha):
        return "revision lacks verified clean repository and remote delivery evidence"
    if task.purpose == "revise" and task.previous_head_sha is not None and not result.baseline_sha:
        return "revision lacks its synchronized coding baseline"
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


async def verify_answer_remote(
    repo: Repository, flow: Flow, task: RevisionTask,
) -> str | None:
    """Answer checks only the original open PR's code head, not refs or ancestry."""
    if flow.installation_id is None:
        return "answer has no open verified delivery"
    pr = await repo.fetch_pull_request(task.pr_number)
    if (pr.repo != task.repo or pr.number != task.pr_number or pr.state != "open" or pr.merged
            or pr.head_repository != task.repo or pr.head_branch != task.branch
            or pr.base_branch != task.base_branch or pr.head_sha != task.expected_head_sha):
        return "original PR closed or head changed; answer paused"
    return None


async def verify_revision_remote(
    repo: Repository, flow: Flow, task: RevisionTask, result: RevisionTaskResult,
) -> str | None:
    """Verify publication against the actual fetched baseline, never historical delivery ancestry."""
    if flow.installation_id is None:
        return "revision has no open verified delivery"
    baseline = result.baseline_sha
    if baseline is None and task.previous_head_sha is None:
        baseline = task.expected_head_sha  # Historical tasks predate the separate fetched baseline.
    if not baseline or not result.commit_sha:
        return "revision lacks its synchronized coding baseline"
    pr = await repo.fetch_pull_request(task.pr_number)
    ref = await repo.fetch_ref(task.branch)
    if (pr.repo != task.repo or pr.number != task.pr_number or pr.state != "open" or pr.merged
            or pr.head_repository != flow.repo or pr.head_branch != task.branch
            or pr.base_branch != task.base_branch or ref.branch != task.branch
            or pr.head_sha != result.commit_sha or ref.sha != result.commit_sha):
        return "original PR or remote branch changed; revision paused"
    if result.commit_sha != baseline and not await repo.compare_commits(baseline, result.commit_sha):
        return "revision commit does not advance the synchronized coding baseline"
    return None
