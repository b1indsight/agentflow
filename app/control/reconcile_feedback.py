"""Recover missing discussion pages into the existing versioned settle/Judge pipeline."""

import asyncio

from loguru import logger

from app.domain.github_models import (
    FeedbackPage,
    FeedbackSource,
    GitHubAdapterError,
    GitHubErrorKind,
)
from app.domain.github_retry import retry_delay
from app.domain.models import DeliveryState, FlowState, ReviewItemKind
from app.domain.ports import GitHubClient
from app.domain.review_models import FeedbackUpdate
from app.persistence.reconcile_store import FeedbackCheck
from app.persistence.store import Store


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
            threads, diagnostic = None, None
            updates: list[FeedbackUpdate] = []
            if check.source == "threads":
                thread_page = await self.github.list_review_thread_page(
                    flow.installation_id, flow.repo, number, check.cursor,
                )
                threads = thread_page.snapshot
                threads = threads.model_copy(update={"threads": [thread.model_copy(update={
                    "comments": [comment.model_copy(update={"is_bot": comment.is_bot
                        or comment.author.casefold() == self.bot_login}) for comment in thread.comments],
                }) for thread in threads.threads]})
                following = thread_page.next_cursor
                diagnostic = "; ".join(threads.limitations) or (
                    "partial thread fragments; resolution requires a complete fresh read"
                    if any(not thread.complete for thread in threads.threads) else None
                )
            else:
                page = await self.github.list_feedback_page(
                    flow.installation_id, flow.repo, number, check.source, check.cursor,
                )
                updates, diagnostic = self._discussion_updates(page, check.source)
                following = page.next_cursor
            with self.store.transaction() as tx:
                fresh = tx.lock_workflow(flow.id)
                current = tx.get_delivery(flow.id)
                if (fresh is None or current is None or fresh.state is not FlowState.ACTIVE
                        or fresh.current_attempt_id != flow.current_attempt_id
                        or fresh.installation_id != flow.installation_id
                        or current != delivery):
                    tx.reconcile.feedback_failed(check, 5, "Flow changed during observation")
                    return
                if threads is None:
                    tx.reviews.observe_feedback(flow.id, flow.current_attempt_id, updates, boundary, self.settle_seconds)
                else:
                    tx.reviews.observe_threads(
                        flow.id, threads, [], boundary=boundary, settle_seconds=self.settle_seconds,
                    )
                tx.reconcile.feedback_finish(
                    check, following, 5 if following else self.interval,
                    diagnostic or ("scan incomplete; continuation saved" if following else None),
                    complete=not threads.limitations if threads is not None else True,
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

    def _discussion_updates(
        self, page: FeedbackPage, source: FeedbackSource,
    ) -> tuple[list[FeedbackUpdate], str | None]:
        updates, diagnostic = [], None
        for item in page.items:
            if item.is_bot or item.author.casefold() == self.bot_login or item.state == "pending":
                continue
            inert = item.body.strip().startswith("/flow")
            if inert:
                diagnostic = "historical command ignored"
            deleted = source == "reviews" and item.state in {"approved", "dismissed"}
            updates.append(FeedbackUpdate(
                kind=ReviewItemKind.REVIEW_BODY if source == "reviews" else ReviewItemKind.CONVERSATION,
                github_id=item.github_id, author=item.author, body="" if deleted else item.body,
                updated_at=item.updated_at, deleted=deleted, inert=inert,
            ))
        return updates, diagnostic
