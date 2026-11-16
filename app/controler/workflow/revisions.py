"""REVISION preparation, remote verification, and feedback result planning."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.contracts.protocol import RevisionTask, RevisionTaskResult, Succeeded
from app.contracts.receipts import decode_result_inbox
from app.contracts.result_models import CommentEffect, ResultPending, ResultPlan, RunUpdate
from app.controler.review.resolution import plan_resolution
from app.controler.workflow.plans import (
    delivered_pr_number,
    failed_plan,
    installation_id,
    review_effects,
)
from app.controler.workflow.preparation import (
    FailJob,
    PreparationReadFailure,
    PreparationReads,
    preparation_retired,
    required_delivery,
    required_pr_number,
)
from app.domain.models import (
    TERMINAL_STATES,
    AddressedComment,
    Attempt,
    DeferredComment,
    DeliveryState,
    FeedbackRef,
    Flow,
    FlowState,
    Job,
    JobKind,
    JobState,
    ReviewItemKind,
)
from app.domain.review_models import BatchState, ReviewBatch
from app.github import GitHubApp, GitHubError, PullRequest, Repository
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.results import ResultStore
from app.persistence.reviews import ReviewStore

if TYPE_CHECKING:
    from app.controler.config import ServerSettings


async def prepare_revision(
    job: Job, flow: Flow, attempt: Attempt, *, github: GitHubApp,
    reads: PreparationReads, flows: FlowStore, deliveries: DeliveryStore,
    reviews: ReviewStore, settings: ServerSettings, fail: FailJob,
) -> RevisionTask | JobState:
    """Read current PR facts outside locks, then freeze only a still-current assignment."""
    batch = reviews.for_job(job)
    if batch.attempt_id != attempt.id or batch.state not in {
        BatchState.READY, BatchState.PROCESSING,
    }:
        raise ValueError("review batch is no longer current")

    threads = None
    if batch.threads is None and any(f.kind is ReviewItemKind.INLINE for f in batch.feedback):
        assert flow.installation_id is not None
        repository = github.installation(flow.installation_id).repository(flow.repo)
        # Bind persisted identity; the thread query does not need current PR facts.
        pr = PullRequest(repository, required_pr_number(flow, deliveries=deliveries))
        try:
            threads = await reads.read(job, pr.list_review_threads)
        except GitHubError as exc:
            raise PreparationReadFailure(exc) from exc

    assert flow.installation_id is not None
    repository = github.installation(flow.installation_id).repository(flow.repo)
    delivery = required_delivery(flow, deliveries=deliveries)
    purpose = (job.payload or {}).get("purpose", "revise")
    if attempt.revision_cycles + 1 > settings.max_revision_cycles:
        await fail(job, "已达最大修订轮数，暂停。")
        return JobState.FAILED
    execution = flows.get_flow_execution(flow.id)
    if execution is None or execution.released_at is not None:
        await fail(job, "修订任务缺少有效 worker binding。")
        return JobState.FAILED
    try:
        remote = await reads.read(job, lambda: repository.fetch_pull_request(
            required_pr_number(flow, deliveries=deliveries),
        ))
        mismatch = (delivery.state is not DeliveryState.OPEN or not delivery.head_sha
            or remote.repo != flow.repo or remote.number != delivery.pr_number
            or remote.state != "open" or remote.merged
            or remote.head_repository != flow.repo or remote.head_branch != delivery.branch)
        if not mismatch and purpose == "revise":
            ref = await reads.read(job, lambda: repository.fetch_ref(delivery.branch))
            mismatch = ref.branch != delivery.branch
        if not mismatch:
            task_content = await reads.task_content(job, flow, repository)
    except GitHubError as exc:
        raise PreparationReadFailure(exc) from exc
    if mismatch:
        await fail(job, "original PR or source branch changed; revision paused")
        return JobState.FAILED
    assert flow.installation_id is not None
    assert delivery.head_sha and delivery.base_branch and delivery.pr_number
    task = RevisionTask(
        batch_id=batch.id,
        purpose=purpose,
        flow_id=flow.id,
        attempt_id=attempt.id,
        repo=flow.repo,
        branch=delivery.branch,
        repo_identity=execution.repo_identity,
        pr_number=delivery.pr_number,
        base_branch=remote.base_branch,
        expected_head_sha=remote.head_sha,
        previous_head_sha=delivery.head_sha,
        session_id=attempt.codex_session_id,
        session_generation=attempt.session_generation,
        task_content=task_content,
        previous_summary=attempt.last_revision_summary,
        open_points=batch.open_points,
        context=batch.context,
        instruction=(job.payload or {}).get("instruction", "Address the review feedback."),
        feedback=batch.feedback,
    )
    with flows.locked(flow.id) as locked:
        tx = locked.database
        deliveries_tx = deliveries.using(tx)
        reviews_tx = reviews.using(tx)
        flows_tx = flows.using(tx)
        fresh = locked.flow
        current = tx.lock_job(job.id)
        binding = flows_tx.get_flow_execution(flow.id)
        latest = deliveries_tx.get_delivery(flow.id)
        if (
            fresh is None
            or current is None
            or preparation_retired(fresh, job, deliveries=deliveries_tx)
            or fresh.current_attempt_id != attempt.id
        ):
            return JobState.DONE
        if (binding != execution or latest != delivery
                or current.assignment_token != job.assignment_token
                or current.assigned_worker_id != job.assigned_worker_id
                or current.state is not JobState.RUNNING
                or current.started_at is not None or current.dispatch_authorized_at is not None
                or current.result_payload is not None
                or (job.assignment_token is not None and (
                    current.lease_expires_at is None
                    or current.lease_expires_at <= tx.now()))):
            return JobState.QUEUED
        if batch.context is not None and (
            batch.context.pull_request is None
            or (batch.context.pull_request.head_sha, batch.context.pull_request.base_sha,
                batch.context.pull_request.base_branch)
            != (remote.head_sha, remote.base_sha, remote.base_branch)
        ):
            # A Judge instruction belongs to its exact context; let triage rebuild both.
            reviews_tx.invalidate_context(batch, batch.context, job=current)
            return JobState.DONE
        if threads is not None:
            reviews_tx.freeze_threads(current, threads)
        return reviews_tx.freeze_revision(current, task)


async def verify_revision_result(
    job: Job, *, github: GitHubApp, database: Database,
    flows: FlowStore, deliveries: DeliveryStore, result_store: ResultStore,
) -> str | None:
    """Perform network reads outside the result transaction, once per frozen plan."""
    if (job.kind is not JobKind.REVISION or job.result_applied_at is not None
            or job.result_payload is None or result_store.get_plan(job.id) is not None):
        return None
    flow = flows.get_flow(job.required_flow_id)
    if flow is None or flow.state in TERMINAL_STATES:
        return None
    delivery = deliveries.get_delivery(flow.id)
    if flow.retires(job, delivery):
        return None
    if job.result_digest is None:
        raise ValueError("result inbox is missing its digest")
    message = decode_result_inbox(
        job.result_payload,
        job_id=job.id,
        assignment_token=job.assignment_token,
        result_digest=job.result_digest,
    ).terminal
    if not isinstance(message, Succeeded) or not isinstance(message.result, RevisionTaskResult):
        return None
    result = message.result
    task = frozen_revision(job)
    mismatch = revision_mismatch(task, result)
    if mismatch:
        return mismatch
    delivery = deliveries.get_delivery(flow.id)
    if delivery is None or delivery.head_sha != (task.previous_head_sha or task.expected_head_sha):
        return "revision baseline no longer matches delivery"
    if job.verify_next_at is not None and job.verify_next_at > database.now():
        raise ResultPending(job.verify_last_error or "revision verification backoff")
    assert result.commit_sha is not None
    if flow.installation_id is None:
        return "revision has no open verified delivery"
    try:
        repo = github.installation(flow.installation_id).repository(flow.repo)
        if task.purpose == "answer":
            return await verify_answer_remote(repo, flow, task)
        return await verify_revision_remote(repo, flow, task, result)
    except GitHubError as error:
        if error.retryable:
            result_store.defer_verification(job, error)
            raise ResultPending(str(error)) from error
        return str(error)


def revision_plan(
    base: ResultPlan, flow: Flow, job: Job, attempt: Attempt, batch: ReviewBatch,
    result: RevisionTaskResult, revision_error: str | None, *,
    deliveries: DeliveryStore, flows: FlowStore, reviews: ReviewStore,
    in_review_label: str, reviewers: Sequence[str],
) -> ResultPlan:
    """Freeze accepted revision facts, exact feedback consumption, and ordered effects."""
    expected = [(f.item_id, f.version) for f in batch.feedback]
    received = [(f.item_id, f.version) for f in result.feedback_refs]
    if result.batch_id != batch.id or received != expected:
        return base.model_copy(
            update={
                "state": JobState.FAILED,
                "flow_state": FlowState.BLOCKED,
                "error": "result does not match its frozen review batch",
            }
        )
    base = base.model_copy(
        update={
            "run": RunUpdate(
                kind="revision",
                session_id=result.session_id,
                exit_code=result.exit_code,
            )
        }
    )
    if result.exit_code != 0:
        return failed_plan(
            base,
            f"修订失败（exit={result.exit_code}, stage={result.failure_stage}）："
            f"{result.failure_detail or '执行结果不确定'}。反馈保留，需核对工作区和远端。",
        )
    task = frozen_revision(job)
    delivery = deliveries.get_delivery(flow.id)
    execution = flows.get_flow_execution(flow.id)
    mismatch = revision_error or revision_mismatch(task, result)
    if mismatch:
        return failed_plan(base, mismatch)
    if (delivery is None or delivery.head_sha != (task.previous_head_sha or task.expected_head_sha)
            or delivery.state is not DeliveryState.OPEN
            or delivery.pr_number != task.pr_number or delivery.branch != task.branch
            or attempt.codex_session_id != task.session_id
            or attempt.session_generation != task.session_generation
            or execution is None or execution.released_at is not None
            or execution.repo_identity != task.repo_identity):
        return failed_plan(base, "revision assignment is no longer current")
    assert result.output is not None
    number = delivered_pr_number(flow, deliveries=deliveries)
    members = {(f.item_id, f.version): f for f in batch.feedback}
    reported: list[AddressedComment | DeferredComment] = [
        *result.output.addressed, *result.output.deferred,
    ]
    keys = [(f.item_id, f.version) for f in reported]
    if (len(keys) != len(set(keys)) or any(
        key not in members or members[key].github_id != item.comment_id
        for key, item in zip(keys, reported, strict=True)
    )):
        return failed_plan(base, "revision reported unknown or conflicting feedback")
    consumed = [FeedbackRef(item_id=f.item_id, version=f.version) for f in result.output.addressed]
    # Empty review summaries carry metadata, not an additional coding request.
    reported_keys = set(keys)
    consumed.extend(
        FeedbackRef(item_id=f.item_id, version=f.version) for f in batch.feedback
        if not f.body.strip()
        and (f.kind is ReviewItemKind.REVIEW_BODY or f.signal == "request_changes")
        and (f.item_id, f.version) not in reported_keys
    )
    unresolved = (len(consumed) != len(batch.feedback)
                  or bool(result.output.needs_clarification))
    notes = plan_resolution(flow, number, batch, result,
                            reviews.addressed(flow.id, attempt.id), purpose=task.purpose)
    blocked = {(f.item_id, f.version) for f in notes.blocked}
    consumed = [f for f in consumed if (f.item_id, f.version) not in blocked]
    unresolved |= bool(notes.problems)
    effects = notes.effects
    new_commit = task.purpose == "revise" and result.commit_sha != (
        result.baseline_sha or task.expected_head_sha
    )
    if result.output.summary:
        effects.append(
            CommentEffect(
                key="summary",
                installation_id=installation_id(flow),
                repo=flow.repo,
                number=number,
                body=f"问题答复：{result.output.summary}" if task.purpose == "answer" else
                f"修订交付（{'已核验新提交' if new_commit else '无新提交'}）：{result.output.summary}",
            )
        )
    if not unresolved and new_commit:
        effects.extend(review_effects(flow, number, in_review_label=in_review_label, reviewers=reviewers))
    return base.model_copy(
        update={
            "effects": effects,
            "delivery": delivery,
            "revision": result,
            "revision_purpose": task.purpose,
            "batch_state": BatchState.WAITING if unresolved else BatchState.COMPLETED,
            "consumed": consumed,
            "open_points": [*notes.problems, *result.output.needs_clarification,
                            *(item.reason for item in result.output.deferred)],
        }
    )


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
