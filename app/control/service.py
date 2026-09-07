"""Control-plane use cases triggered by GitHub events and the admin CLI."""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from app.models import TERMINAL_STATES, DeliveryState, Flow, FlowState, JobKind, WorkerView
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
            fresh.state = FlowState.CANCELLED
            tx.update_flow(fresh)
            tx.release_flow_execution(fresh.id)
            tx.cancel_jobs(fresh.id)
        self._jobs_available()
        logger.info("flow {} cancelled", flow.id)

    def on_merged(self, flow: Flow) -> None:
        with self.store.transaction() as tx:
            fresh = tx.lock_workflow(flow.id)
            if fresh is None or fresh.state in TERMINAL_STATES:
                return
            delivery = tx.get_delivery(flow.id)
            if delivery is None:
                raise ValueError(f"flow {flow.id} has no delivery to mark merged")
            if delivery.state is DeliveryState.MERGED:
                return
            delivery.state = DeliveryState.MERGED
            tx.update_delivery(delivery)
            tx.enqueue_job(flow.id, JobKind.ARCHIVE)
        self._fire("on_merged", flow=flow)
        self._jobs_available()

    def retry(self, flow: Flow) -> None:
        with self.store.transaction() as tx:
            tx.serialize_issue(flow.repo, flow.issue_number)
            fresh = tx.lock_workflow(flow.id)
            if fresh is None:
                raise ValueError(f"flow {flow.id} disappeared")
            if tx.has_pending_result(flow.id):
                raise ValueError("Flow has a pending result; reconcile its effects before retry")
            active = tx.get_active_flow(fresh.repo, fresh.issue_number)
            if active is not None and active.id != fresh.id:
                raise ValueError(
                    f"flow {active.id} is already active for {fresh.repo}#{fresh.issue_number}"
                )
            if fresh.state is FlowState.ACTIVE:
                return
            if fresh.state is FlowState.SUCCEEDED:
                raise ValueError("a succeeded Flow cannot be retried")
            failed_job = tx.latest_failed_job(fresh.id)
            kind = failed_job.kind if failed_job is not None else JobKind.INITIAL
            fresh.state = FlowState.ACTIVE
            tx.update_flow(fresh)
            replacement = tx.enqueue_job(
                fresh.id,
                kind,
                payload=(
                    failed_job.payload if failed_job else {"issue_number": fresh.issue_number}
                ),
            )
            if failed_job is not None and kind in {JobKind.TRIAGE, JobKind.REVISION, JobKind.ANSWER}:
                tx.reviews.retry(failed_job, replacement)
        self._jobs_available()

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
