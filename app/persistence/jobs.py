"""Durable jobs, lifecycle checks, and accepted-result application."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.contracts.protocol import ArchiveTask
from app.domain.models import (
    Job,
    JobKind,
    JobState,
    WorkerStatus,
)
from app.persistence.records import required

if TYPE_CHECKING:
    from app.persistence.database import Database


class JobStore:
    """Durable jobs, lifecycle checks, and accepted-result application."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def cancel_jobs(self, flow_id: int, *, reason: str = "Flow cancelled") -> None:
        """Retire queued/never-dispatched preparation; actual execution requires stop proof."""
        # Like FlowStore.update_flow, cancellation reuses reviews without an import cycle.
        from app.persistence.reviews import ReviewStore

        ReviewStore(self._db).cancel(flow_id)
        with self._db.connection() as connection:
            connection.execute(
                "WITH retired AS (UPDATE jobs SET state=%s, last_error=%s, updated_at=%s WHERE flow_id=%s"
                " AND (state=%s OR (state='running' AND started_at IS NULL AND dispatch_authorized_at IS NULL"
                " AND EXISTS (SELECT 1 FROM flows f WHERE f.id=jobs.flow_id AND f.state='cancelled')))"
                " AND result_received_at IS NULL RETURNING id)"
                " UPDATE workers SET current_job_id=NULL,status=CASE WHEN status='busy' THEN 'online' ELSE status END"
                " WHERE current_job_id IN (SELECT id FROM retired)",
                (
                    JobState.FAILED,
                    reason,
                    self._db.now(),
                    flow_id,
                    JobState.QUEUED,
                ),
            )

    def lifecycle_job(self, flow_id: int) -> Job | None:
        """Latest coding execution, excluding jobs cancelled before they could run."""
        with self._db.connection() as connection:
            return connection.cursor(row_factory=class_row(Job)).execute(
                "SELECT * FROM jobs WHERE flow_id=%s AND kind IN ('initial','revision')"
                " AND (state='running' OR started_at IS NOT NULL OR result_received_at IS NOT NULL)"
                " AND NOT (started_at IS NULL AND COALESCE(result_payload->>'connection_epoch','')='control')"
                " ORDER BY id DESC LIMIT 1", (flow_id,),
            ).fetchone()

    def reopen_blocked(self, flow_id: int) -> bool:
        """A timeout cannot prove that an executor exited or a dispatched effect settled."""
        with self._db.connection() as connection:
            return connection.execute(
                "SELECT 1 FROM jobs WHERE flow_id=%s AND (state='running'"
                " OR (result_received_at IS NOT NULL AND result_applied_at IS NULL)"
                " OR (started_at IS NOT NULL AND result_received_at IS NULL)) LIMIT 1", (flow_id,),
            ).fetchone() is not None

    def has_pending_result(self, flow_id: int) -> bool:
        with self._db.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM jobs WHERE flow_id=%s AND result_received_at IS NOT NULL"
                    " AND result_applied_at IS NULL LIMIT 1",
                    (flow_id,),
                ).fetchone()
                is not None
            )

    def has_unsettled_jobs(self, flow_id: int, *, excluding_job_id: int) -> bool:
        """Check for another executor or unapplied result without loading job payloads."""
        with self._db.connection() as connection:
            return connection.execute(
                "SELECT 1 FROM jobs WHERE flow_id=%s AND id<>%s"
                " AND (state=%s OR (result_received_at IS NOT NULL"
                " AND result_applied_at IS NULL)) LIMIT 1",
                (flow_id, excluding_job_id, JobState.RUNNING),
            ).fetchone() is not None

    def enqueue_job(
        self,
        flow_id: int,
        kind: JobKind,
        payload: dict | None = None,
        *,
        source_job_id: int | None = None,
        source_key: str | None = None,
    ) -> Job:
        if (source_job_id is None) != (source_key is None):
            raise ValueError("derived jobs require both source job and purpose")
        now = self._db.now()
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Job)) as cursor,
        ):
            row = cursor.execute(
                "INSERT INTO jobs(flow_id, kind, state, attempt, payload, created_at,"
                " updated_at,source_job_id,source_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT(source_job_id,source_key) WHERE source_job_id IS NOT NULL"
                " DO UPDATE SET source_key=excluded.source_key"
                " WHERE jobs.flow_id=excluded.flow_id AND jobs.kind=excluded.kind"
                " AND jobs.payload IS NOT DISTINCT FROM excluded.payload RETURNING *",
                (
                    flow_id,
                    kind,
                    JobState.QUEUED,
                    0,
                    Jsonb(payload) if payload else None,
                    now,
                    now,
                    source_job_id,
                    source_key,
                ),
            ).fetchone()
        return required(row)

    def get_job(self, job_id: int) -> Job | None:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Job)) as cursor,
        ):
            return cursor.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()

    def list_jobs(self, flow_id: int) -> list[Job]:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Job)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM jobs WHERE flow_id=%s ORDER BY id", (flow_id,)
            ).fetchall()

    def freeze_archive(self, job: Job, task: ArchiveTask) -> None:
        """Persist the command under the caller's workflow/job locks before dispatch."""
        with self._db.connection() as connection:
            connection.execute(
                "UPDATE jobs SET payload=%s WHERE id=%s",
                (Jsonb({**(job.payload or {}), "archive": task.model_dump(mode="json")}), job.id),
            )

    def latest_failed_job(self, flow_id: int) -> Job | None:
        """Return the current failed job; a later job retires every earlier retry candidate."""
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Job)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM (SELECT * FROM jobs WHERE flow_id=%s ORDER BY id DESC LIMIT 1)"
                " latest WHERE state=%s",
                (flow_id, JobState.FAILED),
            ).fetchone()

    def unapplied_result_ceiling(self) -> int:
        """Freeze a finite scan boundary so new results cannot postpone older retries forever."""
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(id),0) AS ceiling FROM jobs WHERE state=%s"
                " AND result_received_at IS NOT NULL AND result_applied_at IS NULL",
                (JobState.RUNNING,),
            ).fetchone()
            assert row is not None
            return row["ceiling"]

    def list_unapplied_results(
        self, *, after_id: int = 0, through_id: int | None = None, limit: int = 64,
    ) -> list[Job]:
        """Page interrupted terminal results by stable job ID; recovery wraps after the last page."""

        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Job)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM jobs WHERE state=%s AND result_received_at IS NOT NULL"
                " AND result_applied_at IS NULL AND id>%s"
                " AND (%s::bigint IS NULL OR id<=%s) ORDER BY id LIMIT %s",
                (JobState.RUNNING, after_id, through_id, through_id, limit),
            ).fetchall()

    def complete_persisted_result(self, job_id: int, state: JobState) -> bool:
        """Complete an inbox item after recovery applies its stored domain result."""

        if state not in (JobState.DONE, JobState.FAILED):
            raise ValueError("persisted results can only complete terminal jobs")
        now = self._db.now()
        with self._db.connection() as connection, connection.transaction():
            owner = connection.execute(
                "SELECT assigned_worker_id FROM jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
            if owner and owner["assigned_worker_id"]:
                self._db._lock_worker(connection, owner["assigned_worker_id"])
            completed = connection.execute(
                "UPDATE jobs SET state=%s, result_applied_at=%s, updated_at=%s"
                " WHERE id=%s AND state=%s AND result_received_at IS NOT NULL"
                " AND result_applied_at IS NULL RETURNING assigned_worker_id",
                (state, now, now, job_id, JobState.RUNNING),
            ).fetchone()
            if completed is not None:
                connection.execute(
                    "UPDATE workers SET status=CASE WHEN status IN (%s,%s) THEN %s"
                    " ELSE %s END, current_job_id=NULL, last_seen_at=%s"
                    " WHERE worker_id=%s AND current_job_id=%s",
                    (
                        WorkerStatus.OFFLINE,
                        WorkerStatus.RECOVERING,
                        WorkerStatus.OFFLINE,
                        WorkerStatus.ONLINE,
                        now,
                        completed["assigned_worker_id"],
                        job_id,
                    ),
                )
                connection.execute(
                    "UPDATE flow_executions SET recovery_deadline=CASE WHEN EXISTS"
                    " (SELECT 1 FROM workers WHERE worker_id=%s AND status=%s) THEN NULL ELSE %s END"
                    " WHERE flow_id=(SELECT flow_id FROM jobs WHERE id=%s) AND released_at IS NULL",
                    (
                        completed["assigned_worker_id"],
                        WorkerStatus.ONLINE,
                        now + self._db.recovery_grace,
                        job_id,
                    ),
                )
        return completed is not None

    def record_control_result(
        self,
        job_id: int,
        payload: Mapping[str, object],
        digest: str,
        reason: str,
    ) -> None:
        """Record a control-generated terminal outcome while holding the workflow/job locks."""
        with self._db.connection() as connection:
            connection.execute(
                "UPDATE jobs SET result_payload=%s,result_digest=%s,result_received_at=%s,"
                " last_error=%s WHERE id=%s AND state=%s"
                " AND result_received_at IS NULL",
                (Jsonb(dict(payload)), digest, self._db.now(), reason, job_id, JobState.RUNNING),
            )

    def set_job_error(self, job_id: int, reason: str) -> None:
        with self._db.connection() as connection:
            connection.execute("UPDATE jobs SET last_error=%s WHERE id=%s", (reason, job_id))
