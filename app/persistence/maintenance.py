"""Durable, operation-owned admission pause for single-controller planned restarts."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from psycopg import Connection
from psycopg.rows import class_row

from app.persistence.records import Row, required
from app.persistence.repository import Repository


def admissions_paused(connection: Connection[Row]) -> bool:
    """Keep a shared fence until the caller's admission transaction commits.

    Pause/resume writers lock this row before worker rows. A job or activity is
    either committed before pause returns, or sees the pause and is not admitted.
    """
    row = connection.execute(
        "SELECT operation_id FROM server_maintenance WHERE singleton FOR SHARE"
    ).fetchone()
    return required(row)["operation_id"] is not None


@dataclass(frozen=True)
class MaintenanceStatus:
    operation_id: str | None
    instance_id: str


class MaintenancePaused(RuntimeError):
    """A late business exchange must retry against the replacement controller."""


class MaintenanceStore(Repository):
    """Pause new work, retain ongoing work, and never mutate per-worker drain flags."""

    # Only runtimes tracking gateway exchanges may cut over with jobs still running.
    COMMUNICATION_IDLE = 1

    def start_instance(self) -> str:
        """Fence stale activity after the old, single controller has exited.

        Only ServerRuntime.startup calls this. An exec-based maintenance client must
        never clear these receipts or initialize the schema on a running old image.
        """
        instance = uuid4().hex
        with self._db.connection() as connection, connection.transaction():
            connection.execute(
                "UPDATE server_maintenance SET instance_id=%s WHERE singleton", (instance,)
            )
            connection.execute("DELETE FROM server_activities")
        return instance

    def status(self) -> MaintenanceStatus:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(MaintenanceStatus)) as cursor,
        ):
            return required(
                cursor.execute(
                    "SELECT operation_id,instance_id FROM server_maintenance WHERE singleton"
                ).fetchone()
            )

    def active(self) -> list[str]:
        with self._db.connection() as connection:
            return [
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM server_activities ORDER BY name,token"
                ).fetchall()
            ]

    def pause(self, operation_id: str) -> None:
        """Set a durable pause without taking over another operator's maintenance."""
        if not operation_id.strip():
            raise ValueError("maintenance requires a nonempty operation ID")
        with self._db.connection() as connection:
            changed = connection.execute(
                "UPDATE server_maintenance SET operation_id=%s,paused_at=COALESCE(paused_at,%s)"
                " WHERE singleton AND (operation_id IS NULL OR operation_id=%s)",
                (operation_id, self._db.now(), operation_id),
            )
            if changed.rowcount != 1:
                raise ValueError("maintenance is owned by another operation")

    def resume(self, operation_id: str) -> None:
        """Release our pause and give disconnected executions a fresh reconnect grace."""
        with self._db.connection() as connection, connection.transaction():
            row = connection.execute(
                "SELECT operation_id FROM server_maintenance WHERE singleton FOR UPDATE"
            ).fetchone()
            if required(row)["operation_id"] != operation_id:
                raise ValueError("maintenance is not owned by this operation")
            connection.execute(
                "SELECT worker_id FROM workers ORDER BY worker_id FOR UPDATE"
            ).fetchall()
            connection.execute(
                "UPDATE flow_executions SET recovery_deadline=GREATEST(recovery_deadline,%s)"
                " WHERE released_at IS NULL AND recovery_deadline IS NOT NULL",
                (self._db.now() + self._db.recovery_grace,),
            )
            connection.execute(
                "UPDATE jobs SET recovery_deadline=GREATEST(recovery_deadline,%s)"
                " WHERE state='running' AND recovery_deadline IS NOT NULL"
                " AND result_received_at IS NULL",
                (self._db.now() + self._db.recovery_grace,),
            )
            connection.execute(
                "UPDATE server_maintenance SET operation_id=NULL,paused_at=NULL WHERE singleton"
            )

    @contextmanager
    def exchange(self, name: str) -> Iterator[None]:
        """Fence one business exchange through its reply, never the local executor."""
        with self.activity(name) as admitted:
            if not admitted:
                raise MaintenancePaused("controller maintenance; retry after reconnect")
            yield

    @contextmanager
    def activity(self, name: str) -> Iterator[bool]:
        """Track a bounded controller work unit without holding a connection over await.

        Callers must skip their unit when False is yielded. A failure to remove a
        receipt blocks cutover; only a new controller instance clears stale receipts.
        """
        token = uuid4().hex
        with self._db.connection() as connection, connection.transaction():
            admitted = not admissions_paused(connection)
            if admitted:
                connection.execute(
                    "INSERT INTO server_activities(token,name) VALUES(%s,%s)", (token, name)
                )
        try:
            yield admitted
        finally:
            if admitted:
                with self._db.connection() as connection:
                    connection.execute("DELETE FROM server_activities WHERE token=%s", (token,))
