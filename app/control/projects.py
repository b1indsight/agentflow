"""Independent bounded Projects convergence; remote failures never mutate a Flow."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger

from app.github_models import GitHubAdapterError, GitHubErrorKind
from app.ports import ProjectsClient
from app.project_models import ProjectConfig
from app.project_store import Projection, ProjectStore
from app.store import Store


class DestinationChanged(Exception):
    pass


class ProjectSync:
    def __init__(self, store: Store, configs: dict[str, ProjectConfig],
                 client: Callable[[ProjectConfig], ProjectsClient], *, interval: float = 300,
                 batch_size: int = 64, concurrency: int = 4) -> None:
        self.store, self.configs, self.client = store, configs, client
        self.projects = ProjectStore(store)
        self.interval, self.batch_size, self.concurrency = interval, batch_size, concurrency
        self.after = 0

    async def run(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 -- operational failures retry independently
                logger.exception("Projects sweep failed")
            await asyncio.sleep(5)

    async def sweep(self) -> None:
        for repo, config in list(self.configs.items()):
            self.projects.seed(repo, config, self.batch_size)
        rows = self.projects.list_projections(after=self.after, limit=self.batch_size)
        self.after = rows[-1].id if len(rows) == self.batch_size else 0
        for row in rows:
            selected = self.configs.get(row.repo)
            if selected and selected.project_id == row.project_id:
                self.projects.refresh(row.id, selected)
        due = iter(self.projects.due(self.configs, self.batch_size))

        async def consume() -> None:
            for row in due:
                try:
                    async with self.projects.claim(row) as acquired:
                        if acquired:
                            await self.sync(row)
                except Exception:  # noqa: BLE001 -- isolate one item from the remaining bounded batch
                    logger.exception("Project projection {} failed", row.id)

        async with asyncio.TaskGroup() as group:
            for _ in range(self.concurrency):
                group.create_task(consume())

    async def _call[T](self, row: Projection, config: ProjectConfig, request: Callable[[], Awaitable[T]]) -> T:
        if self.configs.get(row.repo) != config:
            raise DestinationChanged
        return await request()

    async def sync(self, row: Projection) -> None:
        config = self.configs.get(row.repo)
        if not config or config.project_id != row.project_id:
            return
        row = self.projects.refresh(row.id, config)
        if row.blocked or row.next_at > self.store.now():
            return
        api = self.client(config)
        try:
            await self._call(row, config, lambda: api.validate(row.repo))
            if row.content_id is None:
                content = await self._call(row, config, lambda: api.issue_id(row.repo, row.issue_number))
                self.projects.save(row, content_id=content)
                row = self.projects.get(row.id)
            if row.item_id is None:
                page = await self._call(row, config, lambda: api.items_page(row.cursor))
                found = [item for item in page.items if item.content_id == row.content_id]
                if found:
                    self.projects.save(row, item_id=found[0].id, cursor=None, scan_incomplete=False, add_started=False)
                elif page.next_cursor:
                    self.projects.save(row, cursor=page.next_cursor,
                                       scan_incomplete=row.scan_incomplete or not page.complete)
                    return
                elif row.scan_incomplete or not page.complete or row.add_started:
                    self.projects.save(row, blocked=True, last_error="project item absence is unconfirmed; retry after inspection")
                    return
                else:
                    fresh = self.projects.refresh(row.id, config)
                    if fresh.desired_version != row.desired_version or fresh.blocked:
                        return
                    assert row.content_id is not None
                    self.projects.save(row, add_started=True, cursor=None, scan_incomplete=False)
                    try:
                        added = await self._call(row, config, lambda: api.add_issue(row.content_id or ""))
                    except GitHubAdapterError as error:
                        if error.definitely_unsent or error.request_rejected:
                            self.projects.save(row, add_started=False)
                        raise
                    self.projects.save(row, item_id=added.id, add_started=False)
                row = self.projects.get(row.id)
            assert row.item_id is not None
            remote = await self._call(row, config, lambda: api.read_item(row.item_id or ""))
            if remote is None or remote.archived or remote.content_id != row.content_id:
                self.projects.save(row, blocked=True, last_error="known project item missing, archived, or content unavailable")
                return
            fresh = self.projects.refresh(row.id, config)
            if fresh.desired_version != row.desired_version or fresh.blocked:
                return
            if remote.option_id != config.options[row.desired_status]:
                await self._call(row, config, lambda: api.set_status(row.item_id or "", row.desired_status))
            self.projects.acknowledge(row, self.interval)
        except DestinationChanged:
            return
        except GitHubAdapterError as error:
            # A known NOT_FOUND item is blocked, never reinterpreted as permission to add.
            if row.item_id and error.kind is GitHubErrorKind.NOT_FOUND:
                self.projects.save(row, blocked=True, last_error="known project item not found or inaccessible")
            else:
                self.projects.failed(row, error)
