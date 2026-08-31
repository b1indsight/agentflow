"""Control-plane use cases triggered by GitHub events and the admin CLI."""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from app.models import Flow, FlowState, JobKind, WorkerView
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

    def on_trigger(
        self, repo: str, issue_number: int, installation_id: int | None
    ) -> Flow | None:
        if self.store.get_active_flow(repo, issue_number):
            logger.info("flow already active for {}#{}", repo, issue_number)
            return None
        flow = self.store.create_flow(repo, issue_number, installation_id)
        self.store.enqueue_job(flow.id, JobKind.INITIAL, payload={"issue_number": issue_number})
        self._jobs_available()
        logger.info("flow {} created (PENDING) for {}#{}", flow.id, repo, issue_number)
        return flow

    def cancel(self, flow: Flow) -> None:
        flow.state = FlowState.CANCELLED
        self.store.update_flow(flow)
        logger.info("flow {} cancelled", flow.id)

    def on_merged(self, flow: Flow) -> None:
        flow.state = FlowState.MERGED
        self.store.update_flow(flow)
        self._fire("on_merged", flow=flow)
        self.store.enqueue_job(flow.id, JobKind.ARCHIVE)
        self._jobs_available()

    def retry(self, flow: Flow) -> None:
        active = self.store.get_active_flow(flow.repo, flow.issue_number)
        if active is not None and active.id != flow.id:
            raise ValueError(
                f"flow {active.id} is already active for {flow.repo}#{flow.issue_number}"
            )
        failed_job = self.store.latest_failed_job(flow.id)
        kind = failed_job.kind if failed_job is not None else JobKind.INITIAL
        flow.state = {
            JobKind.INITIAL: FlowState.PENDING,
            JobKind.TRIAGE: FlowState.PR_OPEN,
            JobKind.REVISION: FlowState.PR_OPEN,
            JobKind.ANSWER: FlowState.PR_OPEN,
            JobKind.ARCHIVE: FlowState.MERGED,
        }[kind]
        self.store.update_flow(flow)
        self.store.enqueue_job(
            flow.id,
            kind,
            payload=(failed_job.payload if failed_job is not None else {"issue_number": flow.issue_number}),
        )
        self._jobs_available()

    def notify_jobs(self) -> None:
        self._jobs_available()

    def list_workers(self) -> list[WorkerView]:
        views: list[WorkerView] = []
        for worker in self.store.list_workers():
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
                    current_flow_id=job.flow_id if job is not None else None,
                    last_seen_at=worker.last_seen_at,
                    last_error=worker.last_error,
                    connection_epoch=worker.connection_epoch,
                    lease_expires_at=job.lease_expires_at if job is not None else None,
                    recovery_deadline=job.recovery_deadline if job is not None else None,
                    last_disconnect_reason=worker.last_disconnect_reason,
                )
            )
        return views
