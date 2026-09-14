"""Recover missing discussion pages into the existing versioned settle/Judge pipeline."""

import asyncio

from loguru import logger

from app.github_models import GitHubAdapterError, GitHubErrorKind
from app.github_retry import retry_delay
from app.models import DeliveryState, FlowState, ReviewItemKind
from app.ports import GitHubClient
from app.reconcile_store import FeedbackCheck
from app.review_models import FeedbackUpdate
from app.store import Store


class FeedbackReconciler:
    def __init__(
        self, store: Store, github: GitHubClient, *, settle_seconds: float,
        bot_login: str = "flowtool[bot]", interval: float = 300, batch_size: int = 64,
        concurrency: int = 4,
    ) -> None:
        self.store, self.github = store, github
        self.settle_seconds, self.bot_login = settle_seconds, bot_login.casefold()
        self.interval, self.batch_size, self.concurrency = interval, batch_size, concurrency
        self._sweep_lock = asyncio.Lock()

    async def run(self) -> None:
        # Separate from PR polling: slow PR reads must not delay feedback scheduling.
        while True:
            try:
                await self._sweep()
            except Exception:  # noqa: BLE001 -- failed DB operations remain durably due
                logger.exception("feedback sweep failed")
            await asyncio.sleep(min(5.0, self.interval))

    async def _sweep(self) -> None:
        # Keep task creation bounded, as in Reconciler; each source reads one page per turn.
        async with self._sweep_lock:
            self.store.reconcile.feedback_seed(self.batch_size)
            checks = self.store.reconcile.feedback_due(self.batch_size)
            pending = iter(checks)

            async def drain() -> None:
                for check in pending:
                    try:
                        await self._check(check)
                    except Exception:  # noqa: BLE001 -- diagnostic writes can fail independently
                        logger.exception("feedback check failed for flow {}", check.flow_id)

            async with asyncio.TaskGroup() as group:
                for _ in range(min(self.concurrency, len(checks))):
                    group.create_task(drain())

    async def _check(self, check: FeedbackCheck) -> None:
        try:
            with self.store.transaction() as tx:
                flow = tx.lock_workflow(check.flow_id)
                delivery = tx.get_delivery(check.flow_id)
                if (flow is None or flow.state is not FlowState.ACTIVE or flow.current_attempt_id is None
                        or delivery is None or delivery.state is not DeliveryState.OPEN
                        or delivery.pr_number is None):
                    return
                if flow.installation_id is None:
                    raise ValueError("missing installation")
                boundary = tx.reviews.observation_boundary()
            number = flow.issue_number if check.source == "issue_comments" else delivery.pr_number
            page = await self.github.list_feedback_page(
                flow.installation_id, flow.repo, number, check.source, check.cursor,
            )
            updates, diagnostic = [], None
            for item in page.items:
                if item.is_bot or item.author.casefold() == self.bot_login or item.state == "pending":
                    continue
                inert = item.body.strip().startswith("/flow")
                if inert:
                    diagnostic = "historical command ignored"
                deleted = check.source == "reviews" and item.state in {"approved", "dismissed"}
                updates.append(FeedbackUpdate(
                    kind=ReviewItemKind.REVIEW_BODY if check.source == "reviews" else ReviewItemKind.CONVERSATION,
                    github_id=item.github_id, author=item.author, body="" if deleted else item.body,
                    updated_at=item.updated_at, deleted=deleted, inert=inert,
                ))
            with self.store.transaction() as tx:
                fresh = tx.lock_workflow(flow.id)
                current = tx.get_delivery(flow.id)
                if (fresh is None or current is None or fresh.state is not FlowState.ACTIVE
                        or fresh.current_attempt_id != flow.current_attempt_id
                        or fresh.installation_id != flow.installation_id
                        or current != delivery):
                    tx.reconcile.feedback_failed(check, 5, "Flow changed during observation")
                    return
                tx.reviews.observe_feedback(flow.id, flow.current_attempt_id, updates, boundary, self.settle_seconds)
                tx.reconcile.feedback_finish(
                    check, page.next_cursor, 5 if page.next_cursor else self.interval,
                    diagnostic or ("scan incomplete; continuation saved" if page.next_cursor else None),
                )
        except GitHubAdapterError as error:
            delay = retry_delay(check.failures + 1, self.store.now(), error)
            if error.kind not in {GitHubErrorKind.TRANSIENT, GitHubErrorKind.RATE_LIMITED}:
                delay = max(1800, delay)
            self.store.reconcile.feedback_failed(check, delay, error.kind.value)
        except Exception as error:  # noqa: BLE001 -- persist only the error class, never SDK credentials
            self.store.reconcile.feedback_failed(
                check, retry_delay(check.failures + 1, self.store.now()), type(error).__name__,
            )
