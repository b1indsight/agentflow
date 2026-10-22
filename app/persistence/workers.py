"""Worker slots, fenced assignments, preparation, and lease recovery."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.contracts.protocol import Failed, terminal_digest
from app.domain.models import (
    FlowExecution,
    FlowState,
    Job,
    JobKind,
    JobState,
    WorkerRecord,
    WorkerStatus,
)
from app.domain.recovery_policy import (
    RECOVERY_BLOCKED,
    RECOVERY_EXPIRED,
    BindingRecoveryFacts,
    JobRecoveryFacts,
    RecoveryAction,
    decide_job_recovery,
    should_release_binding,
)
from app.github import GitHubError, retry_delay
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.records import required
from app.persistence.repository import Repository

# A frozen archive may have started deleting before its Started frame reached serve.
_MAY_HAVE_EXECUTED = (
    "(started_at IS NOT NULL OR dispatch_authorized_at IS NOT NULL OR (kind='archive' AND payload->'archive' IS NOT NULL))"
)


class WorkerStore(Repository):
    """Worker slots, fenced assignments, preparation, and lease recovery."""

    def get_flow_execution(self, flow_id: int) -> FlowExecution | None:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(FlowExecution)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM flow_executions WHERE flow_id=%s", (flow_id,)
            ).fetchone()

    def get_worker_execution(self, worker_id: str) -> FlowExecution | None:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(FlowExecution)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM flow_executions WHERE worker_id=%s AND released_at IS NULL",
                (worker_id,),
            ).fetchone()

    def release_flow_execution(self, flow_id: int) -> bool:
        now = self._db.now()
        with self._db.connection() as connection:
            result = connection.execute(
                "UPDATE flow_executions SET released_at=%s"
                " WHERE flow_id=%s AND released_at IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM execution_stops s WHERE s.flow_id=flow_executions.flow_id"
                " AND s.binding_at=flow_executions.bound_at AND s.state!='confirmed')"
                " AND NOT EXISTS (SELECT 1 FROM jobs j JOIN flows f ON f.id=j.flow_id"
                " WHERE j.flow_id=flow_executions.flow_id AND f.state='cancelled'"
                " AND j.result_received_at IS NOT NULL AND j.result_applied_at IS NULL)"
                " AND NOT EXISTS (SELECT 1 FROM result_effects e JOIN jobs j ON j.id=e.job_id"
                " JOIN flows f ON f.id=j.flow_id WHERE j.flow_id=flow_executions.flow_id"
                " AND f.state='cancelled' AND e.dispatched_at IS NOT NULL AND e.receipt IS NULL)",
                (now, flow_id),
            )
        return result.rowcount == 1

    def worker_connected(
        self,
        worker_id: str,
        protocol_version: int,
        connection_epoch: str = "",
    ) -> WorkerRecord:
        now = self._db.now()
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(WorkerRecord)) as cursor,
        ):
            row = cursor.execute(
                "INSERT INTO workers(worker_id, protocol_version, status, connection_epoch,"
                " current_job_id, first_connected_at, last_connected_at, last_seen_at,"
                " last_error, last_disconnect_reason)"
                " VALUES (%s,%s,%s,%s,NULL,%s,%s,%s,NULL,NULL)"
                " ON CONFLICT(worker_id) DO UPDATE SET protocol_version=excluded.protocol_version,"
                " status=excluded.status, connection_epoch=excluded.connection_epoch,"
                " last_connected_at=excluded.last_connected_at,"
                " last_seen_at=excluded.last_seen_at, last_error=NULL RETURNING *",
                (
                    worker_id,
                    protocol_version,
                    WorkerStatus.ONLINE,
                    connection_epoch,
                    now,
                    now,
                    now,
                ),
            ).fetchone()
            connection.execute(
                "UPDATE flow_executions SET recovery_deadline=NULL"
                " WHERE worker_id=%s AND released_at IS NULL",
                (worker_id,),
            )
        return required(row)

    def worker_disconnected(
        self,
        worker_id: str,
        reason: str | None = None,
        connection_epoch: str = "",
    ) -> None:
        now = self._db.now()
        with self._db.connection() as connection, connection.transaction():
            self._db._lock_worker(connection, worker_id)
            connection.execute(
                "UPDATE flow_executions SET recovery_deadline=COALESCE(recovery_deadline,%s)"
                " WHERE worker_id=%s AND released_at IS NULL"
                " AND EXISTS (SELECT 1 FROM workers WHERE worker_id=%s"
                " AND connection_epoch=%s)",
                (now + self._db.recovery_grace, worker_id, worker_id, connection_epoch),
            )
            connection.execute(
                "UPDATE jobs SET recovery_deadline=COALESCE(recovery_deadline,%s), updated_at=%s"
                " WHERE state=%s AND " + _MAY_HAVE_EXECUTED + " AND assigned_worker_id=%s"
                " AND EXISTS (SELECT 1 FROM workers WHERE worker_id=%s"
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
                " last_seen_at=%s, last_error=%s, last_disconnect_reason=%s"
                " WHERE worker_id=%s AND connection_epoch=%s",
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

    def list_workers(self) -> list[WorkerRecord]:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(WorkerRecord)) as cursor,
        ):
            return cursor.execute("SELECT * FROM workers ORDER BY worker_id").fetchall()

    def assign_job(
        self,
        worker_id: str,
        token: str,
        connection_epoch: str = "",
    ) -> Job | None:
        """Claim work while retaining a Flow's worker slot between individual jobs."""

        now = self._db.now()
        # Share this predicate across fair-worker selection and both binding paths.
        eligible = (
            " AND (j.prepare_next_at IS NULL OR j.prepare_next_at<=%s)"
            " AND NOT EXISTS (SELECT 1 FROM flow_deliveries paused"
            " WHERE paused.flow_id=j.flow_id AND paused.state='closed')"
            " AND ((j.kind='archive') = EXISTS (SELECT 1 FROM flow_deliveries d"
            " WHERE d.flow_id=j.flow_id AND d.state='merged'))"
            " AND (j.kind<>'archive' OR EXISTS (SELECT 1 FROM flow_executions e"
            " WHERE e.flow_id=j.flow_id AND e.released_at IS NULL))"
            " AND (j.kind<>'archive' OR NOT EXISTS (SELECT 1 FROM jobs prior"
            " WHERE prior.flow_id=j.flow_id AND prior.id<>j.id"
            " AND (prior.state='running' OR (prior.result_received_at IS NOT NULL"
            " AND prior.result_applied_at IS NULL))))"
        )
        with self._db.connection() as connection, connection.transaction():
            candidate = connection.execute(
                "SELECT w.worker_id FROM workers w WHERE w.status=%s"
                " AND w.current_job_id IS NULL AND EXISTS (SELECT 1 FROM jobs j"
                " JOIN flows f ON f.id=j.flow_id"
                " LEFT JOIN flow_executions own ON own.worker_id=w.worker_id"
                " AND own.released_at IS NULL"
                " LEFT JOIN flow_executions bound ON bound.flow_id=f.id"
                " AND bound.released_at IS NULL WHERE j.state=%s"
                " AND f.state=%s AND ((own.flow_id=j.flow_id) OR"
                " (own.flow_id IS NULL AND bound.flow_id IS NULL AND NOT w.draining))" + eligible + ")"
                " ORDER BY w.last_assigned_at NULLS FIRST, w.worker_id LIMIT 1",
                (WorkerStatus.ONLINE, JobState.QUEUED, FlowState.ACTIVE, now),
            ).fetchone()
            if candidate is None or candidate["worker_id"] != worker_id:
                return None
            # A noncandidate must not lock another worker: concurrent callers can
            # otherwise skip each other's candidates and both return without work.
            # Selection is advisory; lock and recheck this caller before any writes.
            if (
                connection.execute(
                    "SELECT 1 FROM workers WHERE worker_id=%s AND connection_epoch=%s"
                    " AND status=%s AND current_job_id IS NULL"
                    " AND (NOT draining OR EXISTS (SELECT 1 FROM flow_executions e"
                    " WHERE e.worker_id=workers.worker_id AND e.released_at IS NULL))"
                    " FOR UPDATE OF workers SKIP LOCKED",
                    (worker_id, connection_epoch, WorkerStatus.ONLINE),
                ).fetchone()
                is None
            ):
                return None
            execution = connection.execute(
                "SELECT flow_id FROM flow_executions WHERE worker_id=%s AND released_at IS NULL",
                (worker_id,),
            ).fetchone()
            if execution is None:
                row = connection.execute(
                    "SELECT j.id, j.flow_id, f.repo FROM jobs j JOIN flows f ON f.id=j.flow_id"
                    " LEFT JOIN flow_executions e ON e.flow_id=f.id AND e.released_at IS NULL"
                    " WHERE j.state=%s AND f.state=%s AND e.flow_id IS NULL" + eligible +
                    " ORDER BY j.created_at, j.id FOR NO KEY UPDATE OF f,j SKIP LOCKED LIMIT 1",
                    (JobState.QUEUED, FlowState.ACTIVE, now),
                ).fetchone()
                if row is None:
                    return None
                bound = connection.execute(
                    "INSERT INTO flow_executions(flow_id, worker_id, repo_identity, bound_at)"
                    " VALUES (%s,%s,%s,%s) ON CONFLICT(flow_id) DO UPDATE SET"
                    " worker_id=EXCLUDED.worker_id, repo_identity=EXCLUDED.repo_identity,"
                    " bound_at=EXCLUDED.bound_at, recovery_deadline=NULL, released_at=NULL"
                    " WHERE flow_executions.released_at IS NOT NULL RETURNING flow_id",
                    (row["flow_id"], worker_id, f"{worker_id}:{row['repo']}", now),
                ).fetchone()
                if bound is None:
                    return None
            else:
                row = connection.execute(
                    "SELECT j.id, j.flow_id, f.repo FROM jobs j JOIN flows f ON f.id=j.flow_id"
                    " WHERE j.state=%s AND f.state=%s AND j.flow_id=%s" + eligible +
                    " ORDER BY j.created_at, j.id FOR NO KEY UPDATE OF f,j SKIP LOCKED LIMIT 1",
                    (JobState.QUEUED, FlowState.ACTIVE, execution["flow_id"], now),
                ).fetchone()
                if row is None:
                    return None
            with connection.cursor(row_factory=class_row(Job)) as cursor:
                assigned = cursor.execute(
                    "UPDATE jobs SET state=%s, assigned_worker_id=%s,"
                    " assignment_token=%s, started_at=NULL, last_error=NULL,"
                    " attempt=attempt+1, heartbeat_at=%s, lease_expires_at=%s,"
                    " recovery_deadline=NULL, result_payload=NULL, result_digest=NULL,"
                    " result_received_at=NULL, result_applied_at=NULL, updated_at=%s"
                    " WHERE id=%s RETURNING *",
                    (
                        JobState.RUNNING,
                        worker_id,
                        token,
                        now,
                        now + self._db.lease_duration,
                        now,
                        row["id"],
                    ),
                ).fetchone()
            connection.execute(
                "UPDATE workers SET status=%s, current_job_id=%s, last_assigned_at=%s,"
                " last_seen_at=%s WHERE worker_id=%s",
                (WorkerStatus.BUSY, row["id"], now, now, worker_id),
            )
            return assigned

    def start_assignment(
        self,
        job_id: int,
        attempt_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str = "",
    ) -> bool:
        now = self._db.now()
        with self._db.connection() as connection, connection.transaction():
            self._db._lock_worker(connection, worker_id)
            job = connection.execute(
                "SELECT flow_id,kind FROM jobs WHERE id=%s AND state=%s"
                " AND assigned_worker_id=%s AND assignment_token=%s"
                " AND started_at IS NULL AND EXISTS (SELECT 1 FROM workers"
                " WHERE worker_id=%s AND connection_epoch=%s) FOR UPDATE",
                (job_id, JobState.RUNNING, worker_id, token, worker_id, connection_epoch),
            ).fetchone()
            if job is None:
                return False
            binding = connection.execute(
                "SELECT 1 FROM flow_executions e JOIN attempts a ON a.flow_id=e.flow_id"
                " WHERE e.flow_id=%s AND e.worker_id=%s AND e.released_at IS NULL"
                " AND a.id=%s",
                (job["flow_id"], worker_id, attempt_id),
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
        now = self._db.now()
        with self._db.connection() as connection:
            self._db._lock_worker(connection, worker_id)
            result = connection.execute(
                "UPDATE jobs SET heartbeat_at=%s, lease_expires_at=%s, updated_at=%s"
                " WHERE id=%s AND state=%s AND assigned_worker_id=%s"
                " AND assignment_token=%s"
                " AND (started_at IS NOT NULL OR (lease_expires_at>%s AND EXISTS"
                " (SELECT 1 FROM flows f WHERE f.id=jobs.flow_id AND f.state='active')))"
                " AND EXISTS (SELECT 1 FROM workers"
                " WHERE worker_id=%s AND connection_epoch=%s)",
                (
                    now,
                    now + self._db.lease_duration,
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
            if result.rowcount == 1:
                connection.execute(
                    "UPDATE workers SET last_seen_at=%s WHERE worker_id=%s AND connection_epoch=%s",
                    (now, worker_id, connection_epoch),
                )
        return result.rowcount == 1

    def resume_assignment(
        self, job_id: int, worker_id: str, token: str, connection_epoch: str
    ) -> bool:
        """Resume started work, including a frozen archive whose Started frame was lost."""

        now = self._db.now()
        with self._db.connection() as connection, connection.transaction():
            self._db._lock_worker(connection, worker_id)
            # Recover a lost Started only against the complete frozen ArchiveTask identity.
            resumed = connection.execute(
                "UPDATE jobs SET recovery_deadline=NULL, heartbeat_at=%s,"
                " lease_expires_at=%s, updated_at=%s, started_at=COALESCE(started_at,%s)"
                " WHERE id=%s AND state=%s"
                " AND (started_at IS NOT NULL OR (kind='archive' AND EXISTS"
                " (SELECT 1 FROM flows f JOIN flow_deliveries d ON d.flow_id=f.id"
                " JOIN flow_executions e ON e.flow_id=f.id WHERE f.id=jobs.flow_id"
                " AND f.state='active' AND d.state='merged' AND e.released_at IS NULL"
                " AND e.worker_id=jobs.assigned_worker_id AND jobs.payload->'archive'=jsonb_build_object("
                " 'kind','archive','flow_id',f.id,'attempt_id',f.current_attempt_id,'repo',f.repo,"
                " 'worker_id',e.worker_id,'repo_identity',e.repo_identity,'delivery_version',d.version))))"
                " AND assigned_worker_id=%s"
                " AND assignment_token=%s AND (recovery_deadline IS NULL"
                " OR recovery_deadline>%s OR EXISTS (SELECT 1 FROM flow_deliveries d"
                " JOIN flows f ON f.id=d.flow_id JOIN flow_executions e ON e.flow_id=f.id"
                " WHERE f.id=jobs.flow_id AND f.state='active' AND d.state IN ('closed','merged')"
                " AND e.released_at IS NULL AND e.worker_id=jobs.assigned_worker_id))"
                " AND EXISTS (SELECT 1 FROM workers"
                " WHERE worker_id=%s AND connection_epoch=%s)",
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
                    "UPDATE workers SET status=%s, current_job_id=%s, last_seen_at=%s"
                    " WHERE worker_id=%s AND connection_epoch=%s",
                    (WorkerStatus.BUSY, job_id, now, worker_id, connection_epoch),
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
                " AND assignment_token=%s AND EXISTS (SELECT 1 FROM workers"
                " WHERE worker_id=%s AND connection_epoch=%s)",
                (job_id, JobState.RUNNING, worker_id, token, worker_id, connection_epoch),
            ).fetchone()
        return row is not None

    def record_assignment_result(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str,
        payload: Mapping[str, object],
        digest: str,
    ) -> str:
        """Persist a fenced terminal message before domain-side result application."""

        now = self._db.now()
        with self._db.connection() as connection, connection.transaction():
            self._db._lock_worker(connection, worker_id)
            row = connection.execute(
                "SELECT result_digest, result_applied_at FROM jobs WHERE id=%s"
                " AND assigned_worker_id=%s AND assignment_token=%s"
                " AND EXISTS (SELECT 1 FROM workers WHERE worker_id=%s"
                " AND connection_epoch=%s) FOR UPDATE",
                (job_id, worker_id, token, worker_id, connection_epoch),
            ).fetchone()
            if row is None:
                return "rejected"
            if row["result_digest"] is not None:
                if row["result_digest"] != digest:
                    return "conflict"
                return "applied" if row["result_applied_at"] is not None else "pending"
            stored = connection.execute(
                "UPDATE jobs SET result_payload=%s, result_digest=%s,"
                " result_received_at=%s, updated_at=%s"
                " WHERE id=%s AND state=%s",
                (Jsonb(dict(payload)), digest, now, now, job_id, JobState.RUNNING),
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
        with self._db.connection() as connection:
            self._db._lock_worker(connection, worker_id)
            result = connection.execute(
                "UPDATE jobs SET state=%s, result_applied_at=COALESCE(result_applied_at,%s),"
                " updated_at=%s"
                " WHERE id=%s AND state=%s AND assigned_worker_id=%s"
                " AND assignment_token=%s AND EXISTS (SELECT 1 FROM workers"
                " WHERE worker_id=%s AND connection_epoch=%s)",
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
                    "UPDATE workers SET status=%s, current_job_id=NULL, last_seen_at=%s"
                    " WHERE worker_id=%s AND current_job_id=%s",
                    (WorkerStatus.ONLINE, self._db.now(), worker_id, job_id),
                )
        return result.rowcount == 1

    def _lock_preparation(self, job_id: int, worker: str, token: str, epoch: str) -> Job | None:
        job = JobStore(self._db).get_job(job_id)
        if job is None:
            return None
        self._db.lock_workflow(job.flow_id)
        current = self._db.lock_job(job_id)
        if (current is None or current.started_at is not None or current.result_received_at is not None
                or current.lease_expires_at is None or current.lease_expires_at <= self._db.now()
                or not self.assignment_active(job_id, worker, token, epoch)):
            return None
        return current

    def clear_preparation(self, job_id: int, worker: str, token: str, epoch: str) -> bool:
        """Clear the read budget only for the same successful, un-dispatched preparation."""
        with self._db.transaction() as tx:
            current = WorkerStore(tx)._lock_preparation(job_id, worker, token, epoch)
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
            current = WorkerStore(tx)._lock_preparation(job_id, worker, token, epoch)
            if current is None:
                return None
            flow, delivery = FlowStore(tx).get_flow(current.flow_id), DeliveryStore(tx).get_delivery(current.flow_id)
            if (flow is None or flow.state is not FlowState.ACTIVE
                    or (delivery is not None and delivery.supersedes(current.kind))):
                return JobState.DONE
            now = tx.now()
            failures = current.prepare_failures + (error is not None)
            first = current.prepare_first_failed_at or now
            next_at = now + timedelta(seconds=retry_delay(failures, now, error))
            permanent = error is not None and not error.retryable
            exhausted = (permanent or error is None or failures >= max_failures
                         or next_at >= first + timedelta(seconds=window_seconds))
            cause = str(error) if error else current.prepare_last_error or "preparation window expired"
            reason = (f"preparation retry budget/window exhausted: {cause}"
                      if exhausted and not permanent else cause)
            with tx.connection() as connection:
                connection.execute(
                    "UPDATE jobs SET prepare_failures=%s,prepare_first_failed_at=%s,prepare_next_at=%s,"
                    " prepare_last_error=%s,last_error=%s WHERE id=%s",
                    (failures, first, None if exhausted else next_at, cause, reason, job_id),
                )
            if exhausted:
                message = Failed(job_id=job_id, assignment_token=token, connection_epoch="control", error=reason)
                JobStore(tx).record_control_result(job_id, message.model_dump(mode="json"), terminal_digest(message), reason)
                return JobState.FAILED
            if not WorkerStore(tx).requeue_assignment(job_id, worker, token, epoch):
                raise ValueError("locked preparation could not be requeued")
            return JobState.QUEUED

    def requeue_assignment(
        self,
        job_id: int,
        worker_id: str,
        token: str,
        connection_epoch: str = "",
    ) -> bool:
        with self._db.connection() as connection:
            self._db._lock_worker(connection, worker_id)
            result = connection.execute(
                "UPDATE jobs SET state=%s, assigned_worker_id=NULL, assignment_token=NULL,"
                " heartbeat_at=NULL, updated_at=%s WHERE id=%s AND state=%s"
                " AND assigned_worker_id=%s AND assignment_token=%s AND started_at IS NULL"
                " AND result_received_at IS NULL AND dispatch_authorized_at IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM execution_stops s WHERE s.job_id=jobs.id AND s.state!='confirmed')"
                " AND EXISTS (SELECT 1 FROM workers WHERE worker_id=%s"
                " AND connection_epoch=%s)",
                (
                    JobState.QUEUED,
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
                    "UPDATE workers SET status=%s, current_job_id=NULL, last_seen_at=%s"
                    " WHERE worker_id=%s AND current_job_id=%s",
                    (WorkerStatus.ONLINE, self._db.now(), worker_id, job_id),
                )
        return result.rowcount == 1

    def recover_abandoned_assignments(self) -> None:
        """Mark persisted work recoverable without replaying or failing it at startup."""

        now = self._db.now()
        with self._db.connection() as connection:
            connection.execute(
                "UPDATE workers SET status=%s",
                (WorkerStatus.OFFLINE,),
            )
            connection.execute(
                "UPDATE jobs SET recovery_deadline=COALESCE(recovery_deadline,%s),"
                " updated_at=%s WHERE state=%s AND " + _MAY_HAVE_EXECUTED,
                (now + self._db.recovery_grace, now, JobState.RUNNING),
            )
            connection.execute(
                "UPDATE flow_executions SET recovery_deadline=COALESCE(recovery_deadline,%s)"
                " WHERE released_at IS NULL",
                (now + self._db.recovery_grace,),
            )

    def sweep_expired_assignments(self) -> tuple[int, int]:
        """Apply pure recovery decisions under the existing worker-first transaction fence."""

        now = self._db.now()
        with self._db.connection() as connection, connection.transaction():
            connection.execute(
                "SELECT worker_id FROM workers ORDER BY worker_id FOR UPDATE"
            ).fetchall()
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
                " WHERE j.state='running' AND (j.lease_expires_at<=%s OR j.recovery_deadline<=%s)"
                " ORDER BY j.id FOR UPDATE OF j",
                (now, now),
            ).fetchall()
            actions: dict[RecoveryAction, list[int]] = {action: [] for action in RecoveryAction}
            for candidate in candidates:
                actions[decide_job_recovery(candidate, now)].append(candidate.id)
            requeued_rows = connection.execute(
                "UPDATE jobs SET state=%s, assigned_worker_id=NULL, assignment_token=NULL,"
                " heartbeat_at=NULL, lease_expires_at=NULL, updated_at=%s"
                " WHERE id=ANY(%s) AND state=%s"
                " AND result_received_at IS NULL"
                " RETURNING id",
                (JobState.QUEUED, now, actions[RecoveryAction.REQUEUE], JobState.RUNNING),
            ).fetchall()
            failed_rows = connection.execute(
                "UPDATE jobs SET state=%s, last_error=%s, updated_at=%s"
                " WHERE id=ANY(%s) AND state=%s"
                " AND result_received_at IS NULL"
                " RETURNING id, flow_id",
                (
                    JobState.FAILED,
                    RECOVERY_EXPIRED,
                    now,
                    actions[RecoveryAction.FAIL],
                    JobState.RUNNING,
                ),
            ).fetchall()
            connection.execute(
                "UPDATE jobs SET last_error=%s WHERE id=ANY(%s) AND state='running'"
                " AND result_received_at IS NULL",
                (RECOVERY_BLOCKED, actions[RecoveryAction.BLOCKED]),
            )
            requeued_ids = [row["id"] for row in requeued_rows]
            if requeued_ids:
                connection.execute(
                    "UPDATE workers SET status=%s, current_job_id=NULL"
                    " WHERE current_job_id=ANY(%s)",
                    (WorkerStatus.OFFLINE, requeued_ids),
                )
            failed_ids = [row["id"] for row in failed_rows]
            if failed_ids:
                connection.execute(
                    "UPDATE workers SET status=%s, current_job_id=NULL"
                    " WHERE current_job_id=ANY(%s)",
                    (WorkerStatus.FAILED, failed_ids),
                )
            flow_ids = [row["flow_id"] for row in failed_rows]
            if flow_ids:
                connection.execute(
                    "UPDATE flows SET state=%s, updated_at=%s WHERE id=ANY(%s) AND state=%s",
                    (FlowState.FAILED, now, flow_ids, FlowState.ACTIVE),
                )
                connection.execute(
                    "UPDATE flow_executions SET released_at=%s"
                    " WHERE flow_id=ANY(%s) AND released_at IS NULL",
                    (now, flow_ids),
                )
            # Binding eligibility observes the preceding job/Flow writes, as the
            # old sequential UPDATEs did; it must not use the pre-transition facts.
            bindings = connection.cursor(row_factory=class_row(BindingRecoveryFacts)).execute(
                "SELECT e.flow_id,e.worker_id,e.recovery_deadline,"
                " f.state AS flow_state,d.state AS delivery_state,"
                " EXISTS (SELECT 1 FROM execution_stops s WHERE s.flow_id=e.flow_id"
                " AND s.state!='confirmed') AS has_pending_stop,"
                " EXISTS (SELECT 1 FROM jobs unknown WHERE unknown.flow_id=e.flow_id"
                " AND unknown.dispatch_authorized_at IS NOT NULL AND unknown.started_at IS NULL"
                " AND unknown.result_received_at IS NULL) AS has_unstarted_dispatch,"
                " EXISTS (SELECT 1 FROM jobs j WHERE j.flow_id=e.flow_id"
                " AND j.result_received_at IS NOT NULL AND j.result_applied_at IS NULL) AS has_unapplied_result"
                " FROM flow_executions e JOIN flows f ON f.id=e.flow_id"
                " LEFT JOIN flow_deliveries d ON d.flow_id=e.flow_id"
                " WHERE e.released_at IS NULL AND e.recovery_deadline<=%s"
                " ORDER BY e.flow_id FOR UPDATE OF e",
                (now,),
            ).fetchall()
            release_ids = [binding.flow_id for binding in bindings if should_release_binding(binding, now)]
            expired_executions = connection.execute(
                "UPDATE flow_executions SET released_at=%s WHERE flow_id=ANY(%s)"
                " AND released_at IS NULL RETURNING flow_id,worker_id",
                (now, release_ids),
            ).fetchall()
            expired_flow_ids = [row["flow_id"] for row in expired_executions]
            if expired_flow_ids:
                connection.execute(
                    "UPDATE flows SET state=%s, updated_at=%s WHERE id=ANY(%s) AND state=%s",
                    (FlowState.FAILED, now, expired_flow_ids, FlowState.ACTIVE),
                )
                connection.execute(
                    "UPDATE workers SET status=%s, current_job_id=NULL WHERE worker_id=ANY(%s)",
                    (
                        WorkerStatus.FAILED,
                        [row["worker_id"] for row in expired_executions],
                    ),
                )
        return len(requeued_rows), len(failed_rows) + len(expired_executions)

    def drain(self, worker_id: str, enabled: bool) -> None:
        with self._db.connection() as conn:
            result = conn.execute("UPDATE workers SET draining=%s WHERE worker_id=%s", (enabled, worker_id))
            if result.rowcount != 1:
                raise ValueError('unknown worker')
