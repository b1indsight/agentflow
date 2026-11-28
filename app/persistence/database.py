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
from app.persistence.console_schema import CONSOLE_EVENT_LOG, CONSOLE_FACTS_VIEW
from app.persistence.records import Row
from app.persistence.schema import COLUMN_ADDITIONS, SCHEMAS

FLOW_TABLES = frozenset(
    [
        "runtime_settings",
        "flows",
        "attempts",
        "jobs",
        "workers",
        "worker_file_links",
        "flow_executions",
        "flow_tool_calls",
        "events",
        "execution_stops",
        "result_applications",
        "result_effects",
        "review_versions",
        "review_buffers",
        "review_batches",
        "reconcile_checks",
        "project_destinations",
        "project_projections",
        "console_flows",
        "console_activity",
        "console_sync",
        "console_installation_backoff",
    ]
)


def schema_relations(connection: Connection[Row]) -> dict[str, str]:
    return {
        row["relname"]: row["relkind"]
        for row in connection.execute(
            "SELECT relname,relkind FROM pg_class WHERE relnamespace=current_schema()::regnamespace"
        )
    }


def require_worker_key(connection: Connection[Row]) -> None:
    primary = connection.execute(
        "SELECT array_agg(a.attname::text ORDER BY k.ordinality) AS columns"
        " FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(attnum,ordinality)"
        " JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum"
        " WHERE c.conrelid='workers'::regclass AND c.contype='p'"
    ).fetchone()
    if primary is None or primary["columns"] != ["worker_id", "workspace"]:
        raise ValueError(
            "expected workers primary key (worker_id, workspace); reconcile worker identities offline"
        )


def _pending_schema_updates(connection: Connection[Row], relations: dict[str, str]) -> list[str]:
    """Validate the v3 baseline before selecting updates for this installation."""
    tables = {name for name, kind in relations.items() if kind in ("r", "p")}
    if (
        FLOW_TABLES | {"flowtool_schema_version"} <= tables
        and relations.get("console_facts") == "v"
    ):
        columns = {
            (r["relname"], r["attname"])
            for r in connection.execute(
                "SELECT c.relname,a.attname FROM pg_class c JOIN pg_attribute a ON c.oid=a.attrelid"
                " WHERE c.relnamespace=current_schema()::regnamespace AND a.attnum>0 AND NOT a.attisdropped"
                " AND c.relkind IN ('r','p')"
            )
        }
        if {
            ("flowtool_schema_version", "version"),
            ("flows", "history_only"),
            ("flows", "is_issue_owner"),
            ("attempts", "delivery_branch"),
            ("jobs", "attempt_id"),
            ("console_activity", "attempt_id"),
        } <= columns and connection.execute(
            "SELECT version FROM flowtool_schema_version"
        ).fetchall() == [{"version": 3}]:
            require_worker_key(connection)
            updates = [ddl for column, ddl in COLUMN_ADDITIONS.items() if column not in columns]
            if not connection.execute(
                "SELECT 1 FROM pg_trigger WHERE tgrelid='jobs'::regclass AND tgname='console_job_finished'"
            ).fetchone():
                updates.append(CONSOLE_EVENT_LOG)
            if connection.execute(
                "SELECT 1 WHERE strpos(pg_get_viewdef('console_facts'::regclass), quote_literal('awaiting_input')) = 0"
            ).fetchone():
                updates.append(CONSOLE_FACTS_VIEW)
                updates.append(
                    "SELECT console_refresh(flow_id) FROM console_flows WHERE facts->>'action'='unknown'"
                    " AND facts->>'delivery_state'='waiting' AND NOT (facts->>'history_only')::boolean ORDER BY flow_id"
                )
            return updates
    raise ValueError(
        "expected Flow schema v3; inspect the database offline with scripts.migrate_flow_attempts"
    )


def require_current_schema(connection: Connection[Row], relations: dict[str, str]) -> None:
    if pending := _pending_schema_updates(connection, relations):
        raise ValueError(f"pending schema update: {pending[0]}")


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
        self._read_snapshot = False
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

    @contextmanager
    def read_snapshot(self) -> Iterator[Database]:
        """Read authorization and paginated projections at one committed database state."""
        if self._read_snapshot:
            yield self
            return
        with self.transaction() as tx, tx.connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            tx._read_snapshot = True
            yield tx

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
        """Apply all schema declarations on bootstrap, or only pending updates on v3."""
        with self.connection() as connection:
            relations = schema_relations(connection)
            statements = _pending_schema_updates(connection, relations) if relations else SCHEMAS
            for schema in statements:
                connection.execute(schema)

    def now(self) -> datetime:
        return self._clock()

    def close(self) -> None:
        if self._bound_connection is not None:
            raise RuntimeError("only the owning database can close the connection pool")
        self._pool.close()
