"""Control-plane use cases triggered by GitHub events and the admin CLI."""

from __future__ import annotations

from loguru import logger

from app.models import Flow, FlowState, JobKind
from app.plugins import get_plugin_manager
from app.store import Store


class ControlService:
    def __init__(self, store: Store, plugins=None) -> None:
        self.store = store
        self.plugins = plugins or get_plugin_manager()

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
