"""Durable inference calls: enqueue, cancellation, result settlement, and loss recovery."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.contracts.protocol import InferenceResult
from app.domain.inference_models import InferenceFailure, InferenceJob, InferenceTask
from app.domain.inference_policy import decide_inference_cancellation, decide_inference_settlement
from app.domain.models import Job, JobKind, JobState
from app.persistence.flows import FlowStore
from app.persistence.inference_notices import InferenceNoticeStore
from app.persistence.jobs import CURRENT_WORKSPACE_SQL, WRITABLE_JOB_SQL, JobStore
from app.persistence.records import required
from app.persistence.repository import Repository
from app.persistence.reviews import ReviewStore


class InferenceStore(Repository):
    """One logical inference call from enqueue to settlement.

    Calls are jobs of kind ``inference``. Claiming one is the assignment store's job;
    everything after the claim, and every way a call ends, is decided here with
    ``app.domain.inference_policy``.
    """

    def enqueue_inference(
        self, task: InferenceTask, *, flow_id: int | None = None,
        batch_id: int | None = None, validation: Literal["text", "review", "flow_naming"] = "text",
    ) -> Job:
        """Freeze a logical call once; a batch's consumed recoverable failure allows another call.

        Polling/restarts never replenish the existing job's attempts or deadline. The
        batch link is independent of its revision job, and is serialized with workflow
        currentness and the batch row before any new call is created. Review jobs are
        reusable only for their frozen context. A generic request ID may only replay the
        same frozen input, validation contract and attribution.
        """
        if batch_id is not None and flow_id is None:
            raise ValueError("review inference requires its source Flow")
        now = self._db.now()
        with self._db.transaction() as tx, tx.connection() as connection:
            flow = tx.lock_workflow(flow_id) if flow_id is not None else None
            if flow_id is not None and flow is None:
                raise ValueError("inference source Flow does not exist")
            if flow is not None and flow.history_only:
                raise ValueError("history-only Flow cannot enqueue inference")
            attempt_id = flow.required_attempt_id if flow else None
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
                    if (existing.flow_id != flow_id or existing.attempt_id != attempt_id
                            or existing.inference is None or existing.inference.batch_id is not None):
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
                source_attempt_id=attempt_id,
                context_digest=batch.context.digest if batch and batch.context else None,
            )
            job = (
                connection.cursor(row_factory=class_row(Job))
                .execute(
                    "INSERT INTO jobs(flow_id,attempt_id,kind,state,inference,created_at,updated_at)"
                    " VALUES (%s,%s,'inference','queued',%s,%s,%s) RETURNING *",
                    (
                        flow_id,
                        attempt_id,
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

    def _lock_inference(self, job_id: int) -> Job | None:
        """Use the same worker -> Flow -> job order, with no fabricated generic Flow."""
        job = JobStore(self._db).get_job(job_id)
        if job is None or job.kind is not JobKind.INFERENCE:
            return None
        if job.flow_id is not None:
            self._db.lock_workflow(job.flow_id)
        elif job.assigned_worker_id is not None:
            self._db.lock_worker(job.assigned_worker_id)
        current = self._db.lock_job(job_id)
        # A queued generic call can be claimed between the initial read and worker
        # lock. Never acquire its new worker after the job lock; let the caller retry.
        if current is not None and (current.assigned_worker_id, current.assignment_token) != (
            job.assigned_worker_id, job.assignment_token,
        ):
            return None
        return current

    def _cancellation(self, job: Job, now: datetime) -> InferenceFailure | None:
        """Read the source facts of a locked call and decide whether it must stop."""
        call = required(job.inference)
        flow = FlowStore(self._db).get_flow(job.flow_id) if job.flow_id is not None else None
        batch, batch_current = None, False
        if call.batch_id is not None:
            reviews = ReviewStore(self._db)
            batch = reviews.get_batch(call.batch_id)
            batch_current = reviews.current(batch)
        return decide_inference_cancellation(
            job, flow=flow, batch=batch, batch_current=batch_current, now=now)

    def inference_needs_cancel(self, job_id: int) -> bool:
        """Retire expired queued work, or persist a running cancellation until cleanup.

        True means this call must not dispatch/continue. A running slot is deliberately
        retained until its matching cleanup result or connection-loss recovery arrives.
        """
        with self._db.transaction() as tx, tx.connection() as connection:
            inference = InferenceStore(tx)
            job = inference._lock_inference(job_id)
            if job is None or job.state not in {JobState.QUEUED, JobState.RUNNING}:
                return False
            failure = inference._cancellation(job, tx.now())
            if failure is None:
                return False
            assert job.inference is not None
            job.inference = job.inference.model_copy(update={"cancel_failure": failure})
            connection.execute("UPDATE jobs SET inference=%s WHERE id=%s",
                               (Jsonb(job.inference.model_dump(mode="json")), job.id))
            if job.state is JobState.QUEUED:
                inference.settle_inference(job, failure)
            return True

    def cancel_flow_inference(self, flow_id: int) -> None:
        """Mark the Flow's active calls cancelled under the caller's workflow lock.

        Queued calls end at once; a running call keeps its slot until cleanup arrives.
        """
        with self._db.connection() as connection:
            cancelled = connection.execute(
                "UPDATE jobs SET inference=jsonb_set(inference,'{cancel_failure}',%s)"
                " WHERE flow_id=%s AND kind='inference' AND state IN ('queued','running') RETURNING id",
                (Jsonb(InferenceFailure(code="cancelled").model_dump(mode="json")), flow_id),
            ).fetchall()
        for job in cancelled:
            self.inference_needs_cancel(job["id"])

    def complete_inference_result(self, job_id: int, *, failure: InferenceFailure | None = None) -> bool:
        """Apply caller validation to a durable result after worker cleanup, never before it.

        The original InferenceResult JSON is retained for restart/replay. Validation
        failures override worker success; cancellation overrides either outcome. Retry
        budgeting, backoff, summary and batch notice publication share this transaction.
        """
        with self._db.transaction() as tx:
            inference = InferenceStore(tx)
            job = inference._lock_inference(job_id)
            if (job is None or job.state is not JobState.RUNNING or job.result_payload is None
                    or job.result_received_at is None or job.result_applied_at is not None):
                return False
            with tx.connection() as conn:
                if conn.execute(
                    "SELECT 1 FROM jobs WHERE id=%s AND " + CURRENT_WORKSPACE_SQL, (job.id,),
                ).fetchone() is None:
                    return False
            result = InferenceResult.model_validate(job.result_payload)
            assert job.inference is not None
            current_task = job.inference.current_task
            if (result.job_id != job.id or result.assignment_token != job.assignment_token
                    or current_task is None or result.reply_to != current_task.request_id
                    # Same immutable-result hash as record_assignment_result, independent of JSONB key order.
                    or hashlib.sha256(result.model_dump_json().encode()).hexdigest() != job.result_digest
                    or (result.output is not None and result.output.adapter_version != job.inference.backend)):
                failure = InferenceFailure(code="invalid_response")
            inference.settle_inference(job, failure or result.failure)
            return True

    def settle_inference(self, job: Job, failure: InferenceFailure | None) -> None:
        """Transition a cleaned-up (or disconnected) call the caller already locked, without coding effects."""
        if job.state is JobState.RUNNING:
            with self._db.connection() as connection:
                if connection.execute(
                    "SELECT 1 FROM jobs WHERE id=%s AND " + CURRENT_WORKSPACE_SQL, (job.id,),
                ).fetchone() is None:
                    raise ValueError("inference settlement lost its workspace generation")
        now = self._db.now()
        settled = decide_inference_settlement(
            job, failure, cancellation=self._cancellation(job, now), now=now)
        state, call, failure = settled.state, settled.call, settled.failure
        with self._db.connection() as connection:
            connection.execute(
                "UPDATE jobs SET state=%s,inference=%s,last_error=%s,updated_at=%s,"
                " lease_expires_at=NULL,recovery_deadline=NULL,"
                " result_applied_at=CASE WHEN result_received_at IS NOT NULL THEN %s ELSE NULL END WHERE id=%s",
                (state, Jsonb(call.model_dump(mode="json")), failure.code if failure else None, now, now, job.id),
            )
            connection.execute(
                "UPDATE workers SET current_inference_job_id=NULL WHERE worker_id=%s"
                " AND repository IS NULL AND workspace=%s AND generation=%s AND current_inference_job_id=%s",
                (job.assigned_worker_id, job.workspace, job.generation, job.id),
            )
        if call.summary is not None and call.batch_id is not None and job.flow_id is not None:
            InferenceNoticeStore(self._db).append(job.flow_id, call.batch_id, call.summary)
        if job.flow_id is not None:
            if state in (JobState.DONE, JobState.FAILED):
                self._db.job_finished(job.flow_id, job.id, job.kind, state)
            flow = FlowStore(self._db).get_flow(job.flow_id)
            if flow is not None and job.attempt_id == flow.current_attempt_id:
                FlowStore(self._db).try_archive(job.flow_id)

    def recover_inference(self, worker_id: str | None = None) -> None:
        """Classify unknown authorized attempts; a durable inbox always wins over recovery.

        Callers already hold the ordered worker locks (startup, disconnect or replacement).
        No execution is resumed: each later claim gets a new request and retains its budget.
        """
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE kind='inference' AND state='running' AND result_received_at IS NULL"
                f" AND {WRITABLE_JOB_SQL}"
                " AND (%s::text IS NULL OR assigned_worker_id=%s) ORDER BY id", (worker_id, worker_id),
            ).fetchall()
            for row in rows:
                job = self._lock_inference(row["id"])
                assert job is not None
                self.settle_inference(job, InferenceFailure(code="connection_closed"))

    def sweep_inference(self) -> tuple[int, int]:
        """Settle attempts whose session is gone and retire calls that must stop.

        Returns (requeued, failed). The caller holds every worker lock and the related
        Flow locks; a durable unapplied answer is left to result application.
        """
        requeued = failed = 0
        with self._db.connection() as connection:
            pending = connection.execute(
                f"SELECT id FROM jobs WHERE kind='inference' AND state IN ('queued','running') AND {WRITABLE_JOB_SQL} ORDER BY id",
            ).fetchall()
            for item in pending:
                job = self._lock_inference(item["id"])
                assert job is not None and job.inference is not None
                if job.result_received_at is not None and job.result_applied_at is None:
                    continue
                if job.state is JobState.RUNNING and connection.execute(
                    "SELECT 1 FROM workers WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s"
                    " AND status IN ('online','busy')",
                    (job.assigned_worker_id, job.inference.assignment_epoch),
                ).fetchone() is None:
                    self.settle_inference(job, InferenceFailure(code="connection_closed"))
                else:
                    self.inference_needs_cancel(job.id)
                updated = required(JobStore(self._db).get_job(job.id))
                failed += updated.state is JobState.FAILED
                requeued += updated.state is JobState.QUEUED and job.state is JobState.RUNNING
        return requeued, failed
