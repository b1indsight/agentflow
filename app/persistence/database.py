"""Shared PostgreSQL connections, transaction scope, clock, and lock ordering."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import copy
from datetime import datetime, timedelta
from typing import cast

from psycopg import Connection
from psycopg.rows import class_row, dict_row
from psycopg_pool import ConnectionPool

from app.domain.models import Flow, Job, utcnow
from app.persistence.records import Row
from app.persistence.schema import SCHEMAS


class _WorkflowOwnershipChanged(Exception):
    """Retry discovery after rolling back just the locks taken by lock_workflow."""


class Database:
    """Own database resources independently of the services and repositories using them.

    A transaction yields a separate context bound to one connection; repositories built
    from it share that transaction and reject access after the context exits.
    """

    def __init__(
        self,
        database_url: str,
        *,
        clock: Callable[[], datetime] = utcnow,
        lease_duration: timedelta = timedelta(seconds=60),
        recovery_grace: timedelta = timedelta(seconds=300),
    ) -> None:
        self.database_url = database_url
        self._clock = clock
        self.lease_duration = lease_duration
        self.recovery_grace = recovery_grace
        self._bound_connection: Connection[Row] | None = None
        self._transaction_closed = False
        self._pool = cast(
            ConnectionPool[Connection[Row]],
            ConnectionPool(
                conninfo=database_url,
                min_size=1,
                max_size=4,
                open=True,
                kwargs={"row_factory": dict_row},
            ),
        )

    @contextmanager
    def connection(self) -> Iterator[Connection[Row]]:
        """Borrow the transaction's connection, or commit one standalone repository operation."""
        if self._transaction_closed:
            raise RuntimeError("transaction repository used outside its context")
        if self._bound_connection is not None:
            yield self._bound_connection
        else:
            with self._pool.connection() as connection:
                yield connection

    @contextmanager
    def transaction(self) -> Iterator[Database]:
        """Yield an explicitly bound repository; exceptions roll back every operation in the unit.

        Build repositories from the yielded context to share its connection.
        The view shares the pool and configuration, not mutable transaction state with callers.
        No network await belongs inside this context.
        """
        with self.connection() as connection, connection.transaction():
            bound = copy(self)
            bound._bound_connection = connection
            try:
                yield bound
            finally:
                bound._transaction_closed = True

    def serialize_issue(self, repo: str, issue_number: int) -> None:
        """Serialize trigger/retry of the same issue, including creation before a Flow row exists."""
        if self._bound_connection is None:
            raise RuntimeError("issue lock requires a workflow transaction")
        self._bound_connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"flowtool:{repo}#{issue_number}",),
        )

    def lock_workflow(self, flow_id: int) -> Flow | None:
        """Lock both slots' owners before the Flow, rediscovering ownership after waits."""
        if self._bound_connection is None:
            raise RuntimeError("workflow lock requires a transaction")
        connection = self._bound_connection
        owners = (
            "SELECT worker_id,workspace FROM workers WHERE worker_id IN"
            " (SELECT worker_id FROM flow_executions WHERE flow_id=%s)"
            " OR current_job_id IN (SELECT id FROM jobs WHERE flow_id=%s)"
            " OR current_inference_job_id IN (SELECT id FROM jobs WHERE flow_id=%s)"
        )
        while True:
            try:
                with connection.transaction():
                    locked = connection.execute(owners + " ORDER BY worker_id,workspace FOR UPDATE",
                                                (flow_id, flow_id, flow_id)).fetchall()
                    with connection.cursor(row_factory=class_row(Flow)) as cursor:
                        flow = cursor.execute("SELECT * FROM flows WHERE id=%s FOR NO KEY UPDATE",
                                              (flow_id,)).fetchone()
                    current = connection.execute(owners, (flow_id, flow_id, flow_id)).fetchall()
                    if not {(row["worker_id"], row["workspace"]) for row in current} <= {
                        (row["worker_id"], row["workspace"]) for row in locked
                    }:
                        # A claim can publish a new slot while this statement waits for
                        # the Flow. Release this savepoint's locks instead of taking the
                        # newly discovered worker after the Flow (a lock-order inversion).
                        raise _WorkflowOwnershipChanged
                    return flow
            except _WorkflowOwnershipChanged:
                continue

    def lock_job(self, job_id: int) -> Job | None:
        """Lock a job after its workflow has been locked in the same transaction."""
        if self._bound_connection is None:
            raise RuntimeError("job lock requires a workflow transaction")
        with self._bound_connection.cursor(row_factory=class_row(Job)) as cursor:
            return cursor.execute("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()

    @staticmethod
    def _lock_worker(connection: Connection[Row], worker_id: str) -> None:
        """Keep assignment writers ordered worker -> job/binding, like workflow completion."""
        connection.execute(
            "SELECT worker_id,workspace FROM workers WHERE worker_id=%s ORDER BY workspace FOR UPDATE",
            (worker_id,),
        ).fetchall()

    @staticmethod
    def _lock_workers(connection: Connection[Row]) -> None:
        """Fence cross-slot selection/recovery before taking any Flow or job lock.

        Inference's local preference compares slots across workers. Lock in the same
        deterministic order as workflow locking, never skip a locked local candidate.
        """
        connection.execute(
            "SELECT worker_id,workspace FROM workers ORDER BY worker_id,workspace FOR UPDATE"
        ).fetchall()

    def init_db(self) -> None:
        """Initialize the complete schema in its foreign-key dependency order."""
        with self.connection() as connection:
            workers = connection.execute("SELECT to_regclass('workers') AS name").fetchone()
            if workers is not None and workers["name"]:
                primary = connection.execute(
                    "SELECT array_agg(a.attname::text ORDER BY k.ordinality) AS columns"
                    " FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey)"
                    " WITH ORDINALITY k(attnum,ordinality)"
                    " JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum"
                    " WHERE c.conrelid='workers'::regclass AND c.contype='p'"
                ).fetchone()
                if primary is None or primary["columns"] != ["worker_id", "workspace"]:
                    raise ValueError(
                        "legacy worker schema: explicit workspace migration required; stop consumers"
                        " and reconcile real worker-root workspace identities before upgrading"
                    )
                obsolete = connection.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema()"
                    " AND table_name='workers' AND column_name IN ('workspace_state','retired_generation')"
                ).fetchone()
                if obsolete is not None:
                    raise ValueError(
                        "obsolete workspace state schema: this unreleased schema requires explicit"
                        " reconciliation before upgrading; automatic migration is not supported"
                    )
            existing = connection.execute("SELECT to_regclass('flows') AS name").fetchone()
            if existing is not None and existing["name"]:
                columns = connection.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema()"
                    " AND table_name='flows' AND column_name='current_round_no'"
                ).fetchone()
                if columns is None:
                    raise ValueError(
                        "legacy Flow schema: stop consumers and run scripts/migrate_flow_states.py"
                    )
            for schema in SCHEMAS:
                connection.execute(schema)

    def now(self) -> datetime:
        return self._clock()

    def close(self) -> None:
        if self._bound_connection is not None:
            raise RuntimeError("only the owning database can close the connection pool")
        self._pool.close()
