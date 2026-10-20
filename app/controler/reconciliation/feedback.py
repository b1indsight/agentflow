"""Recover missing discussion pages into the existing versioned settle/Judge pipeline."""

from __future__ import annotations

import asyncio

from loguru import logger
from pydantic import BaseModel, ConfigDict, model_validator

from app.domain.models import DeliveryState, FlowState, ReviewItemKind
from app.domain.review_models import FeedbackUpdate
from app.github import (
    Batch,
    FeedbackSource,
    GitHubApp,
    GitHubError,
    GitHubErrorKind,
    Issue,
    PullRequest,
    RemoteFeedback,
    ReviewComment,
    ReviewThread,
    ReviewThreads,
    retry_delay,
)
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.maintenance import MaintenanceStore
from app.persistence.reconciliation import FeedbackCheck, ReconcileStore
from app.persistence.reviews import ReviewStore


class ThreadScanState(BaseModel):
    """Scanner-owned continuation: next thread position and an unfinished thread.

    Only metadata and the comment cursor are saved, never the already observed bodies.
    Query methods receive native cursors and do not understand this serialized state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    cursor: str | None = None
    thread: ReviewThread | None = None

    @model_validator(mode="after")
    def unfinished_thread(self) -> ThreadScanState:
        if self.thread is not None:
            required = {"path", "resolved", "outdated", "comments", "complete"}
            if (not required.issubset(self.thread.model_fields_set)
                    or self.thread.comments.next_cursor is None or self.thread.comments.items
                    or self.thread.complete):
                raise ValueError("invalid unfinished thread state")
        return self

    def continuation(self) -> str | None:
        return self.model_dump_json(exclude_unset=True) if self.cursor or self.thread is not None else None


class FeedbackReconciler:
    def __init__(
        self,
        github: GitHubApp,
        *,
        settle_seconds: float,
        bot_login: str = "flowtool[bot]",
        interval: float = 300,
        batch_size: int = 64,
        concurrency: int = 4,
        database: Database,
    ) -> None:
        self.database = database
        self.maintenance = MaintenanceStore(database)
        self.deliveries = DeliveryStore(database)
        self.reconcile_store = ReconcileStore(database)
        self.reviews = ReviewStore(database)
        self.github = github
        self.settle_seconds, self.bot_login = settle_seconds, bot_login.casefold()
        self.interval, self.batch_size, self.concurrency = interval, batch_size, concurrency
        self._sweep_lock = asyncio.Lock()

    async def run(self) -> None:
        # Separate from PR polling: slow PR reads must not delay feedback scheduling.
        while True:
            try:
                with self.maintenance.activity("feedback") as admitted:
                    if admitted:
                        await self._sweep()
            except Exception:  # noqa: BLE001 -- failed DB operations remain durably due
                logger.exception("feedback sweep failed")
            await asyncio.sleep(min(5.0, self.interval))

    async def _sweep(self) -> None:
        # Keep task creation bounded, as in Reconciler; each source reads one page per turn.
        async with self._sweep_lock:
            self.reconcile_store.feedback_seed(self.batch_size)
            checks = self.reconcile_store.feedback_due(self.batch_size)
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
            with self.database.transaction() as tx:
                deliveries_tx = self.deliveries.using(tx)
                reviews_tx = self.reviews.using(tx)
                flow = tx.lock_workflow(check.flow_id)
                delivery = deliveries_tx.get_delivery(check.flow_id)
                if (flow is None or flow.state is not FlowState.ACTIVE or flow.current_attempt_id is None
                        or delivery is None or delivery.state is not DeliveryState.OPEN
                        or delivery.pr_number is None):
                    return
                if flow.installation_id is None:
                    raise ValueError("missing installation")
                boundary = reviews_tx.observation_boundary()
            number = flow.issue_number if check.source == "issue_comments" else delivery.pr_number
            repo = self.github.installation(flow.installation_id).repository(flow.repo)
            threads, diagnostic = None, None
            updates: list[FeedbackUpdate] = []
            if check.source == "threads":
                threads, following = await self._scan_threads(PullRequest(repo, number), check.cursor)
                threads = threads.model_copy(update={"threads": tuple(thread.model_copy(update={
                    "comments": thread.comments.model_copy(update={"items": tuple(
                        comment.model_copy(update={"is_bot": comment.is_bot
                        or comment.author.casefold() == self.bot_login}) for comment in thread.comments.items),
                    }),
                }) for thread in threads.threads)})
                diagnostic = "; ".join(threads.limitations) or (
                    "partial thread fragments; resolution requires a complete fresh read"
                    if any(not thread.complete for thread in threads.threads) else None
                )
            else:
                if check.source == "issue_comments":
                    page = await Issue(repo, number).fetch_comments(cursor=check.cursor, limit=50)
                elif check.source == "reviews":
                    page = await PullRequest(repo, number).fetch_reviews(cursor=check.cursor, limit=50)
                else:
                    page = await PullRequest(repo, number).fetch_comments(cursor=check.cursor, limit=50)
                updates, diagnostic = self._discussion_updates(page, check.source)
                following = page.next_cursor
            with self.database.transaction() as tx:
                deliveries_tx = self.deliveries.using(tx)
                reconcile_store_tx = self.reconcile_store.using(tx)
                reviews_tx = self.reviews.using(tx)
                fresh = tx.lock_workflow(flow.id)
                current = deliveries_tx.get_delivery(flow.id)
                if (fresh is None or current is None or fresh.state is not FlowState.ACTIVE
                        or fresh.current_attempt_id != flow.current_attempt_id
                        or fresh.installation_id != flow.installation_id
                        or current != delivery):
                    reconcile_store_tx.feedback_failed(check, 5, "Flow changed during observation")
                    return
                if threads is None:
                    reviews_tx.observe_feedback(flow.id, flow.current_attempt_id, updates, boundary, self.settle_seconds)
                else:
                    reviews_tx.observe_threads(
                        flow.id, threads, [], boundary=boundary, settle_seconds=self.settle_seconds,
                    )
                reconcile_store_tx.feedback_finish(
                    check, following, 5 if following else self.interval,
                    diagnostic or ("scan incomplete; continuation saved" if following else None),
                    complete=not threads.limitations if threads is not None else True,
                )
        except GitHubError as error:
            delay = retry_delay(check.failures + 1, self.database.now(), error)
            if error.kind not in {GitHubErrorKind.TRANSIENT, GitHubErrorKind.RATE_LIMITED}:
                delay = max(1800, delay)
            self.reconcile_store.feedback_failed(check, delay, error.kind.value)
        except Exception as error:  # noqa: BLE001 -- persist only the error class, never SDK credentials
            self.reconcile_store.feedback_failed(
                check, retry_delay(check.failures + 1, self.database.now()), type(error).__name__,
            )

    async def _scan_threads(
        self, pr: PullRequest, cursor: str | None,
    ) -> tuple[ReviewThreads, str | None]:
        """Observe one request and advance durable traversal separately from queries."""
        state = ThreadScanState.model_validate_json(cursor) if cursor else ThreadScanState()
        snapshot = ReviewThreads(
            repo=pr.repository.full_name, pr_number=pr.number, fetched_at=self.database.now(),
        )
        if state.thread is None:
            batch = await pr.fetch_threads(cursor=state.cursor, limit=1)
            following = batch.next_cursor
            threads = batch.items
        else:
            thread = ReviewThread(pr, **state.thread.model_dump(exclude_unset=True))
            following = state.cursor
            try:
                comments = await thread.fetch_comments(cursor=thread.comments.next_cursor, limit=20)
            except GitHubError as error:
                if error.kind is not GitHubErrorKind.NOT_FOUND:
                    raise
                # A removed thread cannot indefinitely block observation of later threads.
                snapshot = snapshot.model_copy(update={"limitations": ("thread:missing-node",)})
                threads = ()
            else:
                # A suffix, including its final batch, never proves coverage of the whole thread.
                threads = (thread.model_copy(update={"comments": comments, "complete": False}),)
        unfinished = next((thread for thread in threads if thread.comments.next_cursor), None)
        if unfinished is not None:
            unfinished = unfinished.model_copy(update={
                "comments": Batch[ReviewComment](next_cursor=unfinished.comments.next_cursor),
                "complete": False,
            })
        continuation = ThreadScanState(cursor=following, thread=unfinished).continuation()
        return snapshot.model_copy(update={"threads": threads}), continuation

    def _discussion_updates(
        self, page: Batch[RemoteFeedback], source: FeedbackSource,
    ) -> tuple[list[FeedbackUpdate], str | None]:
        updates, diagnostic = [], None
        for item in page.items:
            if (item.author is None or item.is_bot or item.author.casefold() == self.bot_login
                    or item.state == "pending"):
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
