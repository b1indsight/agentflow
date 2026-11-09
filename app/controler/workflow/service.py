"""Control-plane use cases triggered by GitHub events and the admin CLI."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Literal

from loguru import logger
from pluggy import PluginManager

from app.controler.workflow.archive import verify_archive
from app.controler.workflow.lifecycle import close_delivery, record_merge
from app.domain.models import (
    WORKING_STATES,
    ArchiveReason,
    DeliveryState,
    Flow,
    FlowState,
    JobKind,
    JobState,
    WorkerStatus,
    WorkerView,
)
from app.github import PullRequestMerge
from app.persistence.activity import ActivityStore
from app.persistence.console_sync import ConsoleSyncStore
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.reconciliation import ReconcileStore
from app.persistence.results import ResultStore
from app.persistence.reviews import ReviewStore
from app.persistence.stops import StopStore
from app.persistence.webhooks import WebhookStore
from app.persistence.workers import WorkerStore
from app.plugins import emit_hook, get_plugin_manager

type RetryFailureCode = Literal[
    "flow_not_found",
    "retry_stop_pending",
    "retry_delivery_closed",
    "retry_result_pending",
    "retry_flow_conflict",
    "retry_delivery_merged",
    "retry_archive_inactive",
    "retry_archive_binding_missing",
    "retry_execution_unsettled",
    "retry_archive_worker_unavailable",
    "retry_archive_missing",
    "retry_archived",
]


class RetryRejected(ValueError):
    """Expected retry refusal; public clients consume the code, not the internal message."""

    def __init__(self, code: RetryFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ControlService:
    def __init__(
        self,
        plugins: PluginManager | None = None,
        jobs_available: Callable[[], None] | None = None,
        *,
        after_commit: list[Callable[[], None]] | None = None,
        database: Database,
    ) -> None:
        self.database = database
        self.deliveries = DeliveryStore(database)
        self.flows = FlowStore(database)
        self.jobs = JobStore(database)
        self.result_store = ResultStore(database)
        self.reviews = ReviewStore(database)
        self.stops = StopStore(database)
        self.webhook_store = WebhookStore(database)
        self.workers = WorkerStore(database)
        self._after_commit = after_commit
        self.plugins = plugins or get_plugin_manager()
        self._jobs_available = jobs_available or (lambda: None)

    def _fire(self, hook: str, **kwargs: object) -> None:
        if self._after_commit is not None:
            self._after_commit.append(partial(emit_hook, self.plugins, hook, **kwargs))
        else:
            emit_hook(self.plugins, hook, **kwargs)

    def on_trigger(self, repo: str, issue_number: int, installation_id: int | None) -> Flow | None:
        with self.database.transaction() as tx:
            flows_tx = self.flows.using(tx)
            jobs_tx = self.jobs.using(tx)
            tx.serialize_issue(repo, issue_number)
            if flows_tx.get_active_flow(repo, issue_number):
                logger.info("flow already active for {}#{}", repo, issue_number)
                return None
            flow = flows_tx.create_flow(repo, issue_number, installation_id)
            jobs_tx.enqueue_job(flow.id, JobKind.INITIAL, payload={"issue_number": issue_number})
        self._jobs_available()
        logger.info("flow {} created (queued) for {}#{}", flow.id, repo, issue_number)
        return flow

    def cancel(self, flow: Flow) -> None:
        """Request abandonment, retaining execution ownership until finalization settles."""
        self.finish(
            flow,
            reason=ArchiveReason.ABANDONED,
            summary="Explicitly abandoned",
            expected_round_no=flow.current_round_no,
        )

    def finish(
        self, flow: Flow, *, reason: ArchiveReason, summary: str, expected_round_no: int
    ) -> None:
        """End the selected round; a conclusion without a PR can succeed independently."""
        with self.database.transaction() as tx:
            deliveries_tx = self.deliveries.using(tx)
            flows_tx = self.flows.using(tx)
            jobs_tx = self.jobs.using(tx)
            stops_tx = self.stops.using(tx)
            fresh = tx.lock_workflow(flow.id)
            if fresh is None or fresh.current_round_no != expected_round_no:
                raise ValueError("Flow round is no longer current")
            if fresh.state is FlowState.ARCHIVED:
                return
            delivery = deliveries_tx.get_delivery(fresh.id)
            merged = delivery is not None and delivery.state is DeliveryState.MERGED
            if reason is ArchiveReason.SUCCEEDED and not merged:
                if not summary.strip():
                    raise ValueError("successful completion requires a conclusion")
                if delivery is not None and delivery.pr_number is not None:
                    raise ValueError("a delivered PR must be merged before successful completion")
            # A repeated end request must not clear an archive failure or downgrade success.
            if fresh.archive_reason is not None:
                return
            fresh.archive_reason = ArchiveReason.SUCCEEDED if merged else reason
            fresh.archive_summary = summary.strip() or None
            fresh.state = FlowState.FINALIZING
            flows_tx.update_flow(fresh)
            stops_tx.request(fresh.id)
            jobs_tx.cancel_jobs(fresh.id)
            if merged:
                jobs_tx.enqueue_job(fresh.id, JobKind.ARCHIVE)
            flows_tx.try_archive(fresh.id)
        self._jobs_available()
        logger.info("flow {} finalization requested", flow.id)

    def resume(self, flow: Flow, *, expected_round_no: int) -> Flow:
        """Explicitly open one new round; stale/repeated requests cannot revive old work."""
        with self.database.transaction() as tx:
            tx.serialize_issue(flow.repo, flow.issue_number)
            fresh = tx.lock_workflow(flow.id)
            flows = self.flows.using(tx)
            if fresh is None or fresh.current_round_no != expected_round_no:
                raise ValueError("Flow round is no longer current")
            active = flows.get_active_flow(fresh.repo, fresh.issue_number)
            if active is not None and active.id != fresh.id:
                raise ValueError(f"flow {active.id} is already active for this Issue")
            if (
                fresh.state is not FlowState.ARCHIVED
                or ActivityStore(tx).archived(fresh.id, expected_round_no) is None
            ):
                raise ValueError("only an archived round can be resumed")
            binding = self.workers.using(tx).get_flow_execution(fresh.id)
            if binding is not None and binding.released_at is None:
                raise ValueError("archived execution is not released")
            ConsoleSyncStore(tx).reset_round(fresh.id)
            ReconcileStore(tx).reset_round(fresh.id)
            resumed = flows.start_round(fresh)
            self.deliveries.using(tx).reset_round(resumed.id, resumed.initial_branch)
            flows.select_owner(resumed)
            self.jobs.using(tx).enqueue_job(
                resumed.id, JobKind.INITIAL, payload={"issue_number": resumed.issue_number}
            )
        self._jobs_available()
        return resumed

    def on_closed(self, flow: Flow, delivery_id: str, payload: dict) -> None:
        """An authenticated unmerged close retires current work in the event transaction."""
        with self.database.transaction() as tx:
            fresh = tx.lock_workflow(flow.id)
            deliveries = self.deliveries.using(tx)
            delivery = deliveries.get_delivery(flow.id)
            remote = payload.get("pull_request") or {}
            if (
                fresh is None
                or fresh.current_round_no != flow.current_round_no
                or delivery is None
                or delivery.pr_number != remote.get("number")
                or fresh.installation_id is None
                or (payload.get("installation") or {}).get("id") != fresh.installation_id
                or (payload.get("repository") or {}).get("full_name") != fresh.repo
                or payload.get("action") != "closed"
                or remote.get("merged") is not False
            ):
                return
            if not self.webhook_store.using(tx).record_event(
                delivery_id, "pull_request", flow.id, payload
            ):
                return
            close_delivery(
                fresh,
                delivery,
                deliveries=deliveries,
                flows=self.flows.using(tx),
                jobs=self.jobs.using(tx),
                stops=self.stops.using(tx),
            )
        self._jobs_available()

    def on_merged(self, flow: Flow, delivery_id: str, payload: dict) -> None:
        """Commit the webhook reservation, merge fact and retirement of old work together."""
        merge = PullRequestMerge.model_validate(payload["pull_request"])
        with self.database.transaction() as tx:
            deliveries_tx = self.deliveries.using(tx)
            flows_tx = self.flows.using(tx)
            jobs_tx = self.jobs.using(tx)
            stops_tx = self.stops.using(tx)
            webhook_store_tx = self.webhook_store.using(tx)
            workers_tx = self.workers.using(tx)
            fresh = tx.lock_workflow(flow.id)
            delivery = deliveries_tx.get_delivery(flow.id)
            if (
                fresh is None
                or payload.get("action") != "closed"
                or (payload.get("repository") or {}).get("full_name") != fresh.repo
                or fresh.installation_id is None
                or (payload.get("installation") or {}).get("id") != fresh.installation_id
            ):
                logger.warning("merge identity does not match flow {}", flow.id)
                return
            historical = ActivityStore(tx).archived_pr(fresh.repo, merge.number)
            if historical is not None and historical[0] == fresh.id:
                if webhook_store_tx.record_event(delivery_id, "pull_request", fresh.id, payload):
                    flows_tx.observe_archived_merge(
                        fresh.id, historical[1], merge, origin="webhook"
                    )
                return
            if delivery is None or delivery.pr_number != merge.number:
                return
            if not webhook_store_tx.record_event(delivery_id, "pull_request", fresh.id, payload):
                return
            first_merge = record_merge(
                fresh,
                delivery,
                merge,
                deliveries=deliveries_tx,
                flows=flows_tx,
                jobs=jobs_tx,
                stops=stops_tx,
                workers=workers_tx,
                origin="webhook",
            )
        if first_merge:
            self.notify_merge(fresh)

    def notify_merge(self, flow: Flow) -> None:
        """Notify plugins and workers only after merge facts and archive work have committed."""
        self._fire("on_merged", flow=flow)
        self._jobs_available()

    def retry(self, flow: Flow) -> bool:
        """Requeue eligible work, returning False when an active Flow needs no retry."""
        with self.database.transaction() as tx:
            deliveries_tx = self.deliveries.using(tx)
            flows_tx = self.flows.using(tx)
            jobs_tx = self.jobs.using(tx)
            result_store_tx = self.result_store.using(tx)
            reviews_tx = self.reviews.using(tx)
            stops_tx = self.stops.using(tx)
            workers_tx = self.workers.using(tx)
            tx.serialize_issue(flow.repo, flow.issue_number)
            fresh = tx.lock_workflow(flow.id)
            if fresh is None:
                raise RetryRejected("flow_not_found", f"flow {flow.id} disappeared")
            if fresh.state is FlowState.ARCHIVED:
                raise RetryRejected("retry_archived", "use explicit resume for an archived Flow")
            if stops_tx.has_pending(fresh.id):
                raise RetryRejected(
                    "retry_stop_pending",
                    "cancellation is awaiting confirmed stop or pending result reconciliation",
                )
            delivery = deliveries_tx.get_delivery(fresh.id)
            if (
                delivery is not None
                and delivery.state is DeliveryState.CLOSED
                and fresh.archive_reason is None
            ):
                raise RetryRejected(
                    "retry_delivery_closed",
                    "PR is closed or paused; reopen and reconcile its baseline before retry",
                )
            if delivery is not None and delivery.state is DeliveryState.MERGED:
                self._retry_archive(
                    fresh,
                    database=tx,
                    deliveries=deliveries_tx,
                    jobs=jobs_tx,
                    result_store=result_store_tx,
                    workers=workers_tx,
                )
            elif jobs_tx.has_pending_result(flow.id):
                if not self._retry_delivery_effects(
                    fresh,
                    database=tx,
                    deliveries=deliveries_tx,
                    jobs=jobs_tx,
                    result_store=result_store_tx,
                    workers=workers_tx,
                ):
                    raise RetryRejected(
                        "retry_result_pending",
                        "Flow has a pending result; reconcile its effects before retry",
                    )
            else:
                active = flows_tx.get_active_flow(fresh.repo, fresh.issue_number)
                if active is not None and active.id != fresh.id:
                    raise RetryRejected(
                        "retry_flow_conflict",
                        f"flow {active.id} is already active for {fresh.repo}#{fresh.issue_number}",
                    )
                if fresh.state in WORKING_STATES or fresh.state is FlowState.FINALIZING:
                    return False
                if any(job.state is JobState.RUNNING for job in jobs_tx.list_jobs(fresh.id)):
                    raise RetryRejected(
                        "retry_execution_unsettled", "original executor is not settled"
                    )
                if fresh.archive_reason is not None:
                    fresh.state = FlowState.FINALIZING
                    flows_tx.update_flow(fresh)
                    flows_tx.try_archive(fresh.id)
                    return True
                # Recovery may have failed the Flow before its queued work was swept.
                jobs_tx.cancel_jobs(fresh.id)
                failed_job = jobs_tx.latest_failed_job(fresh.id)
                kind = failed_job.kind if failed_job is not None else JobKind.INITIAL
                delivery = deliveries_tx.get_delivery(fresh.id)
                if delivery is not None and delivery.supersedes(kind):
                    raise RetryRejected(
                        "retry_delivery_merged", "a merged PR cannot retry coding or review jobs"
                    )
                fresh.state = FlowState.QUEUED
                flows_tx.update_flow(fresh)
                replacement = jobs_tx.enqueue_job(
                    fresh.id,
                    kind,
                    payload=failed_job.payload
                    if failed_job
                    else {"issue_number": fresh.issue_number},
                )
                if failed_job is not None and kind is JobKind.REVISION:
                    reviews_tx.retry(failed_job, replacement)
        self._jobs_available()
        return True

    @staticmethod
    def _retry_delivery_effects(
        flow: Flow,
        *,
        database: Database,
        deliveries: DeliveryStore,
        jobs: JobStore,
        result_store: ResultStore,
        workers: WorkerStore,
    ) -> bool:
        """Validate verified delivery or cleanup before rearming its failed GitHub effects."""
        plan = result_store.get_pending_plan(flow.id)
        if (
            plan is None
            or (plan.revision is None and plan.archive is None)
            or plan.state is not JobState.DONE
            or plan.attempt_id != flow.current_attempt_id
        ):
            return False
        if plan.archive is not None:
            job = database.lock_job(plan.job_id)
            if job is None:
                return False
            try:
                verify_archive(
                    flow, job, plan.archive, deliveries=deliveries, jobs=jobs, workers=workers
                )
            except ValueError:
                return False
        return result_store.retry_failed_effects(plan.job_id)

    @staticmethod
    def _retry_archive(
        flow: Flow,
        *,
        database: Database,
        deliveries: DeliveryStore,
        jobs: JobStore,
        result_store: ResultStore,
        workers: WorkerStore,
    ) -> None:
        """Retry only a settled archive on its original binding and current Attempt."""
        if flow.state not in {FlowState.FINALIZING, FlowState.BLOCKED}:
            raise RetryRejected(
                "retry_archive_inactive",
                "a merged PR cannot retry coding or review jobs or terminal Flows",
            )
        execution = workers.get_flow_execution(flow.id)
        if execution is None or execution.released_at is not None:
            raise RetryRejected(
                "retry_archive_binding_missing",
                "archive retry requires the original active binding",
            )
        if jobs.has_pending_result(flow.id):
            if ControlService._retry_delivery_effects(
                flow,
                database=database,
                deliveries=deliveries,
                jobs=jobs,
                result_store=result_store,
                workers=workers,
            ):
                return
            raise RetryRejected(
                "retry_result_pending",
                "archive has a pending result; wait for result reconciliation",
            )
        job_records = jobs.list_jobs(flow.id)
        if any(job.state is JobState.RUNNING for job in job_records):
            raise RetryRejected(
                "retry_execution_unsettled",
                "archive retry is blocked by a running or unknown executor",
            )
        worker = next(
            (w for w in workers.list_workers() if w.worker_id == execution.worker_id), None
        )
        if (
            worker is None
            or worker.status is not WorkerStatus.ONLINE
            or worker.current_job_id is not None
        ):
            raise RetryRejected(
                "retry_archive_worker_unavailable",
                "archive retry requires the original worker online with no executor",
            )
        if any(job.kind is JobKind.ARCHIVE and job.state is JobState.QUEUED for job in job_records):
            flow.state = FlowState.FINALIZING
            FlowStore(database).update_flow(flow)
            return
        failed = next(
            (
                job
                for job in reversed(job_records)
                if job.kind is JobKind.ARCHIVE and job.state is JobState.FAILED
            ),
            None,
        )
        if failed is None:
            raise RetryRejected("retry_archive_missing", "Flow has no failed archive to retry")
        flow.state = FlowState.FINALIZING
        FlowStore(database).update_flow(flow)
        jobs.enqueue_job(flow.id, JobKind.ARCHIVE, payload=failed.payload)

    def notify_jobs(self) -> None:
        self._jobs_available()

    def list_workers(self) -> list[WorkerView]:
        views: list[WorkerView] = []
        for worker in self.workers.list_workers():
            execution = self.workers.get_worker_execution(worker.worker_id)
            job = (
                self.jobs.get_job(worker.current_job_id)
                if worker.current_job_id is not None
                else None
            )
            views.append(
                WorkerView(
                    worker_id=worker.worker_id,
                    draining=worker.draining,
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
