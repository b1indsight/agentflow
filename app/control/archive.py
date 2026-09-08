"""The archive command and receipt share one frozen ownership contract."""

from app.models import DeliveryState, Flow, FlowState, Job, JobKind, JobState
from app.protocol import ArchiveResult, ArchiveTask
from app.store import Store


def archive_task(store: Store, flow: Flow, job: Job) -> ArchiveTask:
    """Check current ownership and quiescence before freezing or accepting cleanup."""
    execution = store.get_flow_execution(flow.id)
    delivery = store.get_delivery(flow.id)
    if (flow.state is not FlowState.ACTIVE or flow.current_attempt_id is None
            or job.kind is not JobKind.ARCHIVE or job.state is not JobState.RUNNING
            or delivery is None or delivery.state is not DeliveryState.MERGED):
        raise ValueError("archive requires an active Flow with a merged delivery")
    if (execution is None or execution.released_at is not None
            or execution.worker_id != job.assigned_worker_id
            or execution.repo_identity != f"{execution.worker_id}:{flow.repo}"):
        raise ValueError("archive requires the original worker and repository binding")
    if any(prior.id != job.id and (prior.state is JobState.RUNNING or (
        prior.result_received_at is not None and prior.result_applied_at is None
    )) for prior in store.list_jobs(flow.id)):
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


def verify_archive(store: Store, flow: Flow, job: Job, result: ArchiveResult) -> None:
    """Accept cleanup only against both the dispatched command and current binding."""
    expected = archive_task(store, flow, job)
    if (job.started_at is None or "archive" not in (job.payload or {})
            or ArchiveTask.model_validate(result.model_dump(exclude={"cleanup"})) != expected):
        raise ValueError("archive receipt does not match its frozen command")


def archive_progress(store: Store, flow: Flow, jobs: list[Job]) -> str:
    """Derive operator-visible cleanup progress from existing jobs and frozen results."""
    archive = next((job for job in reversed(jobs) if job.kind is JobKind.ARCHIVE), None)
    if archive is None:
        return "blocked: no archive job; manual reconciliation required"
    if archive.result_applied_at is not None and archive.state is JobState.DONE:
        return f"complete at {archive.result_applied_at.isoformat()}"
    if archive.result_received_at is not None and archive.result_applied_at is None:
        plan = store.results.get_plan(archive.id)
        if plan is not None and plan.archive is not None:
            return "cleaned; waiting for atomic completion"
        return "result received; waiting for verification"
    if archive.state is JobState.FAILED:
        return "cleanup failed; original worker retained; use flow retry after resolving the error"
    if archive.state is JobState.RUNNING:
        return "blocked: original executor status unknown" if archive.recovery_deadline else "cleaning"
    if any(job.state is JobState.RUNNING for job in jobs):
        return "waiting for the original executor and its result"
    binding = store.get_flow_execution(flow.id)
    if binding is None or binding.released_at is not None:
        return "blocked: original binding is missing; manual reconciliation required"
    return "waiting for the original worker"
