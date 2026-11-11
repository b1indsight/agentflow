"""Worker slots, fenced assignments, preparation, and lease recovery."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from psycopg import Rollback
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.contracts.protocol import Failed, InferenceResult, TerminalPayload, terminal_digest
from app.contracts.receipts import ResultInbox
from app.domain.inference_models import (
    LOCAL_BUB_WORKER_ID,
    InferenceCapability,
    InferenceFailure,
    InferenceSummary,
    InferenceTask,
    StopReason,
)
from app.domain.models import (
    ArchiveReason,
    DeliveryState,
    FlowExecution,
    FlowState,
    Job,
    JobKind,
    JobState,
    WorkerRecord,
    WorkerStatus,
    WorkspaceInventory,
)
from app.domain.recovery_policy import (
    RECOVERY_BLOCKED,
    JobRecoveryFacts,
    RecoveryAction,
    decide_job_recovery,
)
from app.github import GitHubError, retry_delay
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.inference_notices import InferenceNoticeStore
from app.persistence.jobs import JobStore
from app.persistence.records import required
from app.persistence.repository import Repository
from app.persistence.results import RESOURCE_JOB_SQL, UNCONFIRMED_EFFECT_SQL

# A frozen archive may have started deleting before its Started message reached serve.
_MAY_HAVE_EXECUTED = (
    "(started_at IS NOT NULL OR dispatch_authorized_at IS NOT NULL OR (kind='archive' AND payload->'archive' IS NOT NULL))"
)

# Every assignment API resolves the immutable job snapshot, not a worker's latest
# binding. Terminal replay may outlive current_job_id, but not its physical generation.
_ASSIGNMENT_WORKSPACE = (
    " EXISTS (SELECT 1 FROM workers slot WHERE slot.worker_id=jobs.assigned_worker_id"
    " AND slot.workspace=jobs.workspace AND slot.generation=jobs.generation"
    " AND ((jobs.kind='inference' AND slot.repository IS NULL)"
    " OR (jobs.kind<>'inference' AND slot.repository IS NOT NULL)))"
)
_CURRENT_WORKSPACE = (
    " EXISTS (SELECT 1 FROM workers slot WHERE slot.worker_id=jobs.assigned_worker_id"
    " AND slot.workspace=jobs.workspace AND slot.generation=jobs.generation"
    " AND ((jobs.kind='inference' AND slot.current_inference_job_id=jobs.id)"
    " OR (jobs.kind<>'inference' AND slot.current_job_id=jobs.id)))"
)

_ASSIGNMENT_FLOW = (
    " EXISTS (SELECT 1 FROM flows current_flow WHERE current_flow.id=jobs.flow_id"
    " AND current_flow.current_round_no=jobs.round_no"
    " AND current_flow.state<>'archived')"
)

# Shared by demand and claims: demand publishes repositories, never job ownership.
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
    " AND prior.kind<>'inference' AND (prior.state='running' OR (prior.result_received_at IS NOT NULL"
    " AND prior.result_applied_at IS NULL) OR EXISTS (SELECT 1 FROM workers held"
    " WHERE held.current_job_id=prior.id)))"
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

    def get_worker_executions(self, worker_id: str) -> list[FlowExecution]:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(FlowExecution)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM flow_executions WHERE worker_id=%s AND released_at IS NULL ORDER BY workspace",
                (worker_id,),
            ).fetchall()

    def get_worker_execution(self, worker_id: str, workspace: str | None = None) -> FlowExecution | None:
        """Compatibility for a single binding; never pick an arbitrary concurrent Flow."""
        executions = self.get_worker_executions(worker_id)
        if workspace is not None:
            return next((item for item in executions if item.workspace == workspace), None)
        if len(executions) > 1:
            raise ValueError("worker has multiple executions; specify a workspace")
        return executions[0] if executions else None

    def release_flow_execution(self, flow_id: int) -> bool:
        now = self._db.now()
        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_workflow(flow_id)
            result = connection.execute(
                "UPDATE flow_executions SET released_at=%s"
                " WHERE flow_id=%s AND released_at IS NULL"
                " AND EXISTS (SELECT 1 FROM workers w WHERE w.worker_id=flow_executions.worker_id"
                " AND w.workspace=flow_executions.workspace AND w.generation=flow_executions.generation"
                " AND w.current_job_id IS NULL)"
                " AND NOT EXISTS (SELECT 1 FROM execution_stops s WHERE s.flow_id=flow_executions.flow_id"
                " AND s.binding_at=flow_executions.bound_at AND s.state!='confirmed')"
                " AND NOT EXISTS (SELECT 1 FROM jobs j JOIN flows f ON f.id=j.flow_id"
                " WHERE j.flow_id=flow_executions.flow_id"
                f" AND j.round_no=f.current_round_no AND j.kind<>'inference' AND {RESOURCE_JOB_SQL}"
                " AND (j.state='running'"
                " OR (j.result_received_at IS NOT NULL AND j.result_applied_at IS NULL)"
                " OR ((j.started_at IS NOT NULL OR j.dispatch_authorized_at IS NOT NULL"
                " OR (j.kind='archive' AND j.payload->'archive' IS NOT NULL))"
                " AND j.result_received_at IS NULL AND NOT EXISTS (SELECT 1 FROM execution_stops s"
                " WHERE s.job_id=j.id AND s.assignment_token=j.assignment_token AND s.state='confirmed'))))"
                " AND NOT EXISTS (SELECT 1 FROM result_effects e JOIN jobs j ON j.id=e.job_id"
                " JOIN flows f ON f.id=j.flow_id WHERE j.flow_id=flow_executions.flow_id"
                f" AND j.round_no=f.current_round_no AND {RESOURCE_JOB_SQL} AND {UNCONFIRMED_EFFECT_SQL})",
                (now, flow_id),
            )
            if result.rowcount == 1:
                used = required(connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM jobs j JOIN flow_executions e ON e.flow_id=j.flow_id"
                    " WHERE e.flow_id=%s AND j.assigned_worker_id=e.worker_id AND j.workspace=e.workspace"
                    " AND j.generation=e.generation AND (j.started_at IS NOT NULL"
                    " OR j.dispatch_authorized_at IS NOT NULL OR j.result_payload->>'source'='worker'"
                    " OR (j.kind='archive' AND j.payload->'archive' IS NOT NULL))"
                    " AND (j.started_at IS NOT NULL OR j.result_payload->>'source' IS DISTINCT FROM 'control')) AS executed",
                    (flow_id,),
                ).fetchone())["executed"]
                # A control-only preparation never sent to the worker has no local
                # binding to clean. Every possibly-used generation stays retired.
                connection.execute(
                    "UPDATE workers w SET workspace_state=%s,inventory_epoch=CASE WHEN %s THEN NULL ELSE inventory_epoch END,"
                    " retired_generation=CASE WHEN %s THEN w.generation ELSE retired_generation END"
                    " FROM flow_executions e WHERE e.flow_id=%s AND w.worker_id=e.worker_id"
                    " AND w.workspace=e.workspace AND w.generation=e.generation",
                    ("releasing" if used else "idle", used, used, flow_id),
                )
        return result.rowcount == 1

    def worker_connected(
        self,
        worker_id: str,
        protocol_version: int,
        connection_epoch: str = "",
        *, workspace: str, repo_workspace_limit: int, execute: bool = True,
    ) -> WorkerRecord:
        WorkspaceInventory(workspace=workspace, repository=None, generation=1, state="idle", flow_id=None)
        if repo_workspace_limit <= 0:
            raise ValueError("repo workspace limit must be positive")
        now = self._db.now()
        with (
            self._db.transaction() as tx,
            tx.connection() as connection,
            connection.cursor(row_factory=class_row(WorkerRecord)) as cursor,
        ):
            tx._lock_workers(connection)
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
                WorkerStore(tx)._recover_inference(worker_id)
            row = cursor.execute(
                "INSERT INTO workers(worker_id,workspace,repo_workspace_limit, protocol_version, status, connection_epoch,"
                " current_job_id, first_connected_at, last_connected_at, last_seen_at,"
                " last_error, last_disconnect_reason,execute)"
                " VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,NULL,NULL,%s)"
                " ON CONFLICT(worker_id,workspace) DO UPDATE SET protocol_version=excluded.protocol_version,"
                " status='online',repo_workspace_limit=excluded.repo_workspace_limit,"
                " connection_epoch=excluded.connection_epoch,execute=excluded.execute,inference=NULL,"
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
                ),
            ).fetchone()
            connection.execute(
                "UPDATE workers SET inventory_epoch=NULL WHERE worker_id=%s",
                (worker_id,),
            )
        return required(row)

    def report_workspace(self, worker_id: str, epoch: str, item: WorkspaceInventory) -> bool:
        """Reconcile one inventory item without treating missing/late reports as releases.

        Only a matching terminal ACK witness clears a claim. Released generations
        stay retired even if an old idle report arrives after archive or cancellation.
        """
        with self._db.transaction() as tx, tx.connection() as conn:
            tx._lock_workers(conn)
            owner = conn.execute(
                "SELECT * FROM workers WHERE worker_id=%s AND repository IS NULL"
                " AND connection_epoch=%s AND status IN ('online','busy')", (worker_id, epoch),
            ).fetchone()
            if owner is None:
                return False
            row = conn.execute(
                "SELECT * FROM workers WHERE worker_id=%s AND workspace=%s", (worker_id, item.workspace),
            ).fetchone()
            binding = WorkerStore(tx).get_worker_execution(worker_id, item.workspace)
            if binding is not None and row is None:
                return False
            if binding is not None:
                tx.lock_workflow(binding.flow_id)
            if row is not None and (row["repository"] != item.repository or item.generation < row["generation"]):
                return False
            if item.repository is None and (row is None or item.workspace != owner["workspace"] or item.flow_id is not None):
                return False
            advancing = row is not None and item.generation > row["generation"]
            if advancing and row is not None and (item.generation != row["generation"] + 1 or item.flow_id is not None
                              or item.state not in {"idle", "absent"} or item.repository is None):
                return False
            settled_flow: int | None = None
            cleanup_handoff = False
            cleanup_settled = False
            if item.settled_job_id is not None:
                job = JobStore(tx).get_job(item.settled_job_id)
                if (job is None or job.assigned_worker_id != worker_id or job.workspace != item.workspace
                        or job.generation is None or job.assignment_token != item.settled_assignment_token
                        or row is None or job.generation not in {row["generation"], row["generation"] - 1}
                        or item.generation not in {job.generation, job.generation + 1}):
                    return False
                if job.flow_id is not None:
                    tx.lock_workflow(job.flow_id)
                job = required(tx.lock_job(job.id))
                stopped = conn.execute(
                    "SELECT state,binding_at,receipt_digest FROM execution_stops"
                    " WHERE job_id=%s AND assignment_token=%s AND worker_id=%s"
                    " AND workspace=%s AND generation=%s",
                    (job.id, job.assignment_token, worker_id, item.workspace, job.generation),
                ).fetchone()
                # The same journal may first ACK its result and later be stopped.
                # Its retained result witness must neither break reconnect nor
                # masquerade as receipt of the newer stop ACK.
                prior_result = (stopped is not None and job.result_applied_at is not None
                                and not advancing and item.flow_id == job.flow_id)
                if stopped is not None and stopped["state"] != "confirmed" and not prior_result:
                    return False
                if job.result_applied_at is None and stopped is None:
                    return False
                cleanup_settled = (stopped is not None and stopped["state"] == "confirmed"
                                   and stopped["receipt_digest"] is not None
                                   and binding is not None and job.flow_id == binding.flow_id
                                   and stopped["binding_at"] == binding.bound_at and item.flow_id is None)
                cleanup_handoff = cleanup_settled and advancing and item.state == "idle"
                if row["current_job_id"] == job.id and job.generation != row["generation"]:
                    return False
                if row["current_job_id"] == job.id and not prior_result:
                    if stopped is not None and (item.flow_id is not None or (
                        not advancing and item.state not in {"releasing", "absent", "quarantined"}
                    )):
                        return False
                    row["current_job_id"] = None
                    settled_flow = job.flow_id
                # Inventory retains the previous ACK while the next job runs.
                # Its witness is valid history, not permission to clear that new claim.
            if advancing and row is not None and (row["current_job_id"] is not None or row["current_inference_job_id"] is not None
                              or (binding is not None and settled_flow != binding.flow_id)):
                return False
            if row is None or (row["workspace_state"] == "absent" and item.state != "absent"):
                count = conn.execute(
                    "SELECT count(*) AS n FROM workers WHERE worker_id=%s AND repository=%s"
                    " AND workspace_state<>'absent'", (worker_id, item.repository),
                ).fetchone()
                if item.repository is not None and required(count)["n"] >= owner["repo_workspace_limit"]:
                    return False
            if binding is None and item.flow_id is not None:
                return False
            if binding is not None and (row is None or binding.generation != row["generation"]
                                        or item.flow_id not in (None, binding.flow_id)):
                return False
            retired = row is not None and row["retired_generation"] is not None and item.generation <= row["retired_generation"]
            accepted = False
            with conn.transaction() as settlement:
                if settled_flow is not None:
                    assert row is not None
                    cleared = conn.execute(
                        "UPDATE workers SET current_job_id=NULL WHERE worker_id=%s AND workspace=%s"
                        " AND generation=%s AND current_job_id=%s",
                        (worker_id, item.workspace, row["generation"], item.settled_job_id),
                    )
                    if cleared.rowcount != 1:
                        raise ValueError("workspace settlement lost its locked claim")
                if binding is not None and settled_flow == binding.flow_id:
                    flow = required(FlowStore(tx).get_flow(binding.flow_id))
                    if flow.state is FlowState.ARCHIVED and (advancing or cleanup_settled):
                        # Business archival is already committed. Settle only the
                        # original cleanup/ACK, atomically with the inventory claim;
                        # a failed release must leave the old generation reserved.
                        if not cleanup_settled or not WorkerStore(tx).release_flow_execution(flow.id):
                            raise Rollback(settlement)
                        binding = None
                if advancing and binding is not None:
                    if FlowStore(tx).try_archive(binding.flow_id):
                        binding = None
                    else:
                        # A merge racing stop cleanup still needs archive on this exact
                        # directory. Carry its exclusive reservation to the cleaned generation.
                        flow = required(FlowStore(tx).get_flow(binding.flow_id))
                        delivery = DeliveryStore(tx).get_delivery(binding.flow_id)
                        if (not cleanup_handoff or flow.state is not FlowState.FINALIZING
                                or flow.archive_reason is not ArchiveReason.SUCCEEDED
                                or delivery is None or delivery.state is not DeliveryState.MERGED
                                or JobStore(tx).has_pending_result(flow.id)
                                or conn.execute(
                                    "SELECT 1 FROM execution_stops WHERE flow_id=%s AND state<>'confirmed'"
                                    " UNION ALL SELECT 1 FROM result_effects e JOIN jobs j ON j.id=e.job_id"
                                    f" WHERE j.flow_id=%s AND {RESOURCE_JOB_SQL} AND {UNCONFIRMED_EFFECT_SQL} LIMIT 1",
                                    (flow.id, flow.id),
                                ).fetchone() is not None):
                            raise Rollback(settlement)
                        updated = conn.execute(
                            "UPDATE flow_executions SET generation=%s WHERE flow_id=%s"
                            " AND worker_id=%s AND workspace=%s AND generation=%s AND released_at IS NULL",
                            (item.generation, flow.id, worker_id, item.workspace, binding.generation),
                        )
                        if updated.rowcount != 1:
                            raise ValueError("cleanup handoff lost its locked Flow binding")
                        binding.generation = item.generation
                held = row is not None and row["current_job_id"] is not None
                state = "occupied" if held or binding is not None else "releasing" if retired else item.state
                # A late pre-claim report cannot revoke already-confirmed eligibility,
                # but cannot confirm a recovered binding in a new connection epoch.
                confirmed = epoch if binding is None or item.flow_id == binding.flow_id or cleanup_handoff else (
                    row["inventory_epoch"] if row is not None else None
                )
                if item.state == "releasing" and binding is not None and item.flow_id == binding.flow_id:
                    # Verified cleanup may fail midway. Only that original Flow's
                    # archive can retry a retained release; coding cannot use it.
                    state, confirmed = "releasing", epoch
                elif item.state in {"absent", "quarantined", "releasing"}:
                    state = item.state if not held and binding is None else "quarantined"
                    confirmed = None
                conn.execute(
                    "INSERT INTO workers(worker_id,workspace,repository,generation,workspace_state,inventory_epoch,"
                    " protocol_version,status,first_connected_at,last_connected_at,last_seen_at,execute)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,'online',%s,%s,%s,FALSE)"
                    " ON CONFLICT(worker_id,workspace) DO UPDATE SET generation=EXCLUDED.generation,"
                    " workspace_state=EXCLUDED.workspace_state,inventory_epoch=EXCLUDED.inventory_epoch",
                    (worker_id, item.workspace, item.repository, item.generation, state, confirmed,
                     owner["protocol_version"], tx.now(), tx.now(), tx.now()),
                )
                if settled_flow is not None and not advancing:
                    FlowStore(tx).try_archive(settled_flow)
                accepted = True
            return accepted

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
                " AND slot.repository=f.repo AND slot.workspace_state<>'absent') < w.repo_workspace_limit"
                + _ELIGIBLE_JOB + " ORDER BY f.repo", (worker_id, epoch, self._db.now()),
            ).fetchall()]

    def worker_ready(self, worker_id: str, connection_epoch: str,
                     capability: InferenceCapability | None) -> bool:
        """Publish inference readiness for this authenticated, fully registered session only."""
        with self._db.connection() as connection:
            result = connection.execute(
                "UPDATE workers SET inference=%s WHERE worker_id=%s AND connection_epoch=%s"
                " AND repository IS NULL AND status IN ('online','busy')",
                (Jsonb(capability.model_dump(mode="json")) if capability else None, worker_id, connection_epoch),
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
            tx._lock_workers(connection)
            if connection.execute("SELECT 1 FROM workers WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s",
                                  (worker_id, connection_epoch)).fetchone() is None:
                return
            WorkerStore(tx)._recover_inference(worker_id)
            connection.execute(
                "UPDATE flow_executions SET recovery_deadline=COALESCE(recovery_deadline,%s)"
                " WHERE worker_id=%s AND released_at IS NULL"
                " AND EXISTS (SELECT 1 FROM workers WHERE repository IS NULL AND worker_id=%s"
                " AND connection_epoch=%s)",
                (now + self._db.recovery_grace, worker_id, worker_id, connection_epoch),
            )
            connection.execute(
                "UPDATE jobs SET recovery_deadline=COALESCE(recovery_deadline,%s), updated_at=%s"
                " WHERE state=%s AND " + _MAY_HAVE_EXECUTED + " AND assigned_worker_id=%s"
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
                " owner.execute,owner.inference,owner.repo_workspace_limit,owner.last_seen_at,"
                " owner.first_connected_at,owner.last_connected_at,owner.last_disconnect_reason"
                " FROM workers slot JOIN workers owner ON owner.worker_id=slot.worker_id AND owner.repository IS NULL"
                " WHERE slot.worker_id=%s ORDER BY slot.workspace", (worker_id,),
            ).fetchall()

    def assign_job(self, worker_id: str, token: str, connection_epoch: str = "", *,
                   slot: Literal["execute", "infer"] = "execute") -> Job | None:
        """Claim a concrete workspace, retaining Flow affinity and the single inference lane.

        Execute retains Flow affinity; infer compares ready capabilities without claiming
        a repository. Worker-first locks make local preference independent of poll order.
        """

        if slot not in {"execute", "infer"}:
            raise ValueError("unknown worker slot")
        now = self._db.now()
        with self._db.transaction() as tx, tx.connection() as connection:
            workers = WorkerStore(tx)
            if slot == "infer":
                tx._lock_workers(connection)
                available = connection.cursor(row_factory=class_row(WorkerRecord)).execute(
                    "SELECT * FROM workers WHERE repository IS NULL AND status IN ('online','busy') AND NOT draining"
                    " AND inventory_epoch=connection_epoch AND workspace_state='idle'"
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
                for job in pending:
                    assert job.inference is not None
                    call = job.inference
                    if workers.inference_needs_cancel(job.id):
                        continue
                    now = tx.now()
                    if call.next_attempt_at is not None and call.next_attempt_at > now:
                        continue
                    if call.deadline_at <= now:
                        workers._settle_inference(job, InferenceFailure(code="execution_timeout"))
                        continue
                    current_task = call.task.model_copy(update={
                        "request_id": uuid4().hex,
                        "timeout_seconds": (call.deadline_at - now).total_seconds(),
                    })
                    candidates = [w for w in available if w.worker_id not in call.excluded_workers
                                  and w.inference is not None and w.inference.supports(current_task)]
                    # Avoid confirmed failures when another worker is ready. Within that
                    # tier bub wins; unknown/busy workers are excluded, not just avoided.
                    candidates.sort(key=lambda w: (w.worker_id in call.avoid_workers,
                                                   w.worker_id != LOCAL_BUB_WORKER_ID))
                    if candidates and candidates[0].worker_id == worker_id:
                        return workers._claim_job(job.id, worker_id, token, connection_epoch, current_task)
                return None
            # A Flow may also own an inference lane on another worker. Fence all
            # owners in deterministic order before locking any Flow or job.
            tx._lock_workers(connection)
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
                " AND bound.generation=slot.generation AND (slot.workspace_state IN ('idle','occupied')"
                " OR (j.kind='archive' AND slot.workspace_state='releasing')))"
                " OR (bound.flow_id IS NULL AND occupied.flow_id IS NULL AND j.kind='initial'"
                " AND slot.workspace_state='idle' AND NOT owner.draining"
                " AND (SELECT count(*) FROM flow_executions active JOIN flows owned ON owned.id=active.flow_id"
                " WHERE active.worker_id=owner.worker_id AND active.released_at IS NULL"
                " AND owned.repo=f.repo)<owner.repo_workspace_limit))" + _ELIGIBLE_JOB
            )
            coding_candidates = connection.execute(
                selection + " ORDER BY j.created_at,j.id,slot.workspace", (worker_id, connection_epoch, now),
            ).fetchall()
            for candidate in coding_candidates:
                # Lock Flow explicitly before job, and re-evaluate after any competing claim.
                if connection.execute(
                    "SELECT id FROM flows WHERE id=%s FOR NO KEY UPDATE SKIP LOCKED",
                    (candidate["flow_id"],),
                ).fetchone() is None:
                    continue
                row = connection.execute(
                    selection + " AND j.id=%s AND slot.workspace=%s",
                    (worker_id, connection_epoch, now, candidate["id"], candidate["workspace"]),
                ).fetchone()
                if row is None:
                    continue
                tx.lock_job(row["id"])
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
                return workers._claim_job(row["id"], worker_id, token, connection_epoch, None)
            return None

    def _claim_job(self, job_id: int, worker_id: str, token: str, epoch: str,
                   current_task: InferenceTask | None) -> Job | None:
        """Commit ownership and the authorized-attempt count under the allocator's locks."""
        now = self._db.now()
        candidate = required(JobStore(self._db).get_job(job_id))
        if candidate.flow_id is not None:
            self._db.lock_workflow(candidate.flow_id)
        job = required(self._db.lock_job(job_id))
        inference = job.inference
        with self._db.connection() as connection:
            if inference is not None:
                if inference.deadline_at <= now:
                    self._settle_inference(job, InferenceFailure(code="execution_timeout"))
                    return None
                assert current_task is not None and job.attempt < 3
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
                binding = required(self.get_flow_execution(job.required_flow_id))
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
                    "UPDATE workers SET workspace_state='occupied',current_job_id=%s,last_assigned_at=%s"
                    " WHERE worker_id=%s AND workspace=%s AND generation=%s AND current_job_id IS NULL",
                    (job.id, now, worker_id, workspace, generation),
                )
                connection.execute(
                    "UPDATE workers SET last_assigned_at=%s WHERE worker_id=%s AND repository IS NULL",
                    (now, worker_id),
                )
                connection.execute(
                    "UPDATE flows SET state='running',updated_at=%s WHERE id=%s"
                    " AND state IN ('queued','review') AND archive_reason IS NULL",
                    (now, job.flow_id),
                )
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
                + _CURRENT_WORKSPACE + " AND " + _ASSIGNMENT_FLOW + " FOR UPDATE",
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
        with self._db.connection() as connection:
            self._db._lock_worker(connection, worker_id)
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
                + _CURRENT_WORKSPACE,
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
                + _CURRENT_WORKSPACE,
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
                + _CURRENT_WORKSPACE,
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
        with self._db.connection() as connection, connection.transaction():
            self._db._lock_worker(connection, worker_id)
            row = connection.cursor(row_factory=class_row(Job)).execute(
                "SELECT * FROM jobs WHERE id=%s"
                " AND assigned_worker_id=%s AND assignment_token=%s"
                " AND EXISTS (SELECT 1 FROM workers WHERE worker_id=%s"
                " AND repository IS NULL AND connection_epoch=%s) AND "
                + _ASSIGNMENT_WORKSPACE + " FOR UPDATE",
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

    def _lock_inference(self, job_id: int) -> Job | None:
        """Use the same worker -> Flow -> job order, with no fabricated generic Flow."""
        job = JobStore(self._db).get_job(job_id)
        if job is None or job.kind is not JobKind.INFERENCE:
            return None
        if job.flow_id is not None:
            self._db.lock_workflow(job.flow_id)
        elif job.assigned_worker_id is not None:
            with self._db.connection() as connection:
                self._db._lock_worker(connection, job.assigned_worker_id)
        current = self._db.lock_job(job_id)
        # A queued generic call can be claimed between the initial read and worker
        # lock. Never acquire its new worker after the job lock; let the caller retry.
        if current is not None and (current.assigned_worker_id, current.assignment_token) != (
            job.assigned_worker_id, job.assignment_token,
        ):
            return None
        return current

    def _inference_cancellation(self, job: Job) -> InferenceFailure | None:
        """Only source currentness and the original deadline can retire an active call."""
        from app.persistence.reviews import ReviewStore

        call = job.inference
        assert call is not None
        if call.cancel_failure is not None:
            return call.cancel_failure
        if job.flow_id is not None:
            flow = FlowStore(self._db).get_flow(job.flow_id)
            if (
                flow is None
                or not flow.accepts(JobKind.INFERENCE)
                or flow.current_attempt_id != call.source_attempt_id
            ):
                return InferenceFailure(code="cancelled")
        if call.batch_id is not None:
            batch = ReviewStore(self._db).get_batch(call.batch_id)
            if (batch.inference_job_id != job.id or not ReviewStore(self._db).current(batch)
                    or batch.decision is not None
                    or (call.context_digest is not None
                        and (batch.context is None or batch.context.digest != call.context_digest))):
                return InferenceFailure(code="cancelled")
        # An on-time durable answer stays valid when application resumes after restart.
        received = job.result_received_at if job.state is JobState.RUNNING and job.result_applied_at is None else None
        if (received or self._db.now()) >= call.deadline_at:
            return InferenceFailure(code="execution_timeout")
        return None

    def inference_needs_cancel(self, job_id: int) -> bool:
        """Retire expired queued work, or persist a running cancellation until cleanup.

        True means this call must not dispatch/continue. A running slot is deliberately
        retained until its matching cleanup result or connection-loss recovery arrives.
        """
        with self._db.transaction() as tx, tx.connection() as connection:
            workers = WorkerStore(tx)
            job = workers._lock_inference(job_id)
            if job is None or job.state not in {JobState.QUEUED, JobState.RUNNING}:
                return False
            failure = workers._inference_cancellation(job)
            if failure is None:
                return False
            assert job.inference is not None
            job.inference = job.inference.model_copy(update={"cancel_failure": failure})
            connection.execute("UPDATE jobs SET inference=%s WHERE id=%s",
                               (Jsonb(job.inference.model_dump(mode="json")), job.id))
            if job.state is JobState.QUEUED:
                workers._settle_inference(job, failure)
            return True

    def complete_inference_result(self, job_id: int, *, failure: InferenceFailure | None = None) -> bool:
        """Apply caller validation to a durable result after worker cleanup, never before it.

        The original InferenceResult JSON is retained for restart/replay. Validation
        failures override worker success; cancellation overrides either outcome. Retry
        budgeting, backoff, summary and batch notice publication share this transaction.
        """
        with self._db.transaction() as tx:
            workers = WorkerStore(tx)
            job = workers._lock_inference(job_id)
            if (job is None or job.state is not JobState.RUNNING or job.result_payload is None
                    or job.result_received_at is None or job.result_applied_at is not None):
                return False
            with tx.connection() as conn:
                if conn.execute(
                    "SELECT 1 FROM jobs WHERE id=%s AND " + _CURRENT_WORKSPACE, (job.id,),
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
            workers._settle_inference(job, failure or result.failure)
            return True

    def _settle_inference(self, job: Job, failure: InferenceFailure | None) -> None:
        """Transition a locked, cleaned-up (or disconnected) call without coding effects."""
        if job.state is JobState.RUNNING:
            with self._db.connection() as connection:
                if connection.execute(
                    "SELECT 1 FROM jobs WHERE id=%s AND " + _CURRENT_WORKSPACE, (job.id,),
                ).fetchone() is None:
                    raise ValueError("inference settlement lost its workspace generation")
        call = job.inference
        assert call is not None
        now = self._db.now()
        cancellation = self._inference_cancellation(job)
        failure = cancellation or failure
        state = JobState.DONE
        if failure is not None:
            delay = failure.retry_after_seconds
            if delay is None:
                delay = 0.25 * job.attempt if failure.code in {"rate_limited", "backend_unavailable"} else 0
            # A provider may suggest any finite delay; compare before constructing a
            # datetime so a huge Retry-After cannot strand the durable inbox forever.
            next_at = (call.deadline_at if delay >= (call.deadline_at - now).total_seconds()
                       else now + timedelta(seconds=delay))
            state = JobState.FAILED
            reason: StopReason = "non_retryable"
            if failure.recoverable:
                if cancellation is not None or next_at >= call.deadline_at:
                    reason = "deadline"
                elif job.attempt >= 3:
                    reason = "max_attempts"
                else:
                    state = JobState.QUEUED
            worker_ids = frozenset({job.assigned_worker_id}) if job.assigned_worker_id else frozenset()
            call = call.model_copy(update={
                "failures": [*call.failures, failure] if job.state is JobState.RUNNING else call.failures,
                "avoid_workers": call.avoid_workers | worker_ids,
                "excluded_workers": call.excluded_workers | (
                    worker_ids if failure.code in {"busy", "connection_closed", "rpc_timeout"} else frozenset()),
                "next_attempt_at": next_at if state is JobState.QUEUED else None,
                "cancel_failure": cancellation,
            })
            if state is JobState.FAILED:
                call = call.model_copy(update={"summary": InferenceSummary(
                    logical_call_id=call.task.request_id, attempts=job.attempt,
                    stop_reason=reason, last_failure_code=failure.code,
                    last_worker_id=job.assigned_worker_id, backend=call.backend,
                    finished_at=now, elapsed_ms=max(0, int((now - job.created_at).total_seconds() * 1000)),
                )})
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
            flow = FlowStore(self._db).get_flow(job.flow_id)
            if flow is not None and job.round_no == flow.current_round_no:
                FlowStore(self._db).try_archive(job.flow_id)

    def _recover_inference(self, worker_id: str | None = None) -> None:
        """Classify unknown authorized attempts; a durable inbox always wins over recovery.

        Callers already hold the ordered worker locks (startup, disconnect or replacement).
        No execution is resumed: each later claim gets a new request and retains its budget.
        """
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE kind='inference' AND state='running' AND result_received_at IS NULL"
                " AND (%s::text IS NULL OR assigned_worker_id=%s) ORDER BY id", (worker_id, worker_id),
            ).fetchall()
            for row in rows:
                job = self._lock_inference(row["id"])
                assert job is not None
                self._settle_inference(job, InferenceFailure(code="connection_closed"))

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
                " AND kind<>'inference'"
                " AND assignment_token=%s AND EXISTS (SELECT 1 FROM workers"
                " WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s) AND "
                + _CURRENT_WORKSPACE,
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
            # Result commit is not proof the worker received its ACK. The concrete
            # claim stays occupied until report_workspace supplies the matching witness.
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
            workers = WorkerStore(tx)
            current = workers._lock_preparation(job_id, worker, token, epoch)
            if (
                current is None
                or current.result_payload is not None
                or current.result_digest is not None
                or current.result_applied_at is not None
            ):
                return False
            flow = FlowStore(tx).get_flow(current.required_flow_id)
            binding = workers.get_flow_execution(current.required_flow_id)
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
            flow = FlowStore(tx).get_flow(current.required_flow_id)
            delivery = DeliveryStore(tx).get_delivery(current.required_flow_id)
            if (
                flow is None
                or not flow.accepts(current.kind)
                or flow.current_round_no != current.round_no
                or (delivery is not None and delivery.supersedes(current.kind))
            ):
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
                terminal = Failed(job_id=job_id, assignment_token=token, error=reason)
                JobStore(tx).record_control_result(job_id, terminal, reason)
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
        candidate = JobStore(self._db).get_job(job_id)
        if candidate is None:
            return False
        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_workflow(candidate.required_flow_id)
            result = connection.execute(
                "UPDATE jobs SET state=%s, assigned_worker_id=NULL, assignment_token=NULL,"
                " workspace=NULL,generation=NULL,"
                " heartbeat_at=NULL, updated_at=%s WHERE id=%s AND state=%s"
                " AND assigned_worker_id=%s AND assignment_token=%s AND started_at IS NULL"
                " AND result_received_at IS NULL AND dispatch_authorized_at IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM execution_stops s WHERE s.job_id=jobs.id AND s.state!='confirmed')"
                " AND EXISTS (SELECT 1 FROM workers WHERE worker_id=%s"
                " AND repository IS NULL AND connection_epoch=%s) AND " + _CURRENT_WORKSPACE,
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
                    "UPDATE flows SET state='queued',updated_at=%s WHERE id=%s AND state='running'"
                    " AND archive_reason IS NULL AND current_round_no=%s",
                    (tx.now(), candidate.flow_id, candidate.round_no),
                )
                connection.execute(
                    "UPDATE workers SET current_job_id=NULL WHERE worker_id=%s AND workspace=%s"
                    " AND generation=%s AND current_job_id=%s",
                    (worker_id, candidate.workspace, candidate.generation, job_id),
                )
        return result.rowcount == 1

    def recover_abandoned_assignments(self) -> None:
        """Fence startup: preserve coding recovery and retry unknown inference attempts."""

        now = self._db.now()
        with self._db.transaction() as tx, tx.connection() as connection:
            tx._lock_workers(connection)
            connection.execute(
                "UPDATE workers SET status=%s,inference=NULL WHERE repository IS NULL",
                (WorkerStatus.OFFLINE,),
            )
            WorkerStore(tx)._recover_inference()
            connection.execute(
                "UPDATE jobs SET recovery_deadline=COALESCE(recovery_deadline,%s),"
                " updated_at=%s WHERE kind<>'inference' AND state=%s AND " + _MAY_HAVE_EXECUTED,
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
        inference_requeued = inference_failed = 0
        with self._db.transaction() as tx, tx.connection() as connection:
            tx._lock_workers(connection)
            # Worker -> Flow -> Job, also for recovery. Never wait for a Flow
            # after locking its job while a result writer holds the opposite order.
            connection.execute(
                "SELECT id FROM flows WHERE id IN (SELECT flow_id FROM jobs WHERE state IN ('queued','running')"
                " UNION SELECT flow_id FROM flow_executions WHERE released_at IS NULL) ORDER BY id FOR NO KEY UPDATE"
            ).fetchall()
            workers = WorkerStore(tx)
            pending = connection.execute(
                "SELECT id FROM jobs WHERE kind='inference' AND state IN ('queued','running') ORDER BY id",
            ).fetchall()
            for item in pending:
                job = workers._lock_inference(item["id"])
                assert job is not None and job.inference is not None
                if job.result_received_at is not None and job.result_applied_at is None:
                    continue
                if job.state is JobState.RUNNING and connection.execute(
                    "SELECT 1 FROM workers WHERE repository IS NULL AND worker_id=%s AND connection_epoch=%s"
                    " AND status IN ('online','busy')",
                    (job.assigned_worker_id, job.inference.assignment_epoch),
                ).fetchone() is None:
                    workers._settle_inference(job, InferenceFailure(code="connection_closed"))
                else:
                    workers.inference_needs_cancel(job.id)
                updated = required(JobStore(tx).get_job(job.id))
                inference_failed += updated.state is JobState.FAILED
                inference_requeued += updated.state is JobState.QUEUED and job.state is JobState.RUNNING
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
            for candidate in candidates:
                actions[decide_job_recovery(candidate, now)].append(candidate.id)
            requeued_rows = connection.execute(
                "UPDATE jobs SET state=%s, assigned_worker_id=NULL, assignment_token=NULL,"
                " workspace=NULL,generation=NULL,"
                " heartbeat_at=NULL, lease_expires_at=NULL, updated_at=%s"
                " WHERE id=ANY(%s) AND state=%s"
                " AND result_received_at IS NULL"
                " RETURNING id",
                (JobState.QUEUED, now, actions[RecoveryAction.REQUEUE], JobState.RUNNING),
            ).fetchall()
            connection.execute(
                "UPDATE flows SET state='queued',updated_at=%s WHERE state='running'"
                " AND archive_reason IS NULL AND id IN (SELECT flow_id FROM jobs WHERE id=ANY(%s))",
                (now, actions[RecoveryAction.REQUEUE]),
            )
            connection.execute(
                "UPDATE jobs SET last_error=%s WHERE id=ANY(%s) AND state='running'"
                " AND result_received_at IS NULL",
                (RECOVERY_BLOCKED, actions[RecoveryAction.BLOCKED]),
            )
            connection.execute(
                "UPDATE flows SET state='blocked',updated_at=%s WHERE id IN"
                " (SELECT flow_id FROM jobs WHERE id=ANY(%s)) AND state<>'archived'",
                (now, actions[RecoveryAction.BLOCKED]),
            )
            requeued_ids = [row["id"] for row in requeued_rows]
            if requeued_ids:
                connection.execute(
                    "UPDATE workers SET current_job_id=NULL"
                    " WHERE current_job_id=ANY(%s)",
                    (requeued_ids,),
                )
            # A recovery timeout says nothing about physical cleanup or local ACKs.
            # Keep every binding until explicit settlement and release; never clear
            # sibling workspace claims when one Flow's recovery window expires.
        return len(requeued_rows) + inference_requeued, inference_failed

    def drain(self, worker_id: str, enabled: bool) -> None:
        with self._db.connection() as conn:
            result = conn.execute(
                "UPDATE workers SET draining=%s WHERE worker_id=%s AND repository IS NULL", (enabled, worker_id),
            )
            if result.rowcount != 1:
                raise ValueError('unknown worker')
