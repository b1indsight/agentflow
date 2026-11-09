"""Durable jobs, lifecycle checks, and accepted-result application."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal
from uuid import uuid4

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.contracts.protocol import ArchiveTask, Failed, terminal_digest
from app.contracts.receipts import ResultInbox
from app.domain.inference_models import InferenceFailure, InferenceJob, InferenceTask
from app.domain.models import (
    Job,
    JobKind,
    JobState,
    WorkerStatus,
)
from app.persistence.records import required
from app.persistence.repository import Repository


class JobStore(Repository):
    """Durable jobs, lifecycle checks, and accepted-result application."""

    def cancel_jobs(self, flow_id: int, *, reason: str = "Flow cancelled") -> None:
        """Retire queued/never-dispatched preparation; actual execution requires stop proof."""
        # Like FlowStore.update_flow, cancellation reuses reviews without an import cycle.
        from app.persistence.reviews import ReviewStore
        from app.persistence.workers import WorkerStore

        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_workflow(flow_id)
            ReviewStore(tx).cancel(flow_id)
            connection.execute(
                "WITH retired AS (UPDATE jobs SET state=%s, last_error=%s, updated_at=%s WHERE flow_id=%s"
                " AND kind<>'inference'"
                " AND (state=%s OR (state='running' AND started_at IS NULL AND dispatch_authorized_at IS NULL"
                " AND EXISTS (SELECT 1 FROM flows f WHERE f.id=jobs.flow_id AND f.archive_reason IS NOT NULL)))"
                " AND result_received_at IS NULL RETURNING id)"
                " UPDATE workers SET current_job_id=NULL"
                " WHERE current_job_id IN (SELECT id FROM retired)",
                (
                    JobState.FAILED,
                    reason,
                    self._db.now(),
                    flow_id,
                    JobState.QUEUED,
                ),
            )
            cancelled = connection.execute(
                "UPDATE jobs SET inference=jsonb_set(inference,'{cancel_failure}',%s)"
                " WHERE flow_id=%s AND kind='inference' AND state IN ('queued','running') RETURNING id",
                (Jsonb(InferenceFailure(code="cancelled").model_dump(mode="json")), flow_id),
            ).fetchall()
            for job in cancelled:
                WorkerStore(tx).inference_needs_cancel(job["id"])

    def lifecycle_job(self, flow_id: int) -> Job | None:
        """Latest coding execution, excluding jobs cancelled before they could run."""
        with self._db.connection() as connection:
            return (
                connection.cursor(row_factory=class_row(Job))
                .execute(
                    "SELECT * FROM jobs WHERE flow_id=%s AND kind IN ('initial','revision')"
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
                "SELECT 1 FROM jobs WHERE flow_id=%s AND kind<>'inference' AND (state='running'"
                " OR (result_received_at IS NOT NULL AND result_applied_at IS NULL)"
                " OR (started_at IS NOT NULL AND result_received_at IS NULL)) LIMIT 1", (flow_id,),
            ).fetchone() is not None

    def has_pending_result(self, flow_id: int) -> bool:
        with self._db.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM jobs WHERE flow_id=%s AND kind<>'inference' AND result_received_at IS NOT NULL"
                    " AND result_applied_at IS NULL LIMIT 1",
                    (flow_id,),
                ).fetchone()
                is not None
            )

    def has_unsettled_jobs(self, flow_id: int, *, excluding_job_id: int) -> bool:
        """Check for another executor or unapplied result without loading job payloads."""
        with self._db.connection() as connection:
            return connection.execute(
                "SELECT 1 FROM jobs WHERE flow_id=%s AND id<>%s AND kind<>'inference'"
                " AND (state=%s OR (result_received_at IS NOT NULL"
                " AND result_applied_at IS NULL) OR EXISTS (SELECT 1 FROM workers w"
                " WHERE w.current_job_id=jobs.id)) LIMIT 1",
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
        if kind is JobKind.INFERENCE:
            raise ValueError("use enqueue_inference to freeze an inference call")
        if (source_job_id is None) != (source_key is None):
            raise ValueError("derived jobs require both source job and purpose")
        now = self._db.now()
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Job)) as cursor,
        ):
            row = cursor.execute(
                "INSERT INTO jobs(flow_id,round_no,kind,state,attempt,payload,created_at,"
                " updated_at,source_job_id,source_key) VALUES (%s,(SELECT current_round_no FROM flows WHERE id=%s),%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT(source_job_id,source_key) WHERE source_job_id IS NOT NULL"
                " DO UPDATE SET source_key=excluded.source_key"
                " WHERE jobs.flow_id=excluded.flow_id AND jobs.kind=excluded.kind"
                " AND jobs.payload IS NOT DISTINCT FROM excluded.payload RETURNING *",
                (
                    flow_id,
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
            if kind is not JobKind.ARCHIVE:
                connection.execute(
                    "UPDATE flows SET state='queued',updated_at=%s WHERE id=%s AND state='review'"
                    " AND archive_reason IS NULL",
                    (now, flow_id),
                )
        return required(row)

    def enqueue_inference(
        self, task: InferenceTask, *, flow_id: int | None = None,
        batch_id: int | None = None, validation: Literal["text", "review"] = "text",
    ) -> Job:
        """Freeze a logical call once; a batch's consumed recoverable failure starts a new round.

        Polling/restarts never replenish the existing job's attempts or deadline. The
        batch link is independent of its revision job, and is serialized with workflow
        currentness and the batch row before any new round is created. Review jobs are
        reusable only for their frozen context. A generic request ID may only replay the
        same frozen input, validation contract and attribution.
        """
        from app.persistence.reviews import ReviewStore

        if batch_id is not None and flow_id is None:
            raise ValueError("review inference requires its source Flow")
        now = self._db.now()
        with self._db.transaction() as tx, tx.connection() as connection:
            flow = tx.lock_workflow(flow_id) if flow_id is not None else None
            previous = None
            batch = None
            if batch_id is not None:
                connection.execute("SELECT id FROM review_batches WHERE id=%s FOR UPDATE", (batch_id,))
                batch = ReviewStore(tx).get_batch(batch_id)
                if batch.flow_id != flow_id:
                    raise ValueError("batch belongs to another Flow")
                if batch.inference_job_id is not None:
                    previous = JobStore(tx).get_job(batch.inference_job_id)
                    assert previous is not None and previous.inference is not None
                    summary = previous.inference.summary
                    repeatable = summary is not None and (
                        InferenceFailure(code=summary.last_failure_code).recoverable
                        or summary.last_failure_code == "unavailable"
                        or (summary.attempts == 0 and summary.stop_reason == "deadline")
                    )
                    same_context = previous.inference.context_digest == (
                        batch.context.digest if batch.context else None
                    )
                    if same_context and (previous.state is not JobState.FAILED or not previous.inference.consumed
                            or not repeatable or (previous.inference.cancel_failure is not None
                                                  and previous.inference.cancel_failure.code == "cancelled")
                            or not ReviewStore(tx).current(batch)):
                        return previous
                if not ReviewStore(tx).current(batch) or batch.decision is not None:
                    raise ValueError("review batch is no longer awaiting inference")
            # Generic idempotency survives consumption too; callers choose a new logical ID.
            if batch is None:
                connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                                   (f"inference:{task.request_id}",))
                existing = connection.cursor(row_factory=class_row(Job)).execute(
                    "SELECT * FROM jobs WHERE kind='inference' AND inference->'task'->>'request_id'=%s",
                    (task.request_id,),
                ).fetchone()
                if existing is not None:
                    if existing.flow_id != flow_id or existing.inference is None or existing.inference.batch_id is not None:
                        raise ValueError("logical inference request belongs to another caller")
                    if existing.inference.task != task or existing.inference.validation != validation:
                        raise ValueError("logical inference request does not match its frozen call")
                    return existing
            frozen = InferenceTask.model_validate_json(task.model_dump_json())
            if previous is not None:
                frozen = frozen.model_copy(update={"request_id": uuid4().hex})
            inference = InferenceJob(
                task=frozen, deadline_at=now + timedelta(seconds=frozen.timeout_seconds),
                validation=validation, batch_id=batch_id,
                source_attempt_id=flow.current_attempt_id if flow else None,
                context_digest=batch.context.digest if batch and batch.context else None,
            )
            job = (
                connection.cursor(row_factory=class_row(Job))
                .execute(
                    "INSERT INTO jobs(flow_id,round_no,kind,state,inference,created_at,updated_at)"
                    " VALUES (%s,%s,'inference','queued',%s,%s,%s) RETURNING *",
                    (
                        flow_id,
                        flow.current_round_no if flow else 1,
                        Jsonb(inference.model_dump(mode="json")),
                        now,
                        now,
                    ),
                )
                .fetchone()
            )
            assert job is not None
            if batch is not None:
                connection.execute("UPDATE review_batches SET inference_job_id=%s WHERE id=%s", (job.id, batch.id))
            return job

    def consume_inference(self, job_id: int) -> None:
        """Record that a caller observed a failed call, without creating another job."""
        with self._db.connection() as connection:
            connection.execute(
                "UPDATE jobs SET inference=jsonb_set(inference,'{consumed}','true'::jsonb)"
                " WHERE id=%s AND kind='inference' AND state='failed'", (job_id,),
            )

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
                "SELECT * FROM (SELECT * FROM jobs WHERE flow_id=%s AND kind<>'inference' ORDER BY id DESC LIMIT 1)"
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
        """Commit an inbox item, retaining remote ownership until the worker ACK witness."""

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
                " AND w.generation=jobs.generation AND w.current_job_id=jobs.id))"
                " RETURNING assigned_worker_id",
                (state, now, now, job_id, JobState.RUNNING),
            ).fetchone()
            if completed is not None:
                # Unstarted control outcomes are preparation failures or explicit
                # request-validation refusals, neither of which has a worker journal.
                # Started control failures and all remote results still require ACK.
                connection.execute(
                    "UPDATE workers w SET current_job_id=NULL FROM jobs j WHERE j.id=%s"
                    " AND w.worker_id=j.assigned_worker_id AND w.workspace=j.workspace"
                    " AND w.generation=j.generation AND w.current_job_id=j.id"
                    " AND j.started_at IS NULL"
                    " AND j.result_payload->>'source'='control'", (job_id,),
                )
                connection.execute(
                    "UPDATE flow_executions SET recovery_deadline=CASE WHEN EXISTS"
                    " (SELECT 1 FROM workers WHERE repository IS NULL AND worker_id=%s AND status=%s) THEN NULL ELSE %s END"
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
        digest = terminal_digest(inbox.terminal)
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
