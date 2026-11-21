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


def flow_schema_version(connection: Connection[Row]) -> str:
    """Only unmarked databases need structural identification; never auto-upgrade them."""
    tables = connection.execute(
        "SELECT relname FROM pg_class WHERE relnamespace=current_schema()::regnamespace"
        " AND relkind IN ('r','p')"
    ).fetchall()
    names = {row["relname"] for row in tables}
    if names & {"issue_flow_owners", "flow_deliveries", "webhook_inbox"}:
        raise ValueError("pre-185 schema: stop writers and migrate the three legacy tables offline")
    if not names:
        return "empty"
    columns = {
        (row["table_name"], row["column_name"])
        for row in connection.execute(
            "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=current_schema()"
        )
    }
    if "workers" in names:
        primary = connection.execute(
            "SELECT array_agg(a.attname::text ORDER BY k.ordinality) AS columns"
            " FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(attnum,ordinality)"
            " JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum"
            " WHERE c.conrelid='workers'::regclass AND c.contype='p'"
        ).fetchone()
        if primary is None or primary["columns"] != ["worker_id", "workspace"]:
            raise ValueError(
                "legacy worker schema: explicit workspace migration required; stop consumers"
                " and reconcile real worker-root workspace identities before upgrading"
            )
        if columns & {("workers", "workspace_state"), ("workers", "retired_generation")}:
            raise ValueError(
                "obsolete workspace state schema: this unreleased schema requires explicit"
                " reconciliation before upgrading; automatic migration is not supported"
            )
    legacy = {
        ("flows", "current_round_no"),
        ("flows", "delivery_branch"),
        ("attempts", "round_no"),
        ("jobs", "round_no"),
        ("console_activity", "round_no"),
    }
    new = {
        ("attempts", "delivery_branch"),
        ("jobs", "attempt_id"),
        ("console_activity", "attempt_id"),
    }
    if "flowtool_schema_version" in names:
        versions = connection.execute("SELECT version FROM flowtool_schema_version").fetchall()
        if versions != [{"version": 2}]:
            raise ValueError(f"unsupported Flow schema version: {versions}")
        if new <= columns and not (legacy & columns) and "flow_attempt_legacy_map" in names:
            return "current"
    elif (
        legacy <= columns
        and not (new & columns)
        and "flow_attempt_legacy_map" not in names
        and {"workers", "result_applications", "flow_tool_calls", "review_batches", "events"}
        <= names
        and ("flows", "is_issue_owner") in columns
    ):
        return "legacy"
    raise ValueError("unrecognized or partial Flow schema: offline investigation required")


def mark_flow_schema_current(connection: Connection[Row]) -> None:
    """Publish the cutover in the same commit as the complete schema and legacy mapping."""
    connection.execute("""
        CREATE TABLE flow_attempt_legacy_map (
          flow_id bigint NOT NULL, round_no integer NOT NULL CHECK(round_no>0),
          attempt_id bigint NOT NULL UNIQUE, PRIMARY KEY(flow_id,round_no),
          FOREIGN KEY(flow_id,attempt_id) REFERENCES attempts(flow_id,id));
        CREATE TABLE flowtool_schema_version(version integer PRIMARY KEY CHECK(version=2));
        INSERT INTO flowtool_schema_version VALUES(2);
    """)


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

    def serialize_issue(self, repo: str, issue_number: int | None) -> None:
        """Serialize trigger/retry of the same issue, including creation before a Flow row exists."""
        if self._bound_connection is None:
            raise RuntimeError("issue lock requires a workflow transaction")
        if issue_number is None:
            return
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

    def lock_worker(self, worker_id: str) -> None:
        """Keep assignment writers ordered worker -> job/binding, like workflow completion."""
        if self._bound_connection is None:
            raise RuntimeError("worker lock requires a transaction")
        self._bound_connection.execute(
            "SELECT worker_id,workspace FROM workers WHERE worker_id=%s ORDER BY workspace FOR UPDATE",
            (worker_id,),
        ).fetchall()

    def lock_workers(self) -> None:
        """Fence cross-slot selection/recovery before taking any Flow or job lock.

        Inference's local preference compares slots across workers. Lock in the same
        deterministic order as workflow locking, never skip a locked local candidate.
        """
        if self._bound_connection is None:
            raise RuntimeError("worker lock requires a transaction")
        self._bound_connection.execute(
            "SELECT worker_id,workspace FROM workers ORDER BY worker_id,workspace FOR UPDATE"
        ).fetchall()

    def init_db(self) -> None:
        """Initialize the complete schema in its foreign-key dependency order."""
        with self.connection() as connection:
            version = flow_schema_version(connection)
            if version == 'legacy':
                raise ValueError('legacy Flow schema: stop writers and run scripts.migrate_flow_attempts offline')
            for schema in SCHEMAS:
                connection.execute(schema)
            if version == 'empty':
                mark_flow_schema_current(connection)

    def now(self) -> datetime:
        return self._clock()

    def close(self) -> None:
        if self._bound_connection is not None:
            raise RuntimeError("only the owning database can close the connection pool")
        self._pool.close()
