"""Discover granted repositories and bootstrap labels with disposable daily scan hints."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import monotonic

from loguru import logger

from app.controler.config import ServerSettings
from app.github import (
    GitHubApp,
    GitHubError,
    GitHubErrorKind,
    Installation,
    retry_delay,
)
from app.github.repositories import LabelDefinition

PENDING_INTERVAL = 300
CONFIRMED_INTERVAL = 86400

FLOW_LABEL_DEFINITIONS = (
    LabelDefinition(name="flow:queued", color="d4c5f9", description="FlowTool task queued"),
    LabelDefinition(name="flow:running", color="1d76db", description="FlowTool task running"),
    LabelDefinition(
        name="flow:review", color="fbca04", description="FlowTool task awaiting review"
    ),
    LabelDefinition(name="flow:blocked", color="b60205", description="FlowTool task blocked"),
    LabelDefinition(name="flow:finalizing", color="0052cc", description="FlowTool task finalizing"),
    LabelDefinition(name="flow:archived", color="0e8a16", description="FlowTool task archived"),
)
FLOW_LABEL_NAMES = frozenset(label.name for label in FLOW_LABEL_DEFINITIONS)


class LabelSynchronizer:
    """Cache successful checks in memory; restart or uncertainty simply causes a fresh read."""

    def __init__(self, github: GitHubApp, settings: ServerSettings) -> None:
        self.github = github
        self._confirmed: dict[tuple[int, str], float] = {}
        self.definitions = (
            LabelDefinition(
                name=settings.trigger_label, color="5319e7", description="Start a FlowTool task"
            ),
            *FLOW_LABEL_DEFINITIONS,
        )

    async def run(self) -> None:
        """Scan in the background without gating readiness; the runtime owns cancellation."""
        while True:
            try:
                delay = await self.sweep()
            except Exception as error:  # noqa: BLE001 -- retry without logging credentials
                logger.warning("repository label sweep unavailable: reason={}", type(error).__name__)
                delay = PENDING_INTERVAL
            await asyncio.sleep(delay)

    async def sweep(self) -> float:
        """Discover current grants each pass, checking labels only when their hint has expired."""
        page = 1
        try:
            while True:
                installations = await self.github.fetch_installations(page=page)
                for installation in installations.items:
                    try:
                        await self._scan_installation(installation)
                    except GitHubError as error:
                        if error.kind is GitHubErrorKind.RATE_LIMITED or error.retry_after:
                            raise
                        logger.warning("repository label inventory failed: installation={} {}",
                                       installation.installation_id, error)
                if installations.next_cursor is None:
                    break
                page = int(installations.next_cursor)
        except GitHubError as error:
            logger.warning("repository label inventory deferred: {}", error)
            return max(PENDING_INTERVAL, retry_delay(1, datetime.now(UTC), error))
        return PENDING_INTERVAL

    async def _scan_installation(self, installation: Installation) -> None:
        page = 1
        while True:
            repositories = await installation.fetch_repositories(page=page)
            for repo in repositories.items:
                identity = (installation.installation_id, repo.full_name.casefold())
                if self._confirmed.get(identity, 0) > monotonic():
                    continue
                self._confirmed.pop(identity, None)
                try:
                    created = await repo.ensure_labels(self.definitions)
                except GitHubError as error:
                    if error.kind is GitHubErrorKind.RATE_LIMITED or error.retry_after:
                        raise
                    logger.warning("repository labels pending: repo={} {}", repo.full_name, error)
                else:
                    self._confirmed[identity] = monotonic() + CONFIRMED_INTERVAL
                    logger.info("repository labels confirmed: repo={} created={}",
                                repo.full_name, created)
            if repositories.next_cursor is None:
                return
            page = int(repositories.next_cursor)
