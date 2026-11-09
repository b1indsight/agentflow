"""Independent Issue/PR label convergence from Flow.state, never the reverse.

The runtime shares its repository LabelSynchronizer with this cancellable background
task after schema initialization. ``sweep()`` runs one bounded pass; no workflow
callbacks or result effects are required.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial

from loguru import logger

from app.controler.config import ServerSettings
from app.controler.reconciliation.labels import FLOW_LABEL_NAMES, LabelSynchronizer
from app.github import GitHubApp, GitHubError, GitHubErrorKind, Issue, PullRequest
from app.persistence.database import Database
from app.persistence.flow_labels import FlowLabelStore, LabelDesired, LabelTarget


class DesiredChanged(Exception):
    """An in-flight observation no longer owns the target's desired state."""


class FlowLabelSync:
    """Read current authority before/after I/O and persist only finite retry timing."""

    def __init__(
        self,
        github: GitHubApp,
        settings: ServerSettings,
        *,
        database: Database,
        repository_labels: LabelSynchronizer,
        interval: float = 300,
        batch_size: int = 64,
        concurrency: int = 4,
    ) -> None:
        if settings.trigger_label.casefold() in FLOW_LABEL_NAMES:
            raise ValueError("trigger label must be distinct from managed Flow state labels")
        if interval <= 0 or batch_size <= 0 or concurrency <= 0:
            raise ValueError("label sync interval, batch size and concurrency must be positive")
        self.github, self.database = github, database
        self.repository_labels = repository_labels
        self.labels = FlowLabelStore(database)
        self.interval, self.batch_size, self.concurrency = interval, batch_size, concurrency
        # Retire both the legacy defaults and configured names, but never the trigger.
        self.managed = (
            FLOW_LABEL_NAMES
            | {
                "flow:in-review",
                "flow:done",
                settings.in_review_label.casefold(),
                settings.done_label.casefold(),
            }
        ) - {settings.trigger_label.casefold()}

    async def run(self) -> None:
        """Retry operational failures independently; cancellation belongs to the runtime."""
        while True:
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 -- no label failure may stop Flow processing
                logger.exception("Flow label sweep failed")
            await asyncio.sleep(5)

    async def sweep(self) -> None:
        """Discover and reconcile a bounded batch, including after process restart."""
        self.labels.seed(self.batch_size)
        due = iter(self.labels.due(self.batch_size))

        async def consume() -> None:
            for row in due:
                try:
                    await self.sync(row)
                except Exception:  # noqa: BLE001 -- isolate unexpected failures to one target
                    logger.exception("Flow labels pending: repo={} number={}", row.repo, row.number)

        async with asyncio.TaskGroup() as group:
            for _ in range(self.concurrency):
                group.create_task(consume())

    async def _call[T](
        self,
        row: LabelTarget,
        desired: LabelDesired,
        request: Callable[[], Awaitable[T]],
    ) -> T:
        if self.labels.desired(row) != desired:
            raise DesiredChanged
        try:
            result = await request()
        finally:
            # Read even after an uncertain write; its error/backoff must not be masked.
            fresh = self.labels.desired(row)
        if fresh != desired:
            raise DesiredChanged
        return result

    async def sync(self, row: LabelTarget) -> None:
        """Converge one target under a cross-controller claim; stale supplied rows are harmless."""
        async with self.labels.claim(row) as acquired:
            if not acquired:
                return
            row = self.labels.get(row.id)
            if row.next_at > self.database.now():
                return
            desired = self.labels.desired(row)
            if desired is None:
                self.labels.checked(row, None, self.interval)
                return
            try:
                if desired.installation_id is None:
                    raise GitHubError(GitHubErrorKind.AUTHENTICATION, "flow_label_installation")
                repository = self.github.installation(desired.installation_id).repository(row.repo)
                target = (
                    Issue(repository, row.number)
                    if desired.kind == "issue"
                    else PullRequest(repository, row.number)
                )
                await self._call(
                    row, desired, lambda: self.repository_labels.ensure(repository)
                )
                observed = await self._call(row, desired, target.fetch_labels)
                wanted = f"flow:{desired.state.value}"
                if wanted not in {name.casefold() for name in observed}:
                    await self._call(row, desired, lambda: target.add_label(wanted))
                # Incremental mutations preserve trigger/user labels, including concurrent additions.
                for name in observed:
                    if name.casefold() in self.managed and name.casefold() != wanted:
                        await self._call(row, desired, partial(target.remove_label, name))
                observed = await self._call(row, desired, target.fetch_labels)
                if {name.casefold() for name in observed} & self.managed != {wanted}:
                    raise GitHubError(
                        GitHubErrorKind.UNEXPECTED_RESPONSE, "flow_label_verification"
                    )
                self.labels.checked(row, desired, self.interval)
            except DesiredChanged:
                self.labels.changed(row)
            except GitHubError as error:
                self.labels.failed(row, error)
                logger.warning(
                    "Flow labels deferred: repo={} number={} {}", row.repo, row.number, error
                )
