"""Own the repositories and bind the complete set to one database context."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from copy import copy
from datetime import datetime, timedelta

from psycopg import Connection

from app.domain.models import Flow, Job, utcnow
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.projects import ProjectStore
from app.persistence.reconciliation import ReconcileStore
from app.persistence.records import Row
from app.persistence.results import ResultStore
from app.persistence.reviews import ReviewStore
from app.persistence.schema import SCHEMAS
from app.persistence.stops import StopStore
from app.persistence.webhooks import WebhookStore
from app.persistence.workers import WorkerStore


class Store:
    """Own a repository set sharing one database context and its lifetime.

    Use ``with store.transaction() as tx:`` and call ``tx.flows``, ``tx.jobs``, etc.
    Each transaction owns a freshly bound set; ordinary repositories retain their scope.
    No network await belongs in a database transaction.
    """

    def __init__(
        self,
        database_url: str,
        *,
        clock: Callable[[], datetime] = utcnow,
        lease_duration: timedelta = timedelta(seconds=60),
        recovery_grace: timedelta = timedelta(seconds=300),
    ) -> None:
        self._bind_repositories(Database(
            database_url, clock=clock, lease_duration=lease_duration, recovery_grace=recovery_grace,
        ))

    def _bind_repositories(self, db: Database) -> None:
        self._db = db
        self._flows = FlowStore(db)
        self._deliveries = DeliveryStore(db)
        self._jobs = JobStore(db)
        self._workers = WorkerStore(db)
        self._stops = StopStore(db)
        self._results = ResultStore(db)
        self._reviews = ReviewStore(db)
        self._webhooks = WebhookStore(db)
        self._reconcile = ReconcileStore(db)
        self._projects = ProjectStore(db)

    @property
    def flows(self) -> FlowStore:
        return self._flows

    @property
    def deliveries(self) -> DeliveryStore:
        return self._deliveries

    @property
    def jobs(self) -> JobStore:
        return self._jobs

    @property
    def workers(self) -> WorkerStore:
        return self._workers

    @property
    def stops(self) -> StopStore:
        return self._stops

    @property
    def results(self) -> ResultStore:
        return self._results

    @property
    def reviews(self) -> ReviewStore:
        return self._reviews

    @property
    def webhooks(self) -> WebhookStore:
        return self._webhooks

    @property
    def reconcile(self) -> ReconcileStore:
        return self._reconcile

    @property
    def projects(self) -> ProjectStore:
        return self._projects

    @property
    def database_url(self) -> str:
        return self._db.database_url

    def connection(self) -> AbstractContextManager[Connection[Row]]:
        return self._db.connection()

    @contextmanager
    def transaction(self) -> Iterator[Store]:
        """Bind every owned repository to a fresh context with one atomic commit boundary."""
        with self._db.transaction() as db:
            bound = copy(self)
            bound._bind_repositories(db)
            yield bound

    def serialize_issue(self, repo: str, issue_number: int) -> None:
        self._db.serialize_issue(repo, issue_number)

    def lock_workflow(self, flow_id: int) -> Flow | None:
        return self._db.lock_workflow(flow_id)

    def lock_job(self, job_id: int) -> Job | None:
        return self._db.lock_job(job_id)

    def init_db(self) -> None:
        """Initialize the complete schema in its foreign-key dependency order."""
        with self._db.connection() as connection:
            for schema in SCHEMAS:
                connection.execute(schema)

    def now(self) -> datetime:
        return self._db.now()

    def close(self) -> None:
        self._db.close()
