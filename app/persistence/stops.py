"""Durable cancellation fences and worker allocation drain state."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from psycopg.rows import class_row
from pydantic import BaseModel

from app.contracts.protocol import ExecutionStopped, StopFence
from app.contracts.receipts import matches_stop_receipt, stop_receipt_digest
from app.domain.models import DeliveryState, FlowState
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.repository import Repository
from app.persistence.results import RESOURCE_JOB_SQL, UNCONFIRMED_EFFECT_SQL
from app.persistence.workers import WorkerStore


class StopIntent(BaseModel):
    request_id: str
    flow_id: int
    job_id: int
    worker_id: str
    workspace: str
    generation: int
    assignment_token: str
    binding_at: datetime
    reason: str
    requested_at: datetime
    state: str
    confirmed_at: datetime | None
    receipt_digest: str | None

    def fence(self) -> StopFence:
        return StopFence(
            request_id=self.request_id,
            flow_id=self.flow_id,
            job_id=self.job_id,
            assignment_token=self.assignment_token,
        )


class StopStore(Repository):

    def request(self, flow_id: int) -> None:
        """Called under the workflow lock before attempting to release the binding."""
        binding = WorkerStore(self._db).get_flow_execution(flow_id)
        if binding is None or binding.released_at is not None:
            return
        with self._db.connection() as conn:
            job = conn.execute(
                "SELECT id,assignment_token FROM jobs WHERE flow_id=%s AND assigned_worker_id=%s"
                " AND workspace=%s AND generation=%s"
                " AND kind<>'inference'"
                " AND (started_at IS NOT NULL OR dispatch_authorized_at IS NOT NULL)"
                " AND (started_at IS NOT NULL OR result_payload->>'source' IS DISTINCT FROM 'control')"
                " AND assignment_token IS NOT NULL"
                " AND (started_at>=%s OR dispatch_authorized_at>=%s)"
                " ORDER BY id DESC LIMIT 1 FOR UPDATE",
                (flow_id, binding.worker_id, binding.workspace, binding.generation, binding.bound_at, binding.bound_at),
            ).fetchone()
            if job is not None:
                inserted = conn.execute(
                    "INSERT INTO execution_stops(request_id,flow_id,job_id,worker_id,workspace,generation,assignment_token,"
                    " binding_at,requested_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT(flow_id,workspace,generation) DO NOTHING RETURNING request_id",
                    (uuid4().hex, flow_id, job['id'], binding.worker_id, binding.workspace, binding.generation, job['assignment_token'],
                     binding.bound_at, self._db.now()),
                ).fetchone()
                if inserted is not None:
                    # Review has no running job, but stopping its retained binding
                    # still needs an ACK witness before that position can be retired.
                    # A successor still in control-only preparation has no journal;
                    # cancellation retires it after fencing the last actual execution.
                    held = conn.execute(
                        "UPDATE workers SET current_job_id=%s WHERE worker_id=%s AND workspace=%s"
                        " AND generation=%s AND (current_job_id IS NULL OR current_job_id=%s"
                        " OR EXISTS (SELECT 1 FROM jobs prep WHERE prep.id=workers.current_job_id"
                        " AND prep.flow_id=%s AND prep.started_at IS NULL AND prep.dispatch_authorized_at IS NULL"
                        " AND prep.result_received_at IS NULL))",
                        (job["id"], binding.worker_id, binding.workspace, binding.generation, job["id"], flow_id),
                    )
                    if held.rowcount != 1:
                        raise ValueError("stop requires its exact workspace generation and claim")

    def authorize_dispatch(self, job_id: int, worker_id: str, token: str, epoch: str) -> bool:
        """Persist the point after which a lost Started ACK cannot mean 'never sent'."""
        job = JobStore(self._db).get_job(job_id)
        if job is None:
            return False
        with self._db.transaction() as tx:
            flow = tx.lock_workflow(job.required_flow_id)
            if flow is None or not flow.accepts(job.kind) or flow.current_round_no != job.round_no:
                return False
            delivery = DeliveryStore(tx).get_delivery(flow.id)
            if delivery is not None and (delivery.state is DeliveryState.CLOSED or delivery.supersedes(job.kind)):
                return False
            if not WorkerStore(tx).assignment_active(job_id, worker_id, token, epoch):
                return False
            with tx.connection() as conn:
                conn.execute("UPDATE jobs SET dispatch_authorized_at=COALESCE(dispatch_authorized_at,%s) WHERE id=%s",
                             (tx.now(), job_id))
            return True

    def has_pending(self, flow_id: int) -> bool:
        with self._db.connection() as conn:
            return conn.execute("SELECT 1 FROM execution_stops WHERE flow_id=%s AND state!='confirmed' LIMIT 1",
                                (flow_id,)).fetchone() is not None

    def pending(self, worker_id: str) -> StopIntent | None:
        """Return one intent for legacy single-message callers, never use for recovery scans."""
        pending = self.pending_for_worker(worker_id)
        return pending[0] if pending else None

    def pending_for_worker(self, worker_id: str) -> list[StopIntent]:
        """Enumerate every independent stop; omission is never cleanup evidence."""
        with self._db.connection() as conn:
            return conn.cursor(row_factory=class_row(StopIntent)).execute(
                "SELECT * FROM execution_stops WHERE worker_id=%s AND state!='confirmed'"
                " ORDER BY requested_at,request_id", (worker_id,),
            ).fetchall()

    def receipt_request(self, worker_id: str, request_id: str) -> StopIntent | None:
        with self._db.connection() as conn:
            return conn.cursor(row_factory=class_row(StopIntent)).execute(
                "SELECT * FROM execution_stops WHERE worker_id=%s AND request_id=%s",
                (worker_id, request_id),
            ).fetchone()

    def confirm(self, worker_id: str, receipt: ExecutionStopped, connection_epoch: str) -> bool:
        """A stopped executor alone cannot release a binding with unapplied external facts."""
        with self._db.transaction() as tx:
            flow = tx.lock_workflow(receipt.flow_id)
            if flow is None:
                return False
            with tx.connection() as conn:
                intent = conn.cursor(row_factory=class_row(StopIntent)).execute(
                    "SELECT * FROM execution_stops WHERE request_id=%s FOR UPDATE", (receipt.request_id,),
                ).fetchone()
                binding = WorkerStore(tx).get_flow_execution(receipt.flow_id)
                worker = conn.execute(
                    "SELECT connection_epoch FROM workers WHERE worker_id=%s AND repository IS NULL", (worker_id,),
                ).fetchone()
                if (
                    intent is None
                    or worker is None
                    or worker["connection_epoch"] != connection_epoch
                    or intent.worker_id != worker_id
                    or intent.job_id != receipt.job_id
                    or intent.flow_id != receipt.flow_id
                    or intent.assignment_token != receipt.assignment_token
                ):
                    return False
                if intent.receipt_digest is not None and not matches_stop_receipt(
                    receipt, intent.receipt_digest
                ):
                    return False
                if intent.state == 'confirmed':
                    # Re-ACK immutable evidence without touching a newer allocation.
                    # The current generation is required only for a first confirmation.
                    return intent.receipt_digest is not None
                if conn.execute(
                    "SELECT 1 FROM workers w JOIN jobs j ON j.assigned_worker_id=w.worker_id"
                    " AND j.workspace=w.workspace AND j.generation=w.generation"
                    " WHERE w.worker_id=%s AND w.workspace=%s AND w.generation=%s"
                    " AND j.id=%s AND j.assignment_token=%s",
                    (worker_id, intent.workspace, intent.generation, intent.job_id, intent.assignment_token),
                ).fetchone() is None:
                    return False
                if (binding is None or binding.worker_id != worker_id or binding.bound_at != intent.binding_at
                        or binding.workspace != intent.workspace or binding.generation != intent.generation):
                    return False
                # A valid historical hash remains the durable identity of this receipt.
                digest = intent.receipt_digest or stop_receipt_digest(receipt)
                conn.execute("UPDATE execution_stops SET state='stopped',receipt_digest=%s WHERE request_id=%s",
                             (digest, intent.request_id))
                if JobStore(tx).has_pending_result(receipt.flow_id) or conn.execute(
                        "SELECT 1 FROM result_effects e JOIN jobs j ON j.id=e.job_id"
                        f" WHERE j.flow_id=%s AND {RESOURCE_JOB_SQL} AND {UNCONFIRMED_EFFECT_SQL} LIMIT 1",
                        (receipt.flow_id,),
                ).fetchone() is not None:
                    return False
                delivery = DeliveryStore(tx).get_delivery(intent.flow_id)
                if (
                    delivery is not None
                    and delivery.state is DeliveryState.MERGED
                    and flow.state
                    not in {FlowState.FINALIZING, FlowState.BLOCKED, FlowState.ARCHIVED}
                ):
                    return False
                # Required dispatched effects must converge through the existing result engine.
                conn.execute("UPDATE execution_stops SET state='confirmed',confirmed_at=%s WHERE request_id=%s",
                             (tx.now(), intent.request_id))
                conn.execute("UPDATE jobs SET state='failed',last_error='execution stopped',updated_at=%s"
                             " WHERE id=%s AND result_received_at IS NULL", (tx.now(), intent.job_id))
                # Receipt commit only permits sending ACK. Inventory's exact settlement
                # witness releases this claim, never any sibling workspace on the worker.
                FlowStore(tx).try_archive(intent.flow_id)
                return True
