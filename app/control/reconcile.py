"""Control-plane reconciliation poller for missed GitHub events."""

from __future__ import annotations

import asyncio

from loguru import logger

from app.control.lifecycle import pause_delivery, prove_reopen, resume_delivery
from app.control.service import ControlService
from app.domain.github_models import GitHubAdapterError, GitHubErrorKind, PullRequestMerge
from app.domain.github_retry import retry_delay
from app.domain.models import TERMINAL_STATES, DeliveryState, FlowState
from app.domain.ports import GitHubClient
from app.persistence.reconcile_store import ReconcileState
from app.persistence.store import Store


class Reconciler:
    """Read tracked PRs outside transactions and apply only still-current observations."""

    def __init__(
        self, store: Store, github: GitHubClient, service: ControlService, *,
        interval: float = 300.0, batch_size: int = 64, concurrency: int = 4,
        settle_seconds: float = 75,
    ) -> None:
        self.store = store
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
            self.store.reconcile.seed(self.batch_size)
            checks = self.store.reconcile.due(self.batch_size)
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
            with self.store.transaction() as tx:
                flow = tx.lock_workflow(check.flow_id)
                delivery = tx.get_delivery(check.flow_id)
                if (flow is None or delivery is None or delivery.pr_number is None
                        or flow.state is FlowState.SUCCEEDED or delivery.state is DeliveryState.MERGED):
                    return
                if flow.installation_id is None:
                    raise ValueError("missing installation")
                coding = tx.lifecycle_job(flow.id)
                blocked = tx.reopen_blocked(flow.id)
            remote = await self.github.get_pull_request(
                flow.installation_id, flow.repo, delivery.pr_number,
            )
            if (remote.repo != flow.repo or remote.number != delivery.pr_number
                    or remote.head_repository != flow.repo):
                raise ValueError("PR observation identity mismatch")
            proof, diagnostic = None, None
            if (flow.state is FlowState.ACTIVE and delivery.state is DeliveryState.CLOSED
                    and remote.state == "open" and not remote.merged):
                if blocked:
                    diagnostic = "old executor or result must settle before reopening"
                else:
                    proof, diagnostic = await prove_reopen(self.store, self.github, flow, delivery, remote, coding)
            merged = False
            with self.store.transaction() as tx:
                fresh = tx.lock_workflow(flow.id)
                current = tx.get_delivery(flow.id)
                if (fresh is None or current is None
                        or fresh.current_attempt_id != flow.current_attempt_id
                        or fresh.state != flow.state or fresh.installation_id != flow.installation_id
                        or current.version != delivery.version or current.pr_number != delivery.pr_number):
                    tx.reconcile.defer(flow.id)
                    return
                if remote.merged:
                    if remote.state != "closed":
                        raise ValueError("merged PR is not closed")
                    merge = PullRequestMerge.model_validate({
                        "number": remote.number, "merged": True, "merged_at": remote.merged_at,
                        "merge_commit_sha": remote.merge_commit_sha,
                    })
                    merged = self.service.record_merge(tx, fresh, current, merge)
                elif fresh.state is FlowState.ACTIVE:
                    if remote.state == "closed":
                        pause_delivery(tx, current)
                        diagnostic = "PR closed; binding retained until a verified reopen"
                    elif current.state is DeliveryState.CLOSED:
                        if (proof is not None and not tx.reopen_blocked(flow.id)
                                and tx.lifecycle_job(flow.id) == coding):
                            resume_delivery(tx, fresh, current, proof, self.settle_seconds)
                        elif diagnostic is None:
                            diagnostic = "old executor or result changed during reopen verification"
                    elif (current.head_sha and remote.head_sha != current.head_sha
                          or remote.head_branch != current.branch
                          or current.base_branch and remote.base_branch != current.base_branch):
                        if coding is not None and coding.result_applied_at is None:
                            diagnostic = "in-flight coding delivery awaits its original result verification"
                        else:
                            pause_delivery(tx, current)
                            diagnostic = "PR head/base changed; verified delivery is unchanged"
                delay = max(1800.0, self.interval) if fresh.state in TERMINAL_STATES else self.interval
                tx.reconcile.succeeded(flow.id, delay, diagnostic, hint_version=check.hint_version)
            if merged:
                self.service.notify_merge(fresh)
        except GitHubAdapterError as error:
            delay = retry_delay(check.failures + 1, self.store.now(), error)
            if error.kind not in {GitHubErrorKind.TRANSIENT, GitHubErrorKind.RATE_LIMITED}:
                delay = max(1800.0, delay)
            self.store.reconcile.failed(check.flow_id, delay, error.kind.value)
        except Exception as error:  # noqa: BLE001 -- do not persist raw SDK/SQL/credential messages
            self.store.reconcile.failed(
                check.flow_id, retry_delay(check.failures + 1, self.store.now()),
                f"observation failed: {type(error).__name__}",
            )

    def stop(self) -> None:
        self._stop.set()
