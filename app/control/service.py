"""Control-plane use cases triggered by GitHub events and the admin CLI."""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from app.github_models import PullRequestMerge
from app.models import (
    TERMINAL_STATES,
    DeliveryState,
    Flow,
    FlowState,
    JobKind,
    JobState,
    WorkerStatus,
    WorkerView,
)
from app.plugins import get_plugin_manager
from app.store import Store


class ControlService:
    def __init__(
        self,
        store: Store,
        plugins=None,
        jobs_available: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.plugins = plugins or get_plugin_manager()
        self._jobs_available = jobs_available or (lambda: None)

    def _fire(self, hook: str, **kwargs: object) -> None:
        try:
            getattr(self.plugins.hook, hook)(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugin hook {} failed: {}", hook, exc)

    def on_trigger(self, repo: str, issue_number: int, installation_id: int | None) -> Flow | None:
        with self.store.transaction() as tx:
            tx.serialize_issue(repo, issue_number)
            if tx.get_active_flow(repo, issue_number):
                logger.info("flow already active for {}#{}", repo, issue_number)
                return None
            flow = tx.create_flow(repo, issue_number, installation_id)
            tx.enqueue_job(flow.id, JobKind.INITIAL, payload={"issue_number": issue_number})
        self._jobs_available()
        logger.info("flow {} created (ACTIVE) for {}#{}", flow.id, repo, issue_number)
        return flow

    def cancel(self, flow: Flow) -> None:
        with self.store.transaction() as tx:
            fresh = tx.lock_workflow(flow.id)
            if fresh is None or fresh.state in TERMINAL_STATES:
                return
            delivery = tx.get_delivery(fresh.id)
            if delivery is not None and delivery.state is DeliveryState.MERGED:
                raise ValueError("a merged Flow must finish archive before its worker can be released")
            fresh.state = FlowState.CANCELLED
            tx.update_flow(fresh)
            tx.release_flow_execution(fresh.id)
            tx.cancel_jobs(fresh.id)
        self._jobs_available()
        logger.info("flow {} cancelled", flow.id)

    def on_merged(self, flow: Flow, delivery_id: str, payload: dict) -> None:
        """Commit the webhook reservation, merge fact and retirement of old work together."""
        merge = PullRequestMerge.model_validate(payload["pull_request"])
        with self.store.transaction() as tx:
            fresh = tx.lock_workflow(flow.id)
            delivery = tx.get_delivery(flow.id)
            if (fresh is None or delivery is None or payload.get("action") != "closed"
                    or (payload.get("repository") or {}).get("full_name") != fresh.repo
                    or fresh.installation_id is None
                    or (payload.get("installation") or {}).get("id") != fresh.installation_id
                    or delivery.pr_number != merge.number):
                logger.warning("merge identity does not match flow {}", flow.id)
                return
            if not tx.record_event(delivery_id, "pull_request", fresh.id, payload):
                return
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
            if not first_merge:
                return
            if fresh.state not in TERMINAL_STATES:
                tx.cancel_jobs(flow.id, reason="PR merged; queued work retired")
                tx.enqueue_job(flow.id, JobKind.ARCHIVE)
        self._fire("on_merged", flow=fresh)
        self._jobs_available()

    def retry(self, flow: Flow) -> None:
        with self.store.transaction() as tx:
            tx.serialize_issue(flow.repo, flow.issue_number)
            fresh = tx.lock_workflow(flow.id)
            if fresh is None:
                raise ValueError(f"flow {flow.id} disappeared")
            delivery = tx.get_delivery(fresh.id)
            if delivery is not None and delivery.state is DeliveryState.MERGED:
                self._retry_archive(tx, fresh)
            elif tx.has_pending_result(flow.id):
                if fresh.state is not FlowState.ACTIVE or not tx.results.retry_delivery_effects(fresh):
                    raise ValueError("Flow has a pending result; reconcile its effects before retry")
            else:
                active = tx.get_active_flow(fresh.repo, fresh.issue_number)
                if active is not None and active.id != fresh.id:
                    raise ValueError(
                        f"flow {active.id} is already active for {fresh.repo}#{fresh.issue_number}"
                    )
                if fresh.state is FlowState.ACTIVE:
                    return
                if fresh.state is FlowState.SUCCEEDED:
                    raise ValueError("a succeeded Flow cannot be retried")
                # Recovery may have failed the Flow before its queued work was swept.
                tx.cancel_jobs(fresh.id)
                failed_job = tx.latest_failed_job(fresh.id)
                kind = failed_job.kind if failed_job is not None else JobKind.INITIAL
                if kind is JobKind.ANSWER:
                    raise ValueError("Reconcile legacy ANSWER jobs before upgrading to protocol v6")
                delivery = tx.get_delivery(fresh.id)
                if delivery is not None and delivery.supersedes(kind):
                    raise ValueError("a merged PR cannot retry coding or review jobs")
                fresh.state = FlowState.ACTIVE
                tx.update_flow(fresh)
                if (failed_job is not None and kind is JobKind.TRIAGE
                        and tx.reviews.for_job(failed_job).context_stale):
                    tx.reviews.retriage(failed_job)
                else:
                    replacement = tx.enqueue_job(
                        fresh.id,
                        kind,
                        payload=(
                            failed_job.payload if failed_job else {"issue_number": fresh.issue_number}
                        ),
                    )
                    if failed_job is not None and kind in {JobKind.TRIAGE, JobKind.REVISION}:
                        tx.reviews.retry(failed_job, replacement)
        self._jobs_available()

    @staticmethod
    def _retry_archive(tx: Store, flow: Flow) -> None:
        """Retry only a settled archive on its original binding and current Attempt."""
        if flow.state is not FlowState.ACTIVE:
            raise ValueError("a merged PR cannot retry coding or review jobs or terminal Flows")
        execution = tx.get_flow_execution(flow.id)
        if execution is None or execution.released_at is not None:
            raise ValueError("archive retry requires the original active binding")
        if tx.has_pending_result(flow.id):
            if tx.results.retry_delivery_effects(flow):
                return
            raise ValueError("archive has a pending result; wait for result reconciliation")
        jobs = tx.list_jobs(flow.id)
        if any(job.state is JobState.RUNNING for job in jobs):
            raise ValueError("archive retry is blocked by a running or unknown executor")
        if any(job.kind is JobKind.ARCHIVE and job.state is JobState.QUEUED for job in jobs):
            raise ValueError("archive is already queued; wait for the original worker")
        workers = tx.list_workers()
        worker = next((w for w in workers if w.worker_id == execution.worker_id), None)
        if worker is None or worker.status is not WorkerStatus.ONLINE or worker.current_job_id is not None:
            raise ValueError("archive retry requires the original worker online with no executor")
        failed = next((job for job in reversed(jobs)
                       if job.kind is JobKind.ARCHIVE and job.state is JobState.FAILED), None)
        if failed is None:
            raise ValueError("Flow has no failed archive to retry")
        tx.enqueue_job(flow.id, JobKind.ARCHIVE, payload=failed.payload)

    def notify_jobs(self) -> None:
        self._jobs_available()

    def list_workers(self) -> list[WorkerView]:
        views: list[WorkerView] = []
        for worker in self.store.list_workers():
            execution = self.store.get_worker_execution(worker.worker_id)
            job = (
                self.store.get_job(worker.current_job_id)
                if worker.current_job_id is not None
                else None
            )
            views.append(
                WorkerView(
                    worker_id=worker.worker_id,
                    status=worker.status,
                    current_job_id=worker.current_job_id,
                    current_flow_id=(
                        execution.flow_id
                        if execution is not None
                        else job.flow_id
                        if job is not None
                        else None
                    ),
                    last_seen_at=worker.last_seen_at,
                    last_error=worker.last_error,
                    connection_epoch=worker.connection_epoch,
                    lease_expires_at=job.lease_expires_at if job is not None else None,
                    recovery_deadline=(
                        job.recovery_deadline
                        if job is not None and job.recovery_deadline is not None
                        else execution.recovery_deadline
                        if execution is not None
                        else None
                    ),
                    last_disconnect_reason=worker.last_disconnect_reason,
                )
            )
        return views
