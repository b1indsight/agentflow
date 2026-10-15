"""Control-plane reconciliation poller for missed GitHub events."""

from __future__ import annotations

import asyncio

from loguru import logger

from app.controler.workflow.lifecycle import (
    pause_delivery,
    prove_reopen,
    record_merge,
    resume_delivery,
)
from app.controler.workflow.service import ControlService
from app.domain.models import TERMINAL_STATES, DeliveryState, FlowState
from app.github import GitHubApp, GitHubError, GitHubErrorKind, PullRequestMerge, retry_delay
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.reconciliation import ReconcileState, ReconcileStore
from app.persistence.reviews import ReviewStore
from app.persistence.stops import StopStore
from app.persistence.workers import WorkerStore


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
        settle_seconds: float = 75,
        database: Database,
    ) -> None:
        self.database = database
        self.deliveries = DeliveryStore(database)
        self.flows = FlowStore(database)
        self.jobs = JobStore(database)
        self.reconcile_store = ReconcileStore(database)
        self.reviews = ReviewStore(database)
        self.stops = StopStore(database)
        self.workers = WorkerStore(database)
        self.github = github
        self.service = service
        self.interval, self.batch_size, self.concurrency = interval, batch_size, concurrency
        self.settle_seconds = settle_seconds
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
            with self.database.transaction() as tx:
                deliveries_tx = self.deliveries.using(tx)
                jobs_tx = self.jobs.using(tx)
                flow = tx.lock_workflow(check.flow_id)
                delivery = deliveries_tx.get_delivery(check.flow_id)
                if (flow is None or delivery is None or delivery.pr_number is None
                        or flow.state is FlowState.SUCCEEDED or delivery.state is DeliveryState.MERGED):
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
            if (flow.state is FlowState.ACTIVE and delivery.state is DeliveryState.CLOSED
                    and remote.state == "open" and not remote.merged):
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
            with self.database.transaction() as tx:
                deliveries_tx = self.deliveries.using(tx)
                flows_tx = self.flows.using(tx)
                jobs_tx = self.jobs.using(tx)
                reconcile_store_tx = self.reconcile_store.using(tx)
                reviews_tx = self.reviews.using(tx)
                stops_tx = self.stops.using(tx)
                workers_tx = self.workers.using(tx)
                fresh = tx.lock_workflow(flow.id)
                current = deliveries_tx.get_delivery(flow.id)
                if (fresh is None or current is None
                        or fresh.current_attempt_id != flow.current_attempt_id
                        or fresh.state != flow.state or fresh.installation_id != flow.installation_id
                        or current.version != delivery.version or current.pr_number != delivery.pr_number):
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
                        workers=workers_tx,
                    )
                elif fresh.state is FlowState.ACTIVE:
                    if remote.state == "closed":
                        pause_delivery(current, deliveries=deliveries_tx, jobs=jobs_tx)
                        diagnostic = "PR closed; binding retained until a verified reopen"
                    elif current.state is DeliveryState.CLOSED:
                        if (proof is not None and not jobs_tx.reopen_blocked(flow.id)
                                and jobs_tx.lifecycle_job(flow.id) == coding):
                            resume_delivery(
                                fresh,
                                current,
                                proof,
                                self.settle_seconds,
                                deliveries=deliveries_tx,
                                flows=flows_tx,
                                jobs=jobs_tx,
                                reviews=reviews_tx,
                            )
                        elif diagnostic is None:
                            diagnostic = "old executor or result changed during reopen verification"
                    elif (current.head_sha and remote.head_sha != current.head_sha
                          or remote.head_branch != current.branch
                          or current.base_branch and remote.base_branch != current.base_branch):
                        if coding is not None and coding.result_applied_at is None:
                            diagnostic = "in-flight coding delivery awaits its original result verification"
                        else:
                            pause_delivery(current, deliveries=deliveries_tx, jobs=jobs_tx)
                            diagnostic = "PR head/base changed; verified delivery is unchanged"
                delay = max(1800.0, self.interval) if fresh.state in TERMINAL_STATES else self.interval
                reconcile_store_tx.succeeded(flow.id, delay, diagnostic, hint_version=check.hint_version)
            if merged:
                self.service.notify_merge(fresh)
        except GitHubError as error:
            delay = retry_delay(check.failures + 1, self.database.now(), error)
            if error.kind not in {GitHubErrorKind.TRANSIENT, GitHubErrorKind.RATE_LIMITED}:
                delay = max(1800.0, delay)
            self.reconcile_store.failed(check.flow_id, delay, error.kind.value)
        except Exception as error:  # noqa: BLE001 -- do not persist raw SDK/SQL/credential messages
            self.reconcile_store.failed(
                check.flow_id, retry_delay(check.failures + 1, self.database.now()),
                f"observation failed: {type(error).__name__}",
            )

    def stop(self) -> None:
        self._stop.set()
