"""Durable cancellation fences and worker allocation drain state."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from psycopg.rows import class_row
from pydantic import BaseModel

from app.contracts.protocol import ExecutionStopped, StopExecution
from app.domain.models import DeliveryState, FlowState

if TYPE_CHECKING:
    from app.persistence.store import Store

STOP_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_stops (
 request_id TEXT PRIMARY KEY, flow_id BIGINT NOT NULL REFERENCES flows(id),
 job_id BIGINT NOT NULL REFERENCES jobs(id), worker_id TEXT NOT NULL REFERENCES workers(worker_id),
 assignment_token TEXT NOT NULL, binding_at TIMESTAMPTZ NOT NULL,
 reason TEXT NOT NULL DEFAULT 'cancelled', requested_at TIMESTAMPTZ NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', confirmed_at TIMESTAMPTZ, receipt_digest TEXT,
 UNIQUE(flow_id,binding_at)
);
CREATE INDEX IF NOT EXISTS ix_stops_worker ON execution_stops(worker_id) WHERE state!='confirmed';
"""


class StopIntent(BaseModel):
    request_id: str
    flow_id: int
    job_id: int
    worker_id: str
    assignment_token: str
    binding_at: datetime
    reason: str
    requested_at: datetime
    state: str
    confirmed_at: datetime | None
    receipt_digest: str | None

    def message(self, epoch: str) -> StopExecution:
        return StopExecution(request_id=self.request_id, flow_id=self.flow_id, job_id=self.job_id,
                             assignment_token=self.assignment_token, connection_epoch=epoch)


class StopStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    def request(self, flow_id: int) -> None:
        """Called under the workflow lock before attempting to release the binding."""
        binding = self.store.get_flow_execution(flow_id)
        if binding is None or binding.released_at is not None:
            return
        with self.store.connection() as conn:
            job = conn.execute(
                "SELECT id,assignment_token FROM jobs WHERE flow_id=%s AND assigned_worker_id=%s"
                " AND (started_at IS NOT NULL OR dispatch_authorized_at IS NOT NULL)"
                " AND assignment_token IS NOT NULL AND (state='running'"
                " OR ((started_at IS NOT NULL OR dispatch_authorized_at IS NOT NULL) AND result_received_at IS NULL)"
                " OR (result_received_at IS NOT NULL AND result_applied_at IS NULL)"
                " OR EXISTS (SELECT 1 FROM result_effects e WHERE e.job_id=jobs.id"
                " AND e.dispatched_at IS NOT NULL AND e.receipt IS NULL))"
                " ORDER BY id DESC LIMIT 1 FOR UPDATE", (flow_id, binding.worker_id),
            ).fetchone()
            if job is not None:
                conn.execute(
                    "INSERT INTO execution_stops(request_id,flow_id,job_id,worker_id,assignment_token,"
                    " binding_at,requested_at) VALUES(%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT(flow_id,binding_at) DO NOTHING",
                    (uuid4().hex, flow_id, job['id'], binding.worker_id, job['assignment_token'],
                     binding.bound_at, self.store.now()),
                )

    def authorize_dispatch(self, job_id: int, worker_id: str, token: str, epoch: str) -> bool:
        """Persist the point after which a lost Started ACK cannot mean 'never sent'."""
        job = self.store.get_job(job_id)
        if job is None:
            return False
        with self.store.transaction() as tx:
            flow = tx.lock_workflow(job.flow_id)
            if flow is None or flow.state is not FlowState.ACTIVE:
                return False
            delivery = tx.get_delivery(job.flow_id)
            if delivery is not None and (delivery.state is DeliveryState.CLOSED or delivery.supersedes(job.kind)):
                return False
            if not tx.assignment_active(job_id, worker_id, token, epoch):
                return False
            with tx.connection() as conn:
                conn.execute("UPDATE jobs SET dispatch_authorized_at=COALESCE(dispatch_authorized_at,%s) WHERE id=%s",
                             (tx.now(), job_id))
            return True

    def has_pending(self, flow_id: int) -> bool:
        with self.store.connection() as conn:
            return conn.execute("SELECT 1 FROM execution_stops WHERE flow_id=%s AND state!='confirmed' LIMIT 1",
                                (flow_id,)).fetchone() is not None

    def pending(self, worker_id: str) -> StopIntent | None:
        with self.store.connection() as conn:
            return conn.cursor(row_factory=class_row(StopIntent)).execute(
                "SELECT * FROM execution_stops WHERE worker_id=%s AND state!='confirmed'"
                " ORDER BY requested_at LIMIT 1", (worker_id,),
            ).fetchone()

    def receipt_request(self, worker_id: str, request_id: str) -> StopIntent | None:
        with self.store.connection() as conn:
            return conn.cursor(row_factory=class_row(StopIntent)).execute(
                "SELECT * FROM execution_stops WHERE worker_id=%s AND request_id=%s",
                (worker_id, request_id),
            ).fetchone()

    def confirm(self, worker_id: str, receipt: ExecutionStopped) -> bool:
        """A stopped executor alone cannot release a binding with unapplied external facts."""
        with self.store.transaction() as tx:
            flow = tx.lock_workflow(receipt.flow_id)
            if flow is None:
                return False
            with tx.connection() as conn:
                intent = conn.cursor(row_factory=class_row(StopIntent)).execute(
                    "SELECT * FROM execution_stops WHERE request_id=%s FOR UPDATE", (receipt.request_id,),
                ).fetchone()
                binding = tx.get_flow_execution(receipt.flow_id)
                worker = conn.execute("SELECT connection_epoch FROM workers WHERE worker_id=%s", (worker_id,)).fetchone()
                if (intent is None or worker is None
                        or worker['connection_epoch'] != receipt.connection_epoch
                        or intent.worker_id != worker_id or intent.job_id != receipt.job_id
                        or intent.flow_id != receipt.flow_id or intent.assignment_token != receipt.assignment_token):
                    return False
                digest = hashlib.sha256(receipt.model_dump_json(exclude={'connection_epoch'}).encode()).hexdigest()
                if intent.receipt_digest is not None and intent.receipt_digest != digest:
                    return False
                if intent.state == 'confirmed':
                    return intent.receipt_digest == digest
                if binding is None or binding.worker_id != worker_id or binding.bound_at != intent.binding_at:
                    return False
                conn.execute("UPDATE execution_stops SET state='stopped',receipt_digest=%s WHERE request_id=%s",
                             (digest, intent.request_id))
                if tx.has_pending_result(receipt.flow_id) or conn.execute(
                        "SELECT 1 FROM result_effects e JOIN jobs j ON j.id=e.job_id"
                        " WHERE j.flow_id=%s AND e.dispatched_at IS NOT NULL AND e.receipt IS NULL LIMIT 1",
                        (receipt.flow_id,),
                ).fetchone() is not None:
                    return False
                delivery = tx.get_delivery(intent.flow_id)
                if (delivery is not None and delivery.state is DeliveryState.MERGED
                        and flow.state not in {FlowState.ACTIVE, FlowState.SUCCEEDED}):
                    return False
                # Any previously dispatched effect must converge through the existing result engine.
                conn.execute("UPDATE execution_stops SET state='confirmed',confirmed_at=%s WHERE request_id=%s",
                             (tx.now(), intent.request_id))
                conn.execute("UPDATE jobs SET state='failed',last_error='execution stopped',updated_at=%s"
                             " WHERE id=%s AND result_received_at IS NULL", (tx.now(), intent.job_id))
                if delivery is None or delivery.state is not DeliveryState.MERGED:
                    tx.release_flow_execution(intent.flow_id)
                conn.execute("UPDATE workers SET current_job_id=NULL,status='online' WHERE worker_id=%s"
                             " AND (current_job_id=%s OR current_job_id IS NULL)", (worker_id, intent.job_id))
                return True

    def drain(self, worker_id: str, enabled: bool) -> None:
        with self.store.connection() as conn:
            result = conn.execute("UPDATE workers SET draining=%s WHERE worker_id=%s", (enabled, worker_id))
            if result.rowcount != 1:
                raise ValueError('unknown worker')
