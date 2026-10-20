"""Process durable source work, actively querying bounded Worker history pages."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial

from loguru import logger
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from app.contracts.conversation import ConversationFailure, ConversationItem
from app.controler.workers.conversation import ConversationService
from app.github import GitHubApp, GitHubError, retry_delay
from app.github.console import Observation, read_source, sources
from app.persistence.console_sync import ConsoleSyncStore, SyncClaim
from app.persistence.database import Database
from app.persistence.maintenance import MaintenanceStore


async def _drain[T](future: asyncio.Future[T]) -> T:
    """Settle owned work before delivering cancellation, even after repeated requests."""
    cancelled = False
    try:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
        return future.result()
    finally:
        # Shutdown must still propagate if the work itself fails while being drained.
        if cancelled:
            raise asyncio.CancelledError


class ConsoleSynchronizer:
    """Bound source/network concurrency; only pending work is retried after restart.

    There is no periodic full-Flow discovery. Business triggers and webhook receipts
    enqueue identities transactionally; the one-second wakeup only drains that outbox.
    """

    def __init__(
        self, github: GitHubApp, conversations: ConversationService, *, database: Database
    ) -> None:
        self.github = github
        self.conversations = conversations
        self.store = ConsoleSyncStore(database)
        self.maintenance = MaintenanceStore(database)
        self._slots = asyncio.Semaphore(4)

    async def db[T](self, call: Callable[[], T]) -> T:
        """Drain short DB-only threads before releasing capacity or closing the pool."""
        async with self._slots:
            task = asyncio.create_task(asyncio.to_thread(call))
            return await _drain(task)

    async def run(self) -> None:
        while True:
            try:
                with self.maintenance.activity("console") as admitted:
                    if admitted and await self._batch():
                        continue
            except (DatabaseError, PoolTimeout):
                logger.warning(
                    "console synchronization database unavailable; durable work retained"
                )
            await asyncio.sleep(1)

    async def _batch(self) -> bool:
        """Settle every admitted source before releasing the maintenance receipt."""
        claims = []
        for _ in range(4):
            claim = await self.db(self.store.claim)
            if claim is None:
                break
            claims.append(claim)
        if not claims:
            return False
        tasks = [asyncio.create_task(self.sync(claim)) for claim in claims]
        try:
            await asyncio.gather(*tasks)
        finally:
            # gather propagates the first failure before siblings finish.
            # Do not cancel twice while db() is draining a shielded thread.
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            await _drain(asyncio.gather(*tasks, return_exceptions=True))
        return True

    async def sync(self, claim: SyncClaim) -> None:
        """A source failure never discards other sources or already archived contents."""
        try:
            async with asyncio.timeout(30):
                if claim.source == "github":
                    if claim.installation_id is None:
                        await self.db(lambda: self.store.fail(claim, "no_installation", delay=None))
                        return
                    await self.db(
                        lambda: self.store.finish(
                            claim,
                            [],
                            cursor=None,
                            more=False,
                            children=sources(claim.pr_number),
                            limitations=["remote_deleted_or_pre_ingestion_versions_unrecoverable"],
                        )
                    )
                elif claim.source == "worker":
                    page = await self.conversations.read(claim.flow_id, claim.cursor, 50)
                    limitations: list[str] = []
                    items = [self._worker(item, claim) for item in page.items]
                    if any(item.truncated for item in page.items):
                        limitations.append("worker_source_truncated")
                    await self.db(
                        lambda: self.store.finish(
                            claim,
                            items,
                            cursor=page.next_cursor,
                            more=page.has_more,
                            limitations=limitations,
                            unavailable_reason=page.unavailable_reason,
                            sessions=page.sessions,
                        )
                    )
                else:
                    assert claim.installation_id is not None
                    repo = self.github.installation(claim.installation_id).repository(claim.repo)
                    page_remote = await read_source(
                        repo, claim.issue_number, claim.pr_number, claim.source, claim.cursor, 20
                    )
                    await self.db(
                        lambda: self.store.finish(
                            claim,
                            page_remote.items,
                            cursor=page_remote.next_cursor,
                            more=page_remote.next_cursor is not None,
                            limitations=page_remote.limitations,
                            children=page_remote.child_sources,
                        )
                    )
        except ConversationFailure as error:
            reset = error.code == "cursor_invalid"
            await self.db(
                partial(
                    self.store.fail,
                    claim,
                    error.code,
                    delay=retry_delay(claim.failures + 1, datetime.now(UTC)),
                    reset_cursor=reset,
                )
            )
        except GitHubError as error:
            await self.db(
                partial(
                    self.store.fail,
                    claim,
                    error.kind.value,
                    delay=retry_delay(claim.failures + 1, datetime.now(UTC), error)
                    if error.retryable
                    else None,
                )
            )
        except TimeoutError:
            await self.db(lambda: self.store.fail(claim, "sync_timeout", delay=30))
        except ValueError:
            await self.db(
                lambda: self.store.fail(claim, "source_cursor_invalid", delay=30, reset_cursor=True)
            )

    @staticmethod
    def _worker(item: ConversationItem, claim: SyncClaim) -> Observation:
        timestamp = item.timestamp
        timestamp_unknown = timestamp is None
        try:
            occurred = datetime.fromisoformat(timestamp) if timestamp else claim.created_at
            if occurred.tzinfo is None:
                raise ValueError("unknown timezone")
        except ValueError:
            occurred = claim.created_at
            timestamp_unknown = True
        # The source identity/generation, not the current job, owns these historic records.
        metadata: dict[str, str | int | bool | None] = {
            "attempt_id": item.attempt_id,
            "session_id": item.session_id,
            "session_generation": item.session_generation,
            "record_id": item.id,
            "role": item.role,
            "source_truncated": item.truncated,
            "timestamp_unknown": timestamp_unknown,
            "call_id": item.call_id,
        }
        body = item.content.text
        title = item.content.name or item.kind
        if item.kind != "message":
            body = body or item.content.arguments or ""
            original_size = len(body)
            body = body[:2000]
            metadata.update(
                summary=True,
                displayed_characters=original_size,
                summary_truncated=original_size > len(body),
                query="conversation",
                arguments_available=item.content.arguments is not None,
            )
        return Observation(
            key=f"worker:{item.attempt_id}:{item.session_generation}:{item.id}",
            source="worker",
            kind=item.kind,
            occurred_at=occurred,
            updated_at=occurred,
            author=item.role,
            title=title,
            body=body,
            metadata=metadata,
        )
