"""Control-plane reconciliation poller for missed GitHub events."""

from __future__ import annotations

import asyncio

from loguru import logger

from app.controler.workflow.lifecycle import (
    close_delivery,
    pause_delivery,
    prove_reopen,
    record_merge,
    record_reopen,
)
from app.controler.workflow.service import ControlService
from app.domain.flow_policy import FlowEvent
from app.domain.models import TERMINAL_STATES, DeliveryState, FlowState
from app.github import GitHubApp, GitHubError, GitHubErrorKind, PullRequestMerge, retry_delay
from app.persistence.assignments import AssignmentStore
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.reconciliation import ReconcileState, ReconcileStore
from app.persistence.stops import StopStore


class Reconciler:
    """Read tracked PRs outside transactions and apply only still-current observations."""

    def __init__(
        self,
        github: GitHubApp,
        service: ControlService,
        *,
        interval: float = 300.0,
        batch_size: int = 64,
        concurrency: int = 4,
        database: Database,
    ) -> None:
        self.database = database
        self.deliveries = DeliveryStore(database)
        self.flows = FlowStore(database)
        self.jobs = JobStore(database)
        self.reconcile_store = ReconcileStore(database)
        self.stops = StopStore(database)
        self.assignments = AssignmentStore(database)
        self.github = github
        self.service = service
        self.interval, self.batch_size, self.concurrency = interval, batch_size, concurrency
        self._stop = asyncio.Event()
        self._sweep_lock = asyncio.Lock()

    async def run(self) -> None:
        logger.info("reconciler started")
        while not self._stop.is_set():
            try:
                await self._sweep()
            except Exception:  # noqa: BLE001 -- database failures leave due checks for the next sweep
                logger.exception("reconcile sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=min(5.0, self.interval))
            except TimeoutError:
                pass

    async def _sweep(self) -> None:
        async with self._sweep_lock:
            for flow_id in self.reconcile_store.merged_backlog(self.batch_size):
                try:
                    with self.flows.locked(flow_id) as context:
                        tx, flow = context.database, context.flow
                        delivery = self.deliveries.using(tx).get_delivery(flow_id)
                        if (flow is None or flow.state is FlowState.ARCHIVED
                                or delivery is None or delivery.state is not DeliveryState.MERGED):
                            continue
                        merge = PullRequestMerge.model_validate({
                            "number": delivery.pr_number, "merged": True,
                            "merged_at": delivery.merged_at,
                            "merge_commit_sha": delivery.merge_commit_sha,
                        })
                        archived = record_merge(
                            flow, delivery, merge, deliveries=self.deliveries.using(tx),
                            flows=self.flows.using(tx), jobs=self.jobs.using(tx),
                            stops=self.stops.using(tx), assignments=self.assignments.using(tx),
                            origin="merged_backlog",
                        )
                    if archived:
                        self.service.notify_merge(flow)
                except Exception:  # noqa: BLE001 -- retain corrupt historical facts without starving the sweep
                    logger.exception("merged backlog recovery failed for flow {}", flow_id)
            self.reconcile_store.seed(self.batch_size)
            checks = self.reconcile_store.due(self.batch_size)
            pending = iter(checks)

            async def drain() -> None:
                # Like result recovery, bound task creation as well as the selected page.
                for check in pending:
                    try:
                        await self._check(check)
                    except Exception:  # noqa: BLE001 -- even diagnostic writes may fail independently
                        logger.exception("reconcile failed for flow {}", check.flow_id)

            async with asyncio.TaskGroup() as group:
                for _ in range(min(self.concurrency, len(checks))):
                    group.create_task(drain())

    async def _check(self, check: ReconcileState) -> None:
        try:
            with self.flows.locked(check.flow_id) as context:
                tx = context.database
                deliveries_tx = self.deliveries.using(tx)
                jobs_tx = self.jobs.using(tx)
                flow = context.flow
                delivery = deliveries_tx.get_delivery(check.flow_id)
                if (
                    flow is None
                    or delivery is None
                    or delivery.attempt_id != check.attempt_id
                    or delivery.pr_number is None
                    or delivery.state is DeliveryState.MERGED
                ):
                    return
                if flow.installation_id is None:
                    raise ValueError("missing installation")
                coding = jobs_tx.lifecycle_job(flow.id)
                blocked = jobs_tx.reopen_blocked(flow.id)
            repo = self.github.installation(flow.installation_id).repository(flow.repo)
            remote = await repo.fetch_pull_request(delivery.pr_number)
            if (remote.repo != flow.repo or remote.number != delivery.pr_number
                    or remote.head_repository != flow.repo):
                raise ValueError("PR observation identity mismatch")
            proof, diagnostic = None, None
            if (
                flow.archive_reason is None
                and delivery.state is DeliveryState.CLOSED
                and remote.state == "open"
                and not remote.merged
            ):
                if blocked:
                    diagnostic = "old executor or result must settle before reopening"
                else:
                    proof, diagnostic = await prove_reopen(
                        repo,
                        flow,
                        delivery,
                        remote,
                        coding,
                        deliveries=self.deliveries,
                        flows=self.flows,
                    )
            merged = False
            with self.flows.locked(flow.id) as context:
                tx = context.database
                deliveries_tx = self.deliveries.using(tx)
                flows_tx = self.flows.using(tx)
                jobs_tx = self.jobs.using(tx)
                reconcile_store_tx = self.reconcile_store.using(tx)
                stops_tx = self.stops.using(tx)
                fresh = context.flow
                if fresh is None:
                    return
                historical = fresh.current_attempt_id != check.attempt_id
                if historical and not remote.merged:
                    return
                current = deliveries_tx.get_attempt_delivery(check.attempt_id)
                if (current is None
                    or fresh.installation_id != flow.installation_id
                    or current.pr_number != delivery.pr_number
                    or (not historical and (fresh.state != flow.state or current.version != delivery.version))
                ):
                    if not historical:
                        reconcile_store_tx.defer(flow.id)
                    return
                if remote.merged:
                    if remote.state != "closed":
                        raise ValueError("merged PR is not closed")
                    merge = PullRequestMerge.model_validate({
                        "number": remote.number, "merged": True, "merged_at": remote.merged_at,
                        "merge_commit_sha": remote.merge_commit_sha,
                    })
                    merged = record_merge(
                        fresh,
                        current,
                        merge,
                        deliveries=deliveries_tx,
                        flows=flows_tx,
                        jobs=jobs_tx,
                        stops=stops_tx,
                        assignments=self.assignments.using(tx),
                        origin="reconciliation",
                    )
                    if historical:
                        return  # Merge evidence belongs to A; B owns the active scheduling row.
                elif fresh.state is not FlowState.ARCHIVED:
                    if remote.state == "closed":
                        close_delivery(
                            fresh,
                            current,
                            deliveries=deliveries_tx,
                            flows=flows_tx,
                            stops=stops_tx,
                            assignments=self.assignments.using(tx),
                        )
                        diagnostic = "PR closed; attempt finalization requested"
                    elif fresh.archive_reason is not None:
                        diagnostic = "PR reopened; explicit resume after archive is required"
                    elif current.state is DeliveryState.CLOSED:
                        if (proof is not None and not jobs_tx.reopen_blocked(flow.id)
                                and jobs_tx.lifecycle_job(flow.id) == coding):
                            record_reopen(
                                fresh,
                                current,
                                proof,
                                deliveries=deliveries_tx,
                                flows=flows_tx,
                            )
                        elif diagnostic is None:
                            diagnostic = "old executor or result changed during reopen verification"
                    elif remote.head_branch != current.branch:
                        if coding is not None and coding.result_applied_at is None:
                            diagnostic = "in-flight coding delivery awaits its original result verification"
                        else:
                            pause_delivery(
                                current, deliveries=deliveries_tx, assignments=self.assignments.using(tx))
                            flows_tx.transition(fresh, FlowEvent.DELIVERY_BASELINE_REJECTED)
                            diagnostic = "PR source branch changed; verified delivery is unchanged"
                delay = max(1800.0, self.interval) if fresh.state in TERMINAL_STATES else self.interval
                reconcile_store_tx.succeeded(flow.id, delay, diagnostic, hint_version=check.hint_version)
            if merged:
                self.service.notify_merge(fresh)
        except GitHubError as error:
            delay = retry_delay(check.failures + 1, self.database.now(), error)
            if error.kind not in {GitHubErrorKind.TRANSIENT, GitHubErrorKind.RATE_LIMITED}:
                delay = max(1800.0, delay)
            diagnostic = error.kind.value
        except Exception as error:  # noqa: BLE001 -- do not persist raw SDK/SQL/credential messages
            delay = retry_delay(check.failures + 1, self.database.now())
            diagnostic = f"observation failed: {type(error).__name__}"
        else:
            return
        with self.flows.locked(check.flow_id) as context:
            if context.flow is not None and context.flow.current_attempt_id == check.attempt_id:
                self.reconcile_store.using(context.database).failed(check.flow_id, delay, diagnostic)

    def stop(self) -> None:
        self._stop.set()
