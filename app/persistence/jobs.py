"""Durable jobs, lifecycle checks, and accepted-result application."""

from __future__ import annotations

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.contracts.protocol import ArchiveTask, Failed, terminal_digest
from app.contracts.receipts import MergeArchiveNotice, ResultInbox, merge_notice_digest
from app.domain.flow_policy import FlowEvent
from app.domain.models import (
    Flow,
    FlowDelivery,
    Job,
    JobKind,
    JobState,
    WorkerStatus,
)
from app.persistence.flows import FlowStore
from app.persistence.records import required
from app.persistence.repository import Repository
from app.persistence.results import RESOURCE_JOB_SQL

# Proof that a job may have run, for unaliased jobs. A frozen archive may have started
# deleting before its Started message reached serve.
MAY_HAVE_EXECUTED_SQL = (
    "(started_at IS NOT NULL OR dispatch_authorized_at IS NOT NULL OR (kind='archive' AND payload->'archive' IS NOT NULL))"
)

# New live operations require the immutable assignment's exact current claim (unaliased jobs).
# Historical terminal receipt replays are validated against their stored digest instead.
CURRENT_WORKSPACE_SQL = (
    " EXISTS (SELECT 1 FROM workers slot WHERE slot.worker_id=jobs.assigned_worker_id"
    " AND slot.workspace=jobs.workspace AND slot.generation=jobs.generation"
    " AND ((jobs.kind='inference' AND slot.current_inference_job_id=jobs.id)"
    " OR (jobs.kind<>'inference' AND slot.current_job_id=jobs.id)))"
)


class JobStore(Repository):
    """Durable jobs, lifecycle checks, and accepted-result application."""

    def lifecycle_job(self, flow_id: int) -> Job | None:
        """Latest coding execution, excluding jobs cancelled before they could run."""
        with self._db.connection() as connection:
            return (
                connection.cursor(row_factory=class_row(Job))
                .execute(
                    "SELECT * FROM jobs WHERE flow_id=%s AND kind IN ('initial','revision')"
                    " AND attempt_id=(SELECT current_attempt_id FROM flows WHERE id=jobs.flow_id)"
                    " AND (state='running' OR started_at IS NOT NULL OR result_received_at IS NOT NULL)"
                    " AND NOT (started_at IS NULL AND COALESCE("
                    " (jsonb_typeof(result_payload->'version')='number'"
                    " AND result_payload->>'version'='1' AND result_payload->>'source'='control')"
                    " OR (NOT (result_payload ? 'version')"
                    " AND jsonb_typeof(result_payload->'protocol_version')='number'"
                    " AND result_payload->>'protocol_version' IN ('14','15')"
                    " AND result_payload->>'connection_epoch'='control'),false))"
                    " ORDER BY id DESC LIMIT 1",
                    (flow_id,),
                )
                .fetchone()
            )

    def reopen_blocked(self, flow_id: int) -> bool:
        """A timeout cannot prove that an executor exited or a dispatched effect settled."""
        with self._db.connection() as connection:
            return connection.execute(
                "SELECT 1 FROM jobs WHERE flow_id=%s AND kind<>'inference'"
                " AND attempt_id=(SELECT current_attempt_id FROM flows WHERE id=jobs.flow_id) AND (state='running'"
                " OR (result_received_at IS NOT NULL AND result_applied_at IS NULL)"
                " OR (started_at IS NOT NULL AND result_received_at IS NULL)) LIMIT 1", (flow_id,),
            ).fetchone() is not None

    def has_pending_result(self, flow_id: int) -> bool:
        with self._db.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM jobs j WHERE flow_id=%s AND kind<>'inference' AND result_received_at IS NOT NULL"
                    " AND attempt_id=(SELECT current_attempt_id FROM flows WHERE id=j.flow_id)"
                    " AND result_applied_at IS NULL"
                    f" AND {RESOURCE_JOB_SQL} LIMIT 1",
                    (flow_id,),
                ).fetchone()
                is not None
            )

    def has_unsettled_jobs(self, flow_id: int, *, excluding_job_id: int) -> bool:
        """Check for another executor or unapplied result without loading job payloads."""
        with self._db.connection() as connection:
            return connection.execute(
                "SELECT 1 FROM jobs j WHERE flow_id=%s AND id<>%s AND kind<>'inference'"
                " AND attempt_id=(SELECT current_attempt_id FROM flows WHERE id=j.flow_id)"
                f" AND {RESOURCE_JOB_SQL}"
                " AND (state=%s OR (result_received_at IS NOT NULL"
                " AND result_applied_at IS NULL) OR EXISTS (SELECT 1 FROM workers w"
                " WHERE w.current_job_id=j.id)) LIMIT 1",
                (flow_id, excluding_job_id, JobState.RUNNING),
            ).fetchone() is not None

    def enqueue_job(
        self,
        flow: Flow,
        kind: JobKind,
        payload: dict | None = None,
        *,
        source_job_id: int | None = None,
        source_key: str | None = None,
    ) -> Job:
        """Queue with fresh Flow facts in the caller's workflow transaction.

        The caller locks the workflow before any job writes, or supplies a Flow
        created in the same transaction. This method never reacquires those locks.
        """
        if kind is JobKind.INFERENCE:
            raise ValueError("use enqueue_inference to freeze an inference call")
        if (source_job_id is None) != (source_key is None):
            raise ValueError("derived jobs require both source job and purpose")
        if self._db._bound_connection is None:
            raise ValueError("enqueue requires the current Flow in its workflow transaction")
        now = self._db.now()
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Job)) as cursor,
        ):
            row = cursor.execute(
                "INSERT INTO jobs(flow_id,attempt_id,kind,state,attempt,payload,created_at,"
                " updated_at,source_job_id,source_key) SELECT id,current_attempt_id,%s,%s,0,%s,%s,%s,%s,%s"
                " FROM flows WHERE id=%s AND current_attempt_id=%s AND (%s::bigint IS NULL OR EXISTS"
                " (SELECT 1 FROM jobs src WHERE src.id=%s AND src.attempt_id=flows.current_attempt_id))"
                " ON CONFLICT(source_job_id,source_key) WHERE source_job_id IS NOT NULL"
                " DO UPDATE SET source_key=excluded.source_key"
                " WHERE jobs.attempt_id=excluded.attempt_id AND jobs.kind=excluded.kind"
                " AND jobs.payload IS NOT DISTINCT FROM excluded.payload RETURNING *",
                (
                    kind,
                    JobState.QUEUED,
                    Jsonb(payload) if payload else None,
                    now,
                    now,
                    source_job_id,
                    source_key,
                    flow.id,
                    flow.required_attempt_id,
                    source_job_id,
                    source_job_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("job does not belong to the current Attempt or conflicts with its source")
            if kind is not JobKind.ARCHIVE:
                FlowStore(self._db).transition(flow, FlowEvent.JOB_ENQUEUED, now=now)
        return row

    def record_merge_notice(self, flow: Flow, delivery: FlowDelivery) -> Job:
        """Persist local notification intent in the merge transaction, never the dispatch queue."""
        if (delivery.attempt_id != flow.required_attempt_id or delivery.flow_id != flow.id or delivery.pr_number is None
                or delivery.merged_at is None or delivery.merge_commit_sha is None):
            raise ValueError("merge notice requires the current delivery's complete merge facts")
        job = self.enqueue_job(flow, JobKind.ARCHIVE)
        notice = MergeArchiveNotice(
            job_id=job.id, flow_id=flow.id, attempt_id=flow.required_attempt_id, repo=flow.repo,
            pr_number=delivery.pr_number, merged_at=delivery.merged_at,
            merge_commit_sha=delivery.merge_commit_sha,
        )
        inbox = ResultInbox(version=3, source="control", terminal=notice)
        with self._db.connection() as connection:
            connection.execute(
                "UPDATE jobs SET state='running',result_payload=%s,result_digest=%s,"
                " result_received_at=%s WHERE id=%s",
                (Jsonb(inbox.model_dump(mode="json")), merge_notice_digest(notice),
                 self._db.now(), job.id),
            )
        return required(self.get_job(job.id))

    def get_job(self, job_id: int) -> Job | None:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Job)) as cursor,
        ):
            return cursor.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()

    def retire_preparation(self, job: Job) -> None:
        """Retire undispatched work under the caller's workflow/job locks, without an inbox."""
        now = self._db.now()
        with self._db.connection() as connection:
            retired = connection.execute(
                "UPDATE jobs SET state='done',result_applied_at=%s,updated_at=%s"
                " WHERE id=%s AND kind<>'inference' AND state='running'"
                " AND started_at IS NULL AND dispatch_authorized_at IS NULL"
                " AND result_received_at IS NULL AND result_payload IS NULL",
                (now, now, job.id),
            )
            if retired.rowcount != 1:
                raise ValueError("cannot retire an executing preparation")
            connection.execute(
                "UPDATE workers w SET current_job_id=NULL FROM jobs j WHERE j.id=%s"
                " AND w.worker_id=j.assigned_worker_id AND w.workspace=j.workspace"
                " AND w.generation=j.generation AND w.current_job_id=j.id", (job.id,),
            )

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
                "SELECT * FROM (SELECT * FROM jobs WHERE flow_id=%s AND kind<>'inference'"
                " AND attempt_id=(SELECT current_attempt_id FROM flows WHERE id=jobs.flow_id) ORDER BY id DESC LIMIT 1)"
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
        """Commit an inbox item and clear only claims with durable writer-exit evidence."""

        if state not in (JobState.DONE, JobState.FAILED):
            raise ValueError("persisted results can only complete terminal jobs")
        now = self._db.now()
        candidate = self.get_job(job_id)
        if candidate is None or candidate.kind is JobKind.INFERENCE:
            return False
        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_workflow(candidate.required_flow_id)
            completed = connection.execute(
                "UPDATE jobs SET state=%s, result_applied_at=%s, updated_at=%s"
                " WHERE id=%s AND state=%s AND result_received_at IS NOT NULL"
                " AND kind<>'inference' AND result_applied_at IS NULL"
                " AND (assigned_worker_id IS NULL OR EXISTS (SELECT 1 FROM workers w"
                " WHERE w.worker_id=jobs.assigned_worker_id AND w.workspace=jobs.workspace"
                " AND w.generation=jobs.generation AND (w.current_job_id=jobs.id"
                " OR (w.current_job_id IS NULL AND EXISTS (SELECT 1 FROM execution_stops s"
                " WHERE s.job_id=jobs.id AND s.assignment_token=jobs.assignment_token"
                " AND s.state IN ('stopped','confirmed'))))))"
                " RETURNING assigned_worker_id",
                (state, now, now, job_id, JobState.RUNNING),
            ).fetchone()
            if completed is not None:
                # Unstarted control outcomes are preparation failures or explicit
                # request-validation refusals, neither of which has a worker journal.
                # A worker terminal proves exit; a started control failure does not.
                connection.execute(
                    "UPDATE workers w SET current_job_id=NULL FROM jobs j WHERE j.id=%s"
                    " AND w.worker_id=j.assigned_worker_id AND w.workspace=j.workspace"
                    " AND w.generation=j.generation AND w.current_job_id=j.id"
                    " AND (j.result_payload->>'source'='worker' OR (j.started_at IS NULL"
                    " AND j.result_payload->>'source'='control'))", (job_id,),
                )
                if completed["assigned_worker_id"] is not None:
                    connection.execute(
                        "UPDATE flow_executions SET recovery_deadline=CASE WHEN EXISTS"
                        " (SELECT 1 FROM workers WHERE repository IS NULL AND worker_id=%s AND status=%s) THEN NULL ELSE %s END"
                        " WHERE flow_id=(SELECT f.id FROM jobs j JOIN flows f ON f.current_attempt_id=j.attempt_id"
                        " WHERE j.id=%s AND j.generation=flow_executions.generation"
                        " AND j.workspace=flow_executions.workspace AND j.assigned_worker_id=flow_executions.worker_id)"
                        " AND released_at IS NULL",
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
        terminal: Failed,
        reason: str,
    ) -> None:
        """Record a control-generated terminal outcome while holding the workflow/job locks."""
        job = self.get_job(job_id)
        if (
            job is None
            or job.kind is JobKind.INFERENCE
            or terminal.job_id != job_id
            or terminal.assignment_token
            != (job.assignment_token if job.assignment_token is not None else "control")
        ):
            raise ValueError("control terminal does not match its assignment")
        inbox = ResultInbox(source="control", terminal=terminal)
        digest = terminal_digest(terminal)
        with self._db.connection() as connection:
            connection.execute(
                "UPDATE jobs SET result_payload=%s,result_digest=%s,result_received_at=%s,"
                " last_error=%s WHERE id=%s AND state=%s"
                " AND result_received_at IS NULL",
                (
                    Jsonb(inbox.model_dump(mode="json")),
                    digest,
                    self._db.now(),
                    reason,
                    job_id,
                    JobState.RUNNING,
                ),
            )

    def set_job_error(self, job_id: int, reason: str) -> None:
        with self._db.connection() as connection:
            connection.execute("UPDATE jobs SET last_error=%s WHERE id=%s", (reason, job_id))
