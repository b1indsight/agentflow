"""Discover granted repositories and bootstrap labels on durable 5-minute/daily deadlines."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from loguru import logger

from app.controler.config import ServerSettings
from app.github import (
    GitHubApp,
    GitHubError,
    GitHubErrorKind,
    Installation,
    Repository,
    retry_delay,
)
from app.github.repositories import LabelDefinition
from app.persistence.database import Database
from app.persistence.labels import LabelStore
from app.persistence.maintenance import MaintenanceStore

PENDING_INTERVAL = 300
CONFIRMED_INTERVAL = 86400


class LabelSynchronizer:
    """Only current inventory authorizes writes; stored checkpoints only control their timing."""

    def __init__(
        self, github: GitHubApp, settings: ServerSettings, *, database: Database,
    ) -> None:
        self.github = github
        self.database = database
        self.store = LabelStore(database)
        self.maintenance = MaintenanceStore(database)
        self.definitions = (
            LabelDefinition(name=settings.trigger_label, color="5319e7",
                            description="Start a FlowTool task"),
            LabelDefinition(name=settings.in_review_label, color="fbca04",
                            description="FlowTool delivery awaiting review"),
            LabelDefinition(name=settings.done_label, color="0e8a16",
                            description="FlowTool task merged and archived"),
        )
        self.names = tuple(item.name.casefold() for item in self.definitions)

    async def run(self) -> None:
        """Start immediately without gating readiness; cancellation releases the current activity."""
        while True:
            try:
                delay = await self.sweep()
            except Exception as error:  # noqa: BLE001 -- retry DB outages without logging credentials
                logger.warning("repository label sweep unavailable: reason={}", type(error).__name__)
                delay = PENDING_INTERVAL
            await asyncio.sleep(delay)

    async def sweep(self) -> float:
        """Process currently granted, due repositories and return the next wake-up delay."""
        now = self.database.now()
        deferred = self.store.deferred_until(self.github.app_id)
        if deferred and deferred > now:
            return (deferred - now).total_seconds()
        next_run = now + timedelta(seconds=PENDING_INTERVAL)
        page = 1
        try:
            while True:
                with self.maintenance.activity("repository-labels") as admitted:
                    if not admitted:
                        return PENDING_INTERVAL
                    installations = await self.github.fetch_installations(page=page)
                for installation in installations.items:
                    try:
                        next_run = min(next_run, await self._scan_installation(installation))
                    except GitHubError as error:
                        if error.kind is GitHubErrorKind.RATE_LIMITED:
                            raise
                        logger.warning("repository label inventory failed: installation={} {}",
                                       installation.installation_id, error)
                if installations.next_cursor is None:
                    break
                page = int(installations.next_cursor)
        except GitHubError as error:
            logger.warning("repository label inventory deferred: {}", error)
            now = self.database.now()
            delay = max(PENDING_INTERVAL, retry_delay(1, now, error))
            self.store.defer(self.github.app_id, now + timedelta(seconds=delay))
            return delay
        # Wake at an earlier pending deadline instead of rounding it to another 5-minute tick.
        return max(1.0, (next_run - self.database.now()).total_seconds())

    async def _scan_installation(self, installation: Installation) -> datetime:
        next_run = self.database.now() + timedelta(seconds=PENDING_INTERVAL)
        page = 1
        while True:
            with self.maintenance.activity("repository-labels") as admitted:
                if not admitted:
                    return next_run
                repositories = await installation.fetch_repositories(page=page)
            for repo in repositories.items:
                with self.maintenance.activity("repository-labels") as admitted:
                    if not admitted:
                        return next_run
                    try:
                        next_run = min(next_run, await self._check(repo))
                    except GitHubError:
                        raise  # rate limiting stops this sweep after its deadline was persisted
                    except Exception as error:  # noqa: BLE001 -- isolate repository/DB failures
                        logger.warning("repository label check unavailable: repo={} reason={}",
                                       repo.full_name, type(error).__name__)
            if repositories.next_cursor is None:
                return next_run
            page = int(repositories.next_cursor)

    async def _check(self, repo: Repository) -> datetime:
        identity = (self.github.app_id, repo.installation.installation_id, repo.full_name)
        saved = self.store.get(*identity)
        if saved and saved.names == self.names and saved.next_at > self.database.now():
            return saved.next_at
        try:
            created = await repo.ensure_labels(self.definitions)
        except GitHubError as error:
            next_at = self.database.now() + timedelta(
                seconds=max(PENDING_INTERVAL, retry_delay(1, self.database.now(), error)),
            )
            self.store.schedule(*identity, self.names, next_at)
            logger.warning("repository labels pending: repo={} {}", repo.full_name, error)
            if error.kind is GitHubErrorKind.RATE_LIMITED:
                raise
        else:
            next_at = self.database.now() + timedelta(seconds=CONFIRMED_INTERVAL)
            self.store.schedule(*identity, self.names, next_at)
            logger.info("repository labels confirmed: repo={} created={} next_at={}",
                        repo.full_name, created, next_at.isoformat())
        return next_at
