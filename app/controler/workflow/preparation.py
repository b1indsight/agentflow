"""Shared bounded reads and checks at the unstarted assignment preparation boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

from app.domain.models import Flow, FlowDelivery, Job, JobState
from app.github import GitHubError, GitHubErrorKind, Repository
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore

if TYPE_CHECKING:
    from app.controler.config import ServerSettings

PreparationState = Literal[JobState.QUEUED, JobState.DONE, JobState.FAILED]
type FailJob = Callable[[Job, str], Awaitable[None]]


class PreparationReadFailure(Exception):
    def __init__(self, error: GitHubError) -> None:
        self.error = error
        super().__init__(error.kind.value)


class PreparationStale(Exception):
    """The original unstarted token/epoch no longer authorizes preparation changes."""


def preparation_retired(flow: Flow, job: Job, *, deliveries: DeliveryStore) -> bool:
    """Retire work no longer accepted by the Flow or its current delivery."""
    delivery = deliveries.get_delivery(flow.id)
    return not flow.accepts(job.kind) or flow.retires(job, delivery)


def required_delivery(flow: Flow, *, deliveries: DeliveryStore) -> FlowDelivery:
    """Read the delivery required by review preparation."""
    delivery = deliveries.get_delivery(flow.id)
    if delivery is None:
        raise ValueError(f"flow {flow.id} has no delivery")
    return delivery


def required_pr_number(flow: Flow, *, deliveries: DeliveryStore) -> int:
    """Read the PR identity required by triage and revision preparation."""
    number = required_delivery(flow, deliveries=deliveries).pr_number
    if number is None:
        raise ValueError(f"flow {flow.id} has no pull request")
    return number


class PreparationReads:
    """Read GitHub facts within the persisted preparation retry window."""

    def __init__(self, database: Database, settings: ServerSettings) -> None:
        self._database = database
        self._settings = settings

    async def read[T](self, job: Job, operation: Callable[[], Awaitable[T]]) -> T:
        """Bound only GitHub reads, never failure effects or capability signing."""
        remaining = None if job.prepare_first_failed_at is None else max(0, (
            job.prepare_first_failed_at + timedelta(seconds=self._settings.prepare_retry_window_seconds)
            - self._database.now()
        ).total_seconds())
        try:
            async with asyncio.timeout(remaining):
                return await operation()
        except TimeoutError as error:
            raise PreparationReadFailure(GitHubError(
                GitHubErrorKind.TRANSIENT, "preparation read window expired",
            )) from error

    async def task_content(self, job: Job, flow: Flow, repository: Repository) -> str:
        """Preserve direct input while Issue tasks continue to observe current Issue edits."""
        if flow.task_content is not None:
            return flow.task_content
        number = flow.issue_number
        assert number is not None
        issue = await self.read(job, lambda: repository.fetch_issue(number))
        return f"{issue.title}\n\n{issue.body}" if issue.body else issue.title
