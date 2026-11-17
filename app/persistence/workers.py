"""Worker sessions and workspace inventory."""

from __future__ import annotations

from typing import Literal

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.domain.models import JobState, WorkerRecord, WorkerStatus, WorkspaceInventory
from app.persistence.inference import InferenceStore
from app.persistence.jobs import MAY_HAVE_EXECUTED_SQL
from app.persistence.records import required
from app.persistence.repository import Repository


class WorkerStore(Repository):
    """Who is connected, what each worker can run, and which workspaces it reports.

    Losing a session (disconnect, replacement, or controller restart) only fences its
    work here: unknown inference attempts are recovered and possibly-executed jobs get
    a recovery deadline. Assignments themselves belong to the assignment store.
    """

    def worker_connected(
        self,
        worker_id: str,
        protocol_version: int,
        connection_epoch: str = "",
        *, workspace: str, repo_workspace_limit: int, execute: bool = True,
        coding_agent: Literal["codex", "amp"] | None,
    ) -> WorkerRecord:
        WorkspaceInventory(workspace=workspace, repository=None, generation=1, flow_id=None)
        if repo_workspace_limit <= 0:
            raise ValueError("repo workspace limit must be positive")
        now = self._db.now()
        with (
            self._db.transaction() as tx,
            tx.connection() as connection,
            connection.cursor(row_factory=class_row(WorkerRecord)) as cursor,
        ):
            tx.lock_workers()
            previous = cursor.execute(
                "SELECT * FROM workers WHERE worker_id=%s AND repository IS NULL", (worker_id,),
            ).fetchone()
            if previous is not None and previous.workspace != workspace:
                raise ValueError("default workspace identity changed; reconcile the worker root")
            if connection.execute(
                "SELECT 1 FROM workers WHERE worker_id=%s AND workspace=%s AND repository IS NOT NULL",
                (worker_id, workspace),
            ).fetchone() is not None:
                raise ValueError("default workspace conflicts with repository inventory")
            if previous is not None and previous.connection_epoch != connection_epoch:
                InferenceStore(tx).recover_inference(worker_id)
            row = cursor.execute(
                "INSERT INTO workers(worker_id,workspace,repo_workspace_limit, protocol_version, status, connection_epoch,"
                " current_job_id, first_connected_at, last_connected_at, last_seen_at,"
                " last_error, last_disconnect_reason,execute,coding_agent)"
                " VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,NULL,NULL,%s,%s)"
                " ON CONFLICT(worker_id,workspace) DO UPDATE SET protocol_version=excluded.protocol_version,"
                " status='online',repo_workspace_limit=excluded.repo_workspace_limit,"
                " connection_epoch=excluded.connection_epoch,execute=excluded.execute,inference=NULL,"
                " coding_agent=excluded.coding_agent,"
                " last_connected_at=excluded.last_connected_at,"
                " last_seen_at=excluded.last_seen_at, last_error=NULL RETURNING *",
                (
                    worker_id,
                    workspace,
                    repo_workspace_limit,
                    protocol_version,
                    WorkerStatus.ONLINE,
                    connection_epoch,
                    now,
                    now,
                    now,
                    execute,
                    coding_agent,
                ),
            ).fetchone()
            connection.execute(
                "UPDATE workers SET inventory_epoch=NULL WHERE worker_id=%s",
                (worker_id,),
            )
        return required(row)

    def report_workspace(self, worker_id: str, epoch: str, item: WorkspaceInventory) -> bool:
        """Register concrete identity and freshness without changing ownership.

        The worker can retain a previous local binding until its next assignment.
        Lower reported generations are therefore expected after controller release;
        neither that generation nor the advisory flow_id overwrites controller facts.
        """
        with self._db.transaction() as tx, tx.connection() as conn:
            tx.lock_workers()
            owner = conn.execute(
                "SELECT * FROM workers WHERE worker_id=%s AND repository IS NULL"
                " AND connection_epoch=%s AND status IN ('online','busy')", (worker_id, epoch),
            ).fetchone()
            if owner is None:
                return False
            row = conn.execute(
                "SELECT * FROM workers WHERE worker_id=%s AND workspace=%s", (worker_id, item.workspace),
            ).fetchone()
            if row is not None and (row["repository"] != item.repository or item.generation > row["generation"]):
                return False
            if item.repository is None and (row is None or item.workspace != owner["workspace"] or item.flow_id is not None):
                return False
            if row is None:
                if item.generation != 1:
                    return False
                count = conn.execute(
                    "SELECT count(*) AS n FROM workers WHERE worker_id=%s AND repository=%s",
                    (worker_id, item.repository),
                ).fetchone()
                if item.repository is not None and required(count)["n"] >= owner["repo_workspace_limit"]:
                    return False
            conn.execute(
                "INSERT INTO workers(worker_id,workspace,repository,generation,inventory_epoch,"
                " protocol_version,status,first_connected_at,last_connected_at,last_seen_at,execute)"
                " VALUES (%s,%s,%s,%s,%s,%s,'online',%s,%s,%s,FALSE)"
                " ON CONFLICT(worker_id,workspace) DO UPDATE SET inventory_epoch=EXCLUDED.inventory_epoch",
                (worker_id, item.workspace, item.repository, item.generation, epoch,
                 owner["protocol_version"], tx.now(), tx.now(), tx.now()),
            )
            return True

    def worker_ready(self, worker_id: str, connection_epoch: str,
                     backend: str | None) -> bool:
        """Publish inference readiness for this authenticated, fully registered session only."""
        with self._db.connection() as connection:
            result = connection.execute(
                "UPDATE workers SET inference=%s WHERE worker_id=%s AND connection_epoch=%s"
                " AND repository IS NULL AND status IN ('online','busy')",
                (Jsonb(backend) if backend else None, worker_id, connection_epoch),
            )
            return result.rowcount == 1

    def worker_disconnected(
        self,
        worker_id: str,
        reason: str | None = None,
        connection_epoch: str = "",
    ) -> None:
        now = self._db.now()
        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_workers()
            if connection.execute("SELECT 1 FROM workers WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s",
                                  (worker_id, connection_epoch)).fetchone() is None:
                return
            InferenceStore(tx).recover_inference(worker_id)
            connection.execute(
                "UPDATE flow_executions SET recovery_deadline=COALESCE(recovery_deadline,%s)"
                " WHERE worker_id=%s AND released_at IS NULL"
                " AND EXISTS (SELECT 1 FROM workers WHERE repository IS NULL AND worker_id=%s"
                " AND connection_epoch=%s)",
                (now + self._db.recovery_grace, worker_id, worker_id, connection_epoch),
            )
            connection.execute(
                "UPDATE jobs SET recovery_deadline=COALESCE(recovery_deadline,%s), updated_at=%s"
                " WHERE state=%s AND " + MAY_HAVE_EXECUTED_SQL + " AND assigned_worker_id=%s"
                " AND kind<>'inference'"
                " AND EXISTS (SELECT 1 FROM workers WHERE repository IS NULL AND worker_id=%s"
                " AND connection_epoch=%s)",
                (
                    now + self._db.recovery_grace,
                    now,
                    JobState.RUNNING,
                    worker_id,
                    worker_id,
                    connection_epoch,
                ),
            )
            connection.execute(
                "UPDATE workers SET status=CASE WHEN EXISTS (SELECT 1 FROM jobs"
                " WHERE jobs.id=workers.current_job_id AND jobs.state=%s"
                " AND jobs.started_at IS NOT NULL) OR EXISTS (SELECT 1"
                " FROM flow_executions e WHERE e.worker_id=workers.worker_id"
                " AND e.released_at IS NULL) THEN %s ELSE %s END,"
                " inference=NULL,last_seen_at=%s, last_error=%s, last_disconnect_reason=%s"
                " WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s",
                (
                    JobState.RUNNING,
                    WorkerStatus.RECOVERING,
                    WorkerStatus.OFFLINE,
                    now,
                    reason,
                    reason,
                    worker_id,
                    connection_epoch,
                ),
            )

    def recover_abandoned_assignments(self) -> None:
        """Fence startup: preserve coding recovery and retry unknown inference attempts."""

        now = self._db.now()
        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_workers()
            connection.execute(
                "UPDATE workers SET status=%s,inference=NULL WHERE repository IS NULL",
                (WorkerStatus.OFFLINE,),
            )
            InferenceStore(tx).recover_inference()
            connection.execute(
                "UPDATE jobs SET recovery_deadline=COALESCE(recovery_deadline,%s),"
                " updated_at=%s WHERE kind<>'inference' AND state=%s AND " + MAY_HAVE_EXECUTED_SQL,
                (now + self._db.recovery_grace, now, JobState.RUNNING),
            )
            connection.execute(
                "UPDATE flow_executions SET recovery_deadline=COALESCE(recovery_deadline,%s)"
                " WHERE released_at IS NULL",
                (now + self._db.recovery_grace,),
            )

    def list_workers(self) -> list[WorkerRecord]:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(WorkerRecord)) as cursor,
        ):
            return cursor.execute("SELECT * FROM workers WHERE repository IS NULL ORDER BY worker_id").fetchall()

    def list_workspaces(self, worker_id: str) -> list[WorkerRecord]:
        """Project shared authority from the default row onto each concrete inventory row."""
        with self._db.connection() as conn:
            return conn.cursor(row_factory=class_row(WorkerRecord)).execute(
                "SELECT slot.*,owner.protocol_version,owner.status,owner.connection_epoch,owner.draining,"
                " owner.execute,owner.coding_agent,owner.inference,owner.repo_workspace_limit,owner.last_seen_at,"
                " owner.first_connected_at,owner.last_connected_at,owner.last_disconnect_reason"
                " FROM workers slot JOIN workers owner ON owner.worker_id=slot.worker_id AND owner.repository IS NULL"
                " WHERE slot.worker_id=%s ORDER BY slot.workspace", (worker_id,),
            ).fetchall()

    def drain(self, worker_id: str, enabled: bool) -> None:
        with self._db.connection() as conn:
            result = conn.execute(
                "UPDATE workers SET draining=%s WHERE worker_id=%s AND repository IS NULL", (enabled, worker_id),
            )
            if result.rowcount != 1:
                raise ValueError('unknown worker')
