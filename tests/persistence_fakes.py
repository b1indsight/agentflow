"""Expose the grouped repository API over existing in-memory protocol simulations."""

from contextlib import contextmanager
from typing import Self, cast
from unittest.mock import Mock

from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.webhooks import WebhookStore
from app.persistence.workers import WorkerStore


class RepositoryViews:
    """Use the real API surface while delegating behavior to a simulation's existing methods.

    Views are created on access so transaction snapshots contain only simulation state, and
    patches/overrides on that state remain visible to every repository view.
    """

    def transaction(self):
        raise NotImplementedError("the simulation must implement transaction rollback")

    @property
    def database(self) -> Database:
        view = Mock(spec=Database, wraps=self)

        @contextmanager
        def transaction():
            with self.transaction():
                yield view

        view.transaction.side_effect = transaction
        return cast(Database, view)

    def _view(self, repository: type) -> Mock:
        view = Mock(spec=repository, wraps=self)
        view.using = Mock(return_value=view)
        return view

    @property
    def flows(self) -> FlowStore:
        return cast(FlowStore, self._view(FlowStore))

    @property
    def deliveries(self) -> DeliveryStore:
        return cast(DeliveryStore, self._view(DeliveryStore))

    @property
    def jobs(self) -> JobStore:
        return cast(JobStore, self._view(JobStore))

    @property
    def workers(self) -> WorkerStore:
        return cast(WorkerStore, self._view(WorkerStore))

    @property
    def webhooks(self) -> WebhookStore:
        return cast(WebhookStore, self._view(WebhookStore))


class MemoryRepository:
    """Bind a fake repository while keeping its simulation state shared."""

    def using(self, database: Database) -> Self:
        return self
