"""Fenced job assignments: claims, leases, preparation retries, and expiry recovery."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.contracts.protocol import Failed, InferenceResult, TerminalPayload, terminal_digest
from app.contracts.receipts import ResultInbox
from app.domain.flow_policy import FlowEvent
from app.domain.inference_models import InferenceFailure, InferenceTask
from app.domain.inference_policy import (
    MAX_INFERENCE_ATTEMPTS,
    choose_inference_worker,
    plan_inference_attempt,
)
from app.domain.models import DeliveryState, Flow, Job, JobKind, JobState, WorkerRecord
from app.domain.recovery_policy import (
    RECOVERY_BLOCKED,
    JobRecoveryFacts,
    RecoveryAction,
    decide_job_recovery,
    decide_preparation_retry,
)
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.inference import InferenceStore
from app.persistence.jobs import CURRENT_WORKSPACE_SQL, JobStore
from app.persistence.records import required
from app.persistence.repository import Repository
from app.persistence.results import RESOURCE_JOB_SQL
from app.persistence.reviews import ReviewStore

if TYPE_CHECKING:
    from app.github import GitHubError

_ASSIGNMENT_FLOW = (
    " EXISTS (SELECT 1 FROM flows current_flow WHERE current_flow.id=jobs.flow_id"
    " AND current_flow.current_round_no=jobs.round_no"
    " AND current_flow.state<>'archived')"
)

# Shared by demand and claims: demand publishes repositories, never job ownership.
# The state and delivery terms are the set form of Flow.accepts and
# FlowDelivery.supersedes (app/domain/models.py); change them together.
_ELIGIBLE_JOB = (
    " AND j.kind<>'inference' AND j.round_no=f.current_round_no"
    " AND ((j.kind='archive' AND f.state='finalizing') OR (j.kind<>'archive'"
    " AND f.state IN ('queued','running','review') AND f.archive_reason IS NULL))"
    " AND (j.prepare_next_at IS NULL OR j.prepare_next_at<=%s)"
    " AND NOT EXISTS (SELECT 1 FROM execution_stops s WHERE s.flow_id=f.id AND s.state<>'confirmed')"
    " AND NOT EXISTS (SELECT 1 FROM flow_deliveries d WHERE d.flow_id=f.id AND d.state='closed')"
    " AND ((j.kind='archive') = EXISTS (SELECT 1 FROM flow_deliveries d"
    " WHERE d.flow_id=f.id AND d.state='merged'))"
    " AND NOT EXISTS (SELECT 1 FROM jobs prior WHERE prior.flow_id=f.id AND prior.id<>j.id"
    f" AND {RESOURCE_JOB_SQL.replace('j.', 'prior.')}"
    " AND prior.kind<>'inference' AND (prior.state='running' OR (prior.result_received_at IS NOT NULL"
    " AND prior.result_applied_at IS NULL) OR EXISTS (SELECT 1 FROM workers held"
    " WHERE held.current_job_id=prior.id)))"
)



class AssignmentStore(Repository):
    """Ownership of a job by one worker slot, from claim to release.

    Every write is fenced by the assignment token, the worker's connection epoch and
    the slot's exact current claim. Locks are taken worker -> Flow -> job.
    """

    def workspace_demand(self, worker_id: str, epoch: str) -> list[str]:
        """Return unbound queued repositories with room to provision, without claiming jobs."""
        with self._db.connection() as conn:
            return [row["repo"] for row in conn.execute(
                "SELECT DISTINCT f.repo FROM jobs j JOIN flows f ON f.id=j.flow_id"
                " JOIN workers w ON w.worker_id=%s AND w.repository IS NULL"
                " WHERE w.connection_epoch=%s AND w.status IN ('online','busy') AND w.execute AND NOT w.draining"
                " AND j.state='queued' AND j.kind='initial'"
                " AND NOT EXISTS (SELECT 1 FROM flow_executions e WHERE e.flow_id=f.id AND e.released_at IS NULL)"
                " AND (SELECT count(*) FROM workers slot WHERE slot.worker_id=w.worker_id"
                " AND slot.repository=f.repo) < w.repo_workspace_limit"
                + _ELIGIBLE_JOB + " ORDER BY f.repo", (worker_id, epoch, self._db.now()),
            ).fetchall()]

    def assign_job(self, worker_id: str, token: str, connection_epoch: str = "", *,
                   slot: Literal["execute", "infer"] = "execute") -> Job | None:
        """Claim a concrete workspace, retaining Flow affinity and the single inference lane.

        Execute retains Flow affinity; infer compares ready capabilities without claiming
        a repository. Worker-first locks make local preference independent of poll order.
        """

        if slot not in {"execute", "infer"}:
            raise ValueError("unknown worker slot")
        now = self._db.now()
        with self._db.transaction() as tx:
            # A Flow may also own an inference lane on another worker. Fence all
            # owners in deterministic order before locking any Flow or job.
            tx.lock_workers()
            assignments = AssignmentStore(tx)
            if slot == "infer":
                return assignments._assign_inference(worker_id, token, connection_epoch)
            return assignments._assign_coding(worker_id, token, connection_epoch, now)

    def _assign_inference(self, worker_id: str, token: str, connection_epoch: str) -> Job | None:
        """Offer each due call to its preferred ready worker; claim when that is the caller."""
        with self._db.connection() as connection:
            available = connection.cursor(row_factory=class_row(WorkerRecord)).execute(
                "SELECT * FROM workers WHERE repository IS NULL AND status IN ('online','busy') AND NOT draining"
                " AND inventory_epoch=connection_epoch"
                " AND inference IS NOT NULL AND current_inference_job_id IS NULL"
                " ORDER BY last_inference_assigned_at NULLS FIRST,worker_id",
            ).fetchall()
            caller = next((w for w in available if w.worker_id == worker_id
                           and w.connection_epoch == connection_epoch), None)
            if caller is None:
                return None
            pending = connection.cursor(row_factory=class_row(Job)).execute(
                "SELECT * FROM jobs WHERE state='queued' AND kind='inference' ORDER BY created_at,id",
            ).fetchall()
        inference = InferenceStore(self._db)
        for job in pending:
            call = required(job.inference)
            if inference.inference_needs_cancel(job.id):
                continue
            attempt = plan_inference_attempt(call, request_id=uuid4().hex, now=self._db.now())
            if attempt is None:
                continue
            if isinstance(attempt, InferenceFailure):
                inference.settle_inference(job, attempt)
                continue
            chosen = choose_inference_worker(call, attempt, available)
            if chosen is not None and chosen.worker_id == worker_id:
                return self._claim_job(job.id, worker_id, token, connection_epoch, attempt)
        return None

    def _assign_coding(self, worker_id: str, token: str, connection_epoch: str, now: datetime) -> Job | None:
        """Claim the oldest eligible job for a workspace this worker holds or may bind."""
        with self._db.connection() as connection:
            selection = (
                "SELECT j.id,j.flow_id,f.repo,slot.workspace,slot.generation,bound.flow_id AS bound_flow"
                " FROM jobs j JOIN flows f ON f.id=j.flow_id"
                " JOIN workers owner ON owner.worker_id=%s AND owner.repository IS NULL"
                " JOIN workers slot ON slot.worker_id=owner.worker_id AND slot.repository=f.repo"
                " LEFT JOIN flow_executions bound ON bound.flow_id=f.id AND bound.released_at IS NULL"
                " LEFT JOIN flow_executions occupied ON occupied.worker_id=slot.worker_id"
                " AND occupied.workspace=slot.workspace AND occupied.released_at IS NULL"
                " WHERE owner.connection_epoch=%s AND owner.execute AND owner.status IN ('online','busy')"
                " AND slot.inventory_epoch=owner.connection_epoch AND slot.current_job_id IS NULL"
                " AND j.state='queued' AND ((bound.worker_id=owner.worker_id AND bound.workspace=slot.workspace"
                " AND bound.generation=slot.generation)"
                " OR (bound.flow_id IS NULL AND occupied.flow_id IS NULL AND j.kind='initial'"
                " AND NOT owner.draining"
                " AND (SELECT count(*) FROM flow_executions active JOIN flows owned ON owned.id=active.flow_id"
                " WHERE active.worker_id=owner.worker_id AND active.released_at IS NULL"
                " AND owned.repo=f.repo)<owner.repo_workspace_limit))" + _ELIGIBLE_JOB
            )
            coding_candidates = connection.execute(
                selection + " ORDER BY j.created_at,j.id,slot.workspace", (worker_id, connection_epoch, now),
            ).fetchall()
            for candidate in coding_candidates:
                # Lock Flow explicitly before job, and re-evaluate after any competing claim.
                flow = connection.cursor(row_factory=class_row(Flow)).execute(
                    "SELECT * FROM flows WHERE id=%s FOR NO KEY UPDATE SKIP LOCKED",
                    (candidate["flow_id"],),
                ).fetchone()
                if flow is None:
                    continue
                row = connection.execute(
                    selection + " AND j.id=%s AND slot.workspace=%s",
                    (worker_id, connection_epoch, now, candidate["id"], candidate["workspace"]),
                ).fetchone()
                if row is None:
                    continue
                self._db.lock_job(row["id"])
                if row["bound_flow"] is None:
                    bound = connection.execute(
                        "INSERT INTO flow_executions(flow_id,worker_id,workspace,generation,repo_identity,bound_at)"
                        " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(flow_id) DO UPDATE SET"
                        " worker_id=EXCLUDED.worker_id,workspace=EXCLUDED.workspace,generation=EXCLUDED.generation,"
                        " repo_identity=EXCLUDED.repo_identity,bound_at=EXCLUDED.bound_at,"
                        " recovery_deadline=NULL,released_at=NULL WHERE flow_executions.released_at IS NOT NULL"
                        " RETURNING flow_id",
                        (row["flow_id"], worker_id, row["workspace"], row["generation"],
                         f"{worker_id}:{row['repo']}", now),
                    ).fetchone()
                    if bound is None:
                        continue
                return self._claim_job(row["id"], worker_id, token, connection_epoch, None, flow=flow)
            return None

    def _claim_job(self, job_id: int, worker_id: str, token: str, epoch: str,
                   current_task: InferenceTask | None, *, flow: Flow | None = None) -> Job | None:
        """Commit ownership and the authorized-attempt count under the allocator's locks."""
        now = self._db.now()
        candidate = required(JobStore(self._db).get_job(job_id))
        if candidate.flow_id is not None and flow is None:
            flow = self._db.lock_workflow(candidate.flow_id)
        job = required(self._db.lock_job(job_id))
        inference = job.inference
        with self._db.connection() as connection:
            if inference is not None:
                if inference.deadline_at <= now:
                    InferenceStore(self._db).settle_inference(job, InferenceFailure(code="execution_timeout"))
                    return None
                assert current_task is not None and job.attempt < MAX_INFERENCE_ATTEMPTS
                worker = connection.cursor(row_factory=class_row(WorkerRecord)).execute(
                    "SELECT * FROM workers WHERE worker_id=%s AND repository IS NULL", (worker_id,),
                ).fetchone()
                assert worker is not None and worker.inference is not None
                inference = inference.model_copy(update={
                    "current_task": current_task,
                    "assignment_epoch": epoch, "backend": worker.inference.adapter_version,
                    "next_attempt_at": None,
                })
                workspace, generation = worker.workspace, worker.generation
            else:
                binding = required(FlowStore(self._db).get_flow_execution(job.required_flow_id))
                workspace, generation = binding.workspace, binding.generation
            assigned = connection.cursor(row_factory=class_row(Job)).execute(
                "UPDATE jobs SET state='running',assigned_worker_id=%s,assignment_token=%s,"
                " workspace=%s,generation=%s,"
                " started_at=NULL,dispatch_authorized_at=%s,last_error=NULL,attempt=attempt+1,"
                " heartbeat_at=%s,lease_expires_at=%s,recovery_deadline=NULL,result_payload=NULL,"
                " result_digest=NULL,result_received_at=NULL,result_applied_at=NULL,updated_at=%s,inference=%s"
                " WHERE id=%s AND state='queued' RETURNING *",
                (worker_id, token, workspace, generation, now if inference else None, now,
                 inference.deadline_at if inference else now + self._db.lease_duration, now,
                 Jsonb(inference.model_dump(mode="json")) if inference else None, job.id),
            ).fetchone()
            if inference is not None:
                claimed = connection.execute(
                    "UPDATE workers SET current_inference_job_id=%s,last_inference_assigned_at=%s,"
                    " last_seen_at=%s WHERE worker_id=%s AND repository IS NULL"
                    " AND workspace=%s AND generation=%s AND current_inference_job_id IS NULL",
                    (job.id, now, now, worker_id, workspace, generation),
                )
            else:
                claimed = connection.execute(
                    "UPDATE workers SET current_job_id=%s,last_assigned_at=%s"
                    " WHERE worker_id=%s AND workspace=%s AND generation=%s AND current_job_id IS NULL",
                    (job.id, now, worker_id, workspace, generation),
                )
                connection.execute(
                    "UPDATE workers SET last_assigned_at=%s WHERE worker_id=%s AND repository IS NULL",
                    (now, worker_id),
                )
                FlowStore(self._db).transition(required(flow), FlowEvent.JOB_CLAIMED, now=now)
            if claimed.rowcount != 1:
                raise ValueError("assignment lost its locked workspace generation")
            return required(assigned)

    def start_assignment(
        self,
        job_id: int,
        attempt_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str = "",
    ) -> bool:
        now = self._db.now()
        candidate = JobStore(self._db).get_job(job_id)
        if candidate is None or candidate.kind is JobKind.INFERENCE:
            return False
        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_workflow(candidate.required_flow_id)
            job = connection.execute(
                "SELECT flow_id,kind,workspace,generation FROM jobs WHERE id=%s AND state=%s"
                " AND kind<>'inference' AND assigned_worker_id=%s AND assignment_token=%s"
                " AND started_at IS NULL AND EXISTS (SELECT 1 FROM workers"
                " WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s) AND "
                + CURRENT_WORKSPACE_SQL + " AND " + _ASSIGNMENT_FLOW + " FOR UPDATE",
                (job_id, JobState.RUNNING, worker_id, token, worker_id, connection_epoch),
            ).fetchone()
            if job is None:
                return False
            binding = connection.execute(
                "SELECT 1 FROM flow_executions e JOIN attempts a ON a.flow_id=e.flow_id"
                " WHERE e.flow_id=%s AND e.worker_id=%s AND e.released_at IS NULL"
                " AND a.id=%s AND e.workspace=%s AND e.generation=%s",
                (job["flow_id"], worker_id, attempt_id, job["workspace"], job["generation"]),
            ).fetchone()
            if binding is None:
                return False
            started = connection.execute(
                "UPDATE jobs SET started_at=%s, heartbeat_at=%s, lease_expires_at=%s,"
                " updated_at=%s"
                " WHERE id=%s AND state=%s AND assigned_worker_id=%s"
                " AND assignment_token=%s AND started_at IS NULL",
                (
                    now,
                    now,
                    now + self._db.lease_duration,
                    now,
                    job_id,
                    JobState.RUNNING,
                    worker_id,
                    token,
                ),
            )
            if started.rowcount == 1 and job["kind"] == JobKind.REVISION:
                # Count in the same transaction as Started: a control crash before feedback
                # relay bookkeeping must not leave a real invocation outside the revision cap.
                connection.execute(
                    "UPDATE attempts SET revision_cycles=revision_cycles+1,updated_at=%s WHERE id=%s",
                    (now, attempt_id),
                )
            return started.rowcount == 1

    def heartbeat_assignment(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str = "",
    ) -> bool:
        """Refresh liveness for either slot; inference's absolute budget never renews."""
        now = self._db.now()
        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_worker(worker_id)
            result = connection.execute(
                "UPDATE jobs SET heartbeat_at=%s,"
                " lease_expires_at=CASE WHEN kind='inference' THEN lease_expires_at ELSE %s END,updated_at=%s"
                " WHERE id=%s AND state=%s AND assigned_worker_id=%s"
                " AND assignment_token=%s"
                " AND ((kind='inference' AND inference->>'assignment_epoch'=%s) OR"
                " (kind<>'inference' AND (started_at IS NOT NULL OR (lease_expires_at>%s AND EXISTS"
                " (SELECT 1 FROM flows f WHERE f.id=jobs.flow_id AND f.state<>'archived')))))"
                " AND EXISTS (SELECT 1 FROM workers"
                " WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s) AND "
                + CURRENT_WORKSPACE_SQL,
                (
                    now,
                    now + self._db.lease_duration,
                    now,
                    job_id,
                    JobState.RUNNING,
                    worker_id,
                    token,
                    connection_epoch,
                    now,
                    worker_id,
                    connection_epoch,
                ),
            )
            if result.rowcount == 1:
                connection.execute(
                    "UPDATE workers SET last_seen_at=%s WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s",
                    (now, worker_id, connection_epoch),
                )
        return result.rowcount == 1

    def resume_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str
    ) -> bool:
        """Resume started work, including a frozen archive whose Started message was lost."""

        now = self._db.now()
        candidate = JobStore(self._db).get_job(job_id)
        if candidate is None or candidate.kind is JobKind.INFERENCE:
            return False
        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_workflow(candidate.required_flow_id)
            # Recover a lost Started only against the complete frozen ArchiveTask identity.
            resumed = connection.execute(
                "UPDATE jobs SET recovery_deadline=NULL, heartbeat_at=%s,"
                " lease_expires_at=%s, updated_at=%s, started_at=COALESCE(started_at,%s)"
                " WHERE id=%s AND state=%s AND kind<>'inference'"
                " AND " + _ASSIGNMENT_FLOW
                + " AND (started_at IS NOT NULL OR (kind='archive' AND EXISTS"
                " (SELECT 1 FROM flows f JOIN flow_deliveries d ON d.flow_id=f.id"
                " JOIN flow_executions e ON e.flow_id=f.id WHERE f.id=jobs.flow_id"
                " AND f.state IN ('finalizing','blocked') AND d.state='merged' AND e.released_at IS NULL"
                " AND e.worker_id=jobs.assigned_worker_id AND jobs.payload->'archive'=jsonb_build_object("
                " 'kind','archive','flow_id',f.id,'attempt_id',f.current_attempt_id,'repo',f.repo,"
                " 'worker_id',e.worker_id,'repo_identity',e.repo_identity,'delivery_version',d.version))))"
                " AND assigned_worker_id=%s"
                " AND assignment_token=%s AND (recovery_deadline IS NULL"
                " OR recovery_deadline>%s OR EXISTS (SELECT 1 FROM flow_deliveries d"
                " JOIN flows f ON f.id=d.flow_id JOIN flow_executions e ON e.flow_id=f.id"
                " WHERE f.id=jobs.flow_id AND f.state<>'archived'"
                " AND e.released_at IS NULL AND e.worker_id=jobs.assigned_worker_id))"
                " AND EXISTS (SELECT 1 FROM workers"
                " WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s) AND "
                + CURRENT_WORKSPACE_SQL,
                (
                    now,
                    now + self._db.lease_duration,
                    now,
                    now,
                    job_id,
                    JobState.RUNNING,
                    worker_id,
                    token,
                    now,
                    worker_id,
                    connection_epoch,
                ),
            )
            if resumed.rowcount == 1:
                connection.execute(
                    "UPDATE workers SET last_seen_at=%s"
                    " WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s",
                    (now, worker_id, connection_epoch),
                )
        return resumed.rowcount == 1

    def assignment_active(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str = "",
    ) -> bool:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM jobs WHERE id=%s AND state=%s AND assigned_worker_id=%s"
                " AND (kind<>'inference' OR inference->>'assignment_epoch'=%s)"
                " AND assignment_token=%s AND EXISTS (SELECT 1 FROM workers"
                " WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s) AND "
                + CURRENT_WORKSPACE_SQL,
                (job_id, JobState.RUNNING, worker_id, connection_epoch, token, worker_id, connection_epoch),
            ).fetchone()
        return row is not None

    def record_assignment_result(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str,
        terminal: TerminalPayload | InferenceResult,
    ) -> str:
        """Persist a fenced business outcome before domain-side result application."""

        if terminal.job_id != job_id or terminal.assignment_token != token:
            if isinstance(terminal, InferenceResult):
                return "rejected"
            raise ValueError("terminal does not match its assignment")
        if isinstance(terminal, InferenceResult):
            payload = terminal.model_dump(mode="json")
            digest = hashlib.sha256(terminal.model_dump_json().encode()).hexdigest()
        else:
            inbox = ResultInbox(source="worker", terminal=terminal)
            payload = inbox.model_dump(mode="json")
            digest = terminal_digest(terminal)
        now = self._db.now()
        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_worker(worker_id)
            row = connection.cursor(row_factory=class_row(Job)).execute(
                "SELECT * FROM jobs WHERE id=%s"
                " AND assigned_worker_id=%s AND assignment_token=%s"
                " AND EXISTS (SELECT 1 FROM workers WHERE worker_id=%s"
                " AND repository IS NULL AND connection_epoch=%s) FOR UPDATE",
                (job_id, worker_id, token, worker_id, connection_epoch),
            ).fetchone()
            if row is None:
                return "rejected"
            if isinstance(terminal, InferenceResult):
                if (row.state is not JobState.RUNNING or row.inference is None
                        or row.inference.assignment_epoch != connection_epoch
                        or row.inference.current_task is None
                        or terminal.reply_to != row.inference.current_task.request_id):
                    return "rejected"
            elif row.kind is JobKind.INFERENCE:
                return "rejected"
            if row.result_digest is not None:
                if row.result_digest != digest:
                    return "conflict"
                return "applied" if row.result_applied_at is not None else "pending"
            if not self.assignment_active(job_id, worker_id, token, connection_epoch):
                return "rejected"
            stored = connection.execute(
                "UPDATE jobs SET result_payload=%s, result_digest=%s,"
                " result_received_at=%s, updated_at=%s"
                " WHERE id=%s AND state=%s",
                (Jsonb(payload), digest, now, now, job_id, JobState.RUNNING),
            )
            return "pending" if stored.rowcount == 1 else "rejected"

    def finish_assignment(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        state: JobState,
        connection_epoch: str = "",
    ) -> bool:
        if state not in (JobState.DONE, JobState.FAILED):
            raise ValueError("remote assignments can only finish as done or failed")
        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_worker(worker_id)
            result = connection.execute(
                "UPDATE jobs SET state=%s, result_applied_at=COALESCE(result_applied_at,%s),"
                " updated_at=%s"
                " WHERE id=%s AND state=%s AND assigned_worker_id=%s"
                " AND kind<>'inference'"
                " AND assignment_token=%s AND EXISTS (SELECT 1 FROM workers"
                " WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s) AND "
                + CURRENT_WORKSPACE_SQL,
                (
                    state,
                    self._db.now(),
                    self._db.now(),
                    job_id,
                    JobState.RUNNING,
                    worker_id,
                    token,
                    worker_id,
                    connection_epoch,
                ),
            )
            if result.rowcount == 1:
                connection.execute(
                    "UPDATE workers w SET current_job_id=NULL FROM jobs j WHERE j.id=%s"
                    " AND w.worker_id=j.assigned_worker_id AND w.workspace=j.workspace"
                    " AND w.generation=j.generation AND w.current_job_id=j.id", (job_id,),
                )
        return result.rowcount == 1

    def _lock_preparation(self, job_id: int, worker: str, token: str, epoch: str) -> Job | None:
        job = JobStore(self._db).get_job(job_id)
        if job is None or job.kind is JobKind.INFERENCE:
            return None
        self._db.lock_workflow(job.required_flow_id)
        current = self._db.lock_job(job_id)
        if (current is None or current.started_at is not None or current.result_received_at is not None
                or current.lease_expires_at is None or current.lease_expires_at <= self._db.now()
                or not self.assignment_active(job_id, worker, token, epoch)):
            return None
        return current

    def reject_assignment(self, job_id: int, worker: str, token: str, epoch: str) -> bool:
        """Persist a control failure only for a current, dispatched but unstarted assignment.

        A peer's error supplies correlation, not a terminal result or stop proof. Lock in
        the same worker/workflow/job order as start, cancel, reconnect and result writers.
        Result application alone may commit this outcome and release its slot afterwards.
        """
        with self._db.transaction() as tx:
            current = AssignmentStore(tx)._lock_preparation(job_id, worker, token, epoch)
            if (
                current is None
                or current.result_payload is not None
                or current.result_digest is not None
                or current.result_applied_at is not None
            ):
                return False
            flows = FlowStore(tx)
            flow = flows.get_flow(current.required_flow_id)
            binding = flows.get_flow_execution(current.required_flow_id)
            delivery = DeliveryStore(tx).get_delivery(current.required_flow_id)
            if (
                flow is None
                or not flow.accepts(current.kind)
                or flow.current_round_no != current.round_no
                or binding is None
                or binding.worker_id != worker
                or binding.released_at is not None
                or (
                    delivery is not None
                    and (
                        delivery.state is DeliveryState.CLOSED or delivery.supersedes(current.kind)
                    )
                )
            ):
                return False
            with tx.connection() as connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM workers WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s"
                        " AND EXISTS (SELECT 1 FROM workers slot WHERE slot.worker_id=workers.worker_id"
                        " AND slot.current_job_id=%s) AND EXISTS (SELECT 1 FROM jobs WHERE id=%s"
                        " AND dispatch_authorized_at>=%s)"
                        " AND NOT EXISTS (SELECT 1 FROM execution_stops"
                        " WHERE flow_id=%s AND state!='confirmed')",
                        (worker, epoch, job_id, job_id, binding.bound_at, current.flow_id),
                    ).fetchone()
                    is None
                ):
                    return False
            reason = "assignment payload failed validation"
            JobStore(tx).record_control_result(
                job_id,
                Failed(job_id=job_id, assignment_token=token, error=reason),
                reason,
            )
            return True

    def clear_preparation(self, job_id: int, worker: str, token: str, epoch: str) -> bool:
        """Clear the read budget only for the same successful, un-dispatched preparation."""
        with self._db.transaction() as tx:
            current = AssignmentStore(tx)._lock_preparation(job_id, worker, token, epoch)
            if current is None:
                return False
            with tx.connection() as connection:
                connection.execute(
                    "UPDATE jobs SET prepare_failures=0,prepare_next_at=NULL,prepare_first_failed_at=NULL,"
                    " prepare_last_error=NULL WHERE id=%s", (job_id,),
                )
            return True

    def defer_preparation(
        self, job_id: int, worker: str, token: str, epoch: str, error: GitHubError | None,
        *, max_failures: int, window_seconds: float,
    ) -> JobState | None:
        """Atomically retry or fail a read; None rejects stale assignment evidence.

        A missing error means the persisted window expired before issuing another request.
        The caller has not emitted Assignment and must complete the ordinary result path on failure.
        """
        with self._db.transaction() as tx:
            current = AssignmentStore(tx)._lock_preparation(job_id, worker, token, epoch)
            if current is None:
                return None
            flow = FlowStore(tx).get_flow(current.required_flow_id)
            delivery = DeliveryStore(tx).get_delivery(current.required_flow_id)
            if (
                flow is None
                or not flow.accepts(current.kind)
                or flow.current_round_no != current.round_no
                or (delivery is not None and delivery.supersedes(current.kind))
            ):
                return JobState.DONE
            retry = decide_preparation_retry(
                current, error, now=tx.now(), max_failures=max_failures, window_seconds=window_seconds)
            with tx.connection() as connection:
                connection.execute(
                    "UPDATE jobs SET prepare_failures=%s,prepare_first_failed_at=%s,prepare_next_at=%s,"
                    " prepare_last_error=%s,last_error=%s WHERE id=%s",
                    (retry.failures, retry.first_failed_at, retry.next_at, retry.cause, retry.reason, job_id),
                )
            if retry.next_at is None:
                terminal = Failed(job_id=job_id, assignment_token=token, error=retry.reason)
                JobStore(tx).record_control_result(job_id, terminal, retry.reason)
                return JobState.FAILED
            if not AssignmentStore(tx).requeue_assignment(job_id, worker, token, epoch):
                raise ValueError("locked preparation could not be requeued")
            return JobState.QUEUED

    def requeue_assignment(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str = "",
    ) -> bool:
        candidate = JobStore(self._db).get_job(job_id)
        if candidate is None:
            return False
        with (
            FlowStore(self._db).locked(candidate.required_flow_id) as context,
            context.database.connection() as connection,
        ):
            tx, flow = context.database, context.flow
            result = connection.execute(
                "UPDATE jobs SET state=%s, assigned_worker_id=NULL, assignment_token=NULL,"
                " workspace=NULL,generation=NULL,"
                " heartbeat_at=NULL, updated_at=%s WHERE id=%s AND state=%s"
                " AND assigned_worker_id=%s AND assignment_token=%s AND started_at IS NULL"
                " AND result_received_at IS NULL AND dispatch_authorized_at IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM execution_stops s WHERE s.job_id=jobs.id AND s.state!='confirmed')"
                " AND EXISTS (SELECT 1 FROM workers WHERE worker_id=%s"
                " AND repository IS NULL AND connection_epoch=%s) AND " + CURRENT_WORKSPACE_SQL,
                (
                    JobState.QUEUED,
                    self._db.now(),
                    candidate.id,
                    JobState.RUNNING,
                    worker_id,
                    token,
                    worker_id,
                    connection_epoch,
                ),
            )
            if result.rowcount == 1:
                if flow is not None and flow.current_round_no == candidate.round_no:
                    FlowStore(tx).transition(flow, FlowEvent.ASSIGNMENT_REQUEUED)
                connection.execute(
                    "UPDATE workers SET current_job_id=NULL WHERE worker_id=%s AND workspace=%s"
                    " AND generation=%s AND current_job_id=%s",
                    (worker_id, candidate.workspace, candidate.generation, candidate.id),
                )
        return result.rowcount == 1

    def retire_queued_work(self, flow_id: int, *, reason: str = "Flow cancelled") -> None:
        """Retire a Flow's pending review, queued or never-dispatched jobs, and inference calls.

        Work that may have started is left alone: ending it requires stop proof.
        """
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
            InferenceStore(tx).cancel_flow_inference(flow_id)

    def sweep_expired_assignments(self) -> tuple[int, int]:
        """Apply pure recovery decisions under the existing worker-first transaction fence."""

        now = self._db.now()
        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_workers()
            # Worker -> Flow -> Job, also for recovery. Never wait for a Flow
            # after locking its job while a result writer holds the opposite order.
            locked_flows = connection.cursor(row_factory=class_row(Flow)).execute(
                "SELECT * FROM flows WHERE id IN (SELECT flow_id FROM jobs WHERE state IN ('queued','running')"
                " UNION SELECT flow_id FROM flow_executions WHERE released_at IS NULL) ORDER BY id FOR NO KEY UPDATE"
            ).fetchall()
            flows = {flow.id: flow for flow in locked_flows}
            inference_requeued, inference_failed = InferenceStore(tx).sweep_inference()
            # Assignment/result/stop writers acquire these worker locks too. Keep
            # facts and updates in this transaction, and lock selected jobs before
            # deciding so a received result cannot be overwritten by a stale read.
            candidates = connection.cursor(row_factory=class_row(JobRecoveryFacts)).execute(
                "SELECT j.id,j.flow_id,j.kind,j.lease_expires_at,j.recovery_deadline,"
                " j.started_at,j.dispatch_authorized_at,j.result_received_at,"
                " (j.payload->'archive' IS NOT NULL) AS archive_frozen,"
                " f.state AS flow_state,d.state AS delivery_state,"
                " EXISTS (SELECT 1 FROM execution_stops s WHERE s.flow_id=j.flow_id"
                " AND s.state!='confirmed') AS has_pending_stop"
                " FROM jobs j JOIN flows f ON f.id=j.flow_id"
                " LEFT JOIN flow_deliveries d ON d.flow_id=j.flow_id"
                " WHERE j.kind<>'inference' AND j.state='running' AND (j.lease_expires_at<=%s OR j.recovery_deadline<=%s)"
                " ORDER BY j.id FOR UPDATE OF j",
                (now, now),
            ).fetchall()
            actions: dict[RecoveryAction, list[int]] = {action: [] for action in RecoveryAction}
            flow_actions: dict[RecoveryAction, set[int]] = {action: set() for action in RecoveryAction}
            for candidate in candidates:
                action = decide_job_recovery(candidate, now)
                actions[action].append(candidate.id)
                flow_actions[action].add(candidate.flow_id)
            requeued_rows = connection.execute(
                "UPDATE jobs SET state=%s, assigned_worker_id=NULL, assignment_token=NULL,"
                " workspace=NULL,generation=NULL,"
                " heartbeat_at=NULL, lease_expires_at=NULL, updated_at=%s"
                " WHERE id=ANY(%s) AND state=%s"
                " AND result_received_at IS NULL"
                " RETURNING id",
                (JobState.QUEUED, now, actions[RecoveryAction.REQUEUE], JobState.RUNNING),
            ).fetchall()
            for flow_id in sorted(flow_actions[RecoveryAction.REQUEUE]):
                FlowStore(tx).transition(flows[flow_id], FlowEvent.ASSIGNMENT_REQUEUED, now=now)
            connection.execute(
                "UPDATE jobs SET last_error=%s WHERE id=ANY(%s) AND state='running'"
                " AND result_received_at IS NULL",
                (RECOVERY_BLOCKED, actions[RecoveryAction.BLOCKED]),
            )
            for flow_id in sorted(flow_actions[RecoveryAction.BLOCKED]):
                FlowStore(tx).transition(flows[flow_id], FlowEvent.EXECUTION_UNCERTAIN, now=now)
            requeued_ids = [row["id"] for row in requeued_rows]
            if requeued_ids:
                connection.execute(
                    "UPDATE workers SET current_job_id=NULL"
                    " WHERE current_job_id=ANY(%s)",
                    (requeued_ids,),
                )
            # A recovery timeout says nothing about whether a writer has stopped.
            # Keep every binding until explicit settlement and release; never clear
            # sibling workspace claims when one Flow's recovery window expires.
        return len(requeued_rows) + inference_requeued, inference_failed
