"""Prepare archive work and convert cleanup receipts or merge notices to frozen plans."""

from datetime import UTC

from app.contracts.protocol import ArchiveResult, ArchiveTask
from app.contracts.receipts import MergeArchiveNotice
from app.contracts.result_models import CommentEffect, Effect, EffectState, LabelEffect, ResultPlan
from app.controler.workflow.plans import failed_plan
from app.controler.workflow.preparation import FailJob
from app.domain.models import DeliveryState, Flow, FlowDelivery, FlowState, Job, JobKind, JobState
from app.persistence.activity import ActivityStore
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.results import ResultStore


async def prepare_archive(
    job: Job, flow: Flow, *, flows: FlowStore, deliveries: DeliveryStore,
    jobs: JobStore, fail: FailJob,
) -> ArchiveTask | JobState:
    """Freeze cleanup under the workflow lock, reporting failure after the transaction exits."""
    try:
        with flows.locked(flow.id) as locked:
            tx = locked.database
            deliveries_tx = deliveries.using(tx)
            jobs_tx = jobs.using(tx)
            flows_tx = flows.using(tx)
            fresh = locked.flow
            current = tx.lock_job(job.id)
            if fresh is None or current is None:
                raise ValueError("archive workflow disappeared")
            archive = archive_task(
                fresh, current, deliveries=deliveries_tx, flows=flows_tx, jobs=jobs_tx
            )
            jobs_tx.freeze_archive(current, archive)
            return archive
    except ValueError as error:
        await fail(job, str(error))
        return JobState.FAILED


def merge_notice_plan(
    base: ResultPlan, flow: Flow, job: Job, notice: MergeArchiveNotice, *,
    activity: ActivityStore, done_label: str, in_review_label: str,
) -> ResultPlan:
    """Build control notification intent independently of worker cleanup and retirement."""
    verify_merge_notice(flow, job, notice, activity=activity)
    return base.model_copy(update={
        "archive_notice": notice,
        "effects": archive_effects(
            flow, notice, done_label=done_label, in_review_label=in_review_label,
        ) if flow.installation_id is not None else [],
    })


def archive_plan(
    base: ResultPlan, flow: Flow, job: Job, result: ArchiveResult, *,
    deliveries: DeliveryStore, flows: FlowStore, jobs: JobStore,
    done_label: str, in_review_label: str,
) -> ResultPlan:
    """Convert a verified cleanup receipt into its local outcome and ordered notifications."""
    try:
        verify_archive(flow, job, result, deliveries=deliveries, flows=flows, jobs=jobs)
    except ValueError as error:
        return failed_plan(base, str(error))
    delivery = deliveries.get_delivery(flow.id)
    assert delivery is not None
    return base.model_copy(
        update={
            "flow_state": FlowState.ARCHIVED,
            "archive": result,
            "effects": archive_effects(
                flow, delivery, done_label=done_label, in_review_label=in_review_label,
            ),
        }
    )


def archive_task(
    flow: Flow, job: Job, *, deliveries: DeliveryStore, flows: FlowStore, jobs: JobStore
) -> ArchiveTask:
    """Check current ownership and quiescence before freezing or accepting cleanup."""
    execution = flows.get_flow_execution(flow.id)
    delivery = deliveries.get_delivery(flow.id)
    if (
        flow.state not in {FlowState.FINALIZING, FlowState.BLOCKED}
        or flow.current_attempt_id is None
        or job.attempt_id != flow.current_attempt_id
        or job.kind is not JobKind.ARCHIVE
        or job.state is not JobState.RUNNING
        or delivery is None
        or delivery.state is not DeliveryState.MERGED
    ):
        raise ValueError("archive requires an active Flow with a merged delivery")
    if (execution is None or execution.released_at is not None
            or execution.worker_id != job.assigned_worker_id
            or execution.repo_identity != f"{execution.worker_id}:{flow.repo}"):
        raise ValueError("archive requires the original worker and repository binding")
    if jobs.has_unsettled_jobs(flow.id, excluding_job_id=job.id):
        raise ValueError("archive is blocked by an executor or unapplied result")
    task = ArchiveTask(
        flow_id=flow.id, attempt_id=flow.current_attempt_id, repo=flow.repo,
        worker_id=execution.worker_id, repo_identity=execution.repo_identity,
        delivery_version=delivery.version,
    )
    frozen = (job.payload or {}).get("archive")
    if frozen is not None and ArchiveTask.model_validate(frozen) != task:
        raise ValueError("archive ownership changed after dispatch")
    return task


def verify_archive(
    flow: Flow,
    job: Job,
    result: ArchiveResult,
    *,
    deliveries: DeliveryStore,
    flows: FlowStore,
    jobs: JobStore,
) -> None:
    """Accept cleanup only against both the dispatched command and current binding."""
    expected = archive_task(flow, job, deliveries=deliveries, flows=flows, jobs=jobs)
    if (job.started_at is None or "archive" not in (job.payload or {})
            or ArchiveTask.model_validate(result.model_dump(exclude={"cleanup"})) != expected):
        raise ValueError("archive receipt does not match its frozen command")


def verify_merge_notice(
    flow: Flow, job: Job, notice: MergeArchiveNotice, *, activity: ActivityStore,
) -> None:
    """Authorize only an unassigned Control notice backed by immutable archive facts."""
    archived = activity.archived(flow.id, notice.attempt_id)
    if (job.kind is not JobKind.ARCHIVE or notice.job_id != job.id
            or notice.flow_id != flow.id or notice.repo != flow.repo
            or notice.attempt_id != job.attempt_id
            or job.started_at is not None or job.dispatch_authorized_at is not None
            or any(value is not None for value in (
                job.assigned_worker_id, job.assignment_token, job.workspace, job.generation,
            ))
            or archived is None
            or archived.metadata.get("archive_reason") != "succeeded"
            or archived.metadata.get("repo") != notice.repo
            or archived.metadata.get("pr_number") != notice.pr_number
            or archived.metadata.get("merge_commit_sha") != notice.merge_commit_sha
            or archived.metadata.get("merged_at") != notice.merged_at.isoformat()):
        raise ValueError("merge notice does not match its archived attempt")


def archive_effects(
    flow: Flow, delivery: FlowDelivery | MergeArchiveNotice, *, done_label: str, in_review_label: str,
) -> list[Effect]:
    """Freeze deterministic completion messages and labels for the PR and any linked Issue."""
    if (flow.installation_id is None or delivery.pr_number is None
            or delivery.merge_commit_sha is None or delivery.merged_at is None):
        raise ValueError("archive notification requires installation and persisted merge facts")
    issue_line = f"关联 Issue：{flow.repo}#{flow.issue_number}\n" if flow.issue_number is not None else ""
    body = (
        f"✅ Flow {flow.id} 的 PR 已合并，本次执行已归档\n\n"
        f"{issue_line}"
        f"关联 PR：{flow.repo}#{delivery.pr_number}\n"
        f"合并 SHA：`{delivery.merge_commit_sha}`\n"
        f"合并时间：{delivery.merged_at.astimezone(UTC).isoformat()}\n\n"
        "工作区由原 Worker 异步回收；本通知不代表工作区清理已完成。"
    )
    effects: list[Effect] = []
    for number in (flow.issue_number, delivery.pr_number):
        if number is None:
            continue
        key = f"archive:{delivery.pr_number}:{number}"
        effects.extend([
            LabelEffect(key=f"{key}:done", installation_id=flow.installation_id,
                        repo=flow.repo, number=number, label=done_label, present=True),
            LabelEffect(key=f"{key}:in-review", installation_id=flow.installation_id,
                        repo=flow.repo, number=number, label=in_review_label, present=False),
            CommentEffect(key=f"{key}:comment", installation_id=flow.installation_id,
                          repo=flow.repo, number=number, body=body),
        ])
    return effects


def archive_progress(
    flow: Flow, jobs: list[Job], *, result_store: ResultStore, flows: FlowStore
) -> str:
    """Derive operator-visible cleanup progress from existing jobs and frozen results."""
    archive = next((job for job in reversed(jobs)
                    if job.kind is JobKind.ARCHIVE and job.attempt_id == flow.current_attempt_id), None)
    if flow.state is FlowState.ARCHIVED:
        binding = flows.get_flow_execution(flow.id)
        cleanup = ("execution binding released" if binding is None or binding.released_at is not None
                   else "workspace cleanup pending on original worker")
        notification = ("no notification intent" if archive is None
                        else "GitHub notification complete" if archive.result_applied_at is not None
                        else "GitHub notification failed; use flow retry" if archive.last_error
                        else "GitHub notification pending")
        return f"archived; {cleanup}; {notification}"
    if archive is None:
        return "blocked: no archive job; manual reconciliation required"
    if archive.result_applied_at is not None and archive.state is JobState.DONE:
        plan = result_store.get_plan(archive.id)
        retained = "; workspace retained for reuse" if (
            plan is not None and plan.archive is not None and plan.archive.cleanup == "released"
        ) else ""
        return f"complete at {archive.result_applied_at.isoformat()}{retained}"
    if archive.result_received_at is not None and archive.result_applied_at is None:
        plan = result_store.get_plan(archive.id)
        if plan is not None and plan.archive is not None:
            cleanup = ("released (workspace retained)" if plan.archive.cleanup == "released"
                       else "cleaned")
            unfinished = [record for record in result_store.list_effects(archive.id)
                          if record.state not in {EffectState.SUCCEEDED, EffectState.SKIPPED}]
            if unfinished:
                first = unfinished[0]
                reason = first.last_error or first.state.value
                retry = ("; use flow retry after resolving the error"
                         if first.state is EffectState.FAILED else "")
                return (f"{cleanup}; GitHub status pending ({len(unfinished)} effects): "
                        f"{first.effect_key}: {reason}{retry}")
            return f"{cleanup}; waiting for atomic completion"
        return "result received; waiting for verification"
    if archive.state is JobState.FAILED:
        return "cleanup failed; original worker retained; use flow retry after resolving the error"
    if archive.state is JobState.RUNNING:
        return "blocked: original executor status unknown" if archive.recovery_deadline else "cleaning"
    if any(job.state is JobState.RUNNING for job in jobs):
        return "waiting for the original executor and its result"
    binding = flows.get_flow_execution(flow.id)
    if binding is None or binding.released_at is not None:
        return "blocked: original binding is missing; manual reconciliation required"
    return "waiting for the original worker"
