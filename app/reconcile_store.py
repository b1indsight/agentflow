"""Durable bounded scheduling and diagnostics for observations of tracked GitHub PRs."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from psycopg.rows import class_row
from pydantic import BaseModel

from app.github_models import FeedbackSource

if TYPE_CHECKING:
    from app.store import Store


RECONCILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_reconcile (
  flow_id BIGINT PRIMARY KEY REFERENCES flows(id),
  next_at TIMESTAMPTZ NOT NULL,
  last_success_at TIMESTAMPTZ,
  failures INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_reconcile_due ON flow_reconcile(next_at,flow_id);
CREATE TABLE IF NOT EXISTS feedback_reconcile (
  flow_id BIGINT NOT NULL REFERENCES flows(id),
  source TEXT NOT NULL,
  cursor TEXT,
  next_at TIMESTAMPTZ NOT NULL,
  last_success_at TIMESTAMPTZ,
  failures INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  PRIMARY KEY(flow_id,source)
);
CREATE INDEX IF NOT EXISTS ix_feedback_reconcile_due ON feedback_reconcile(next_at,flow_id);
"""


class ReconcileState(BaseModel):
    flow_id: int
    next_at: datetime
    last_success_at: datetime | None = None
    failures: int = 0
    last_error: str | None = None


class FeedbackCheck(ReconcileState):
    source: FeedbackSource
    cursor: str | None = None


class ReconcileStore:
    """Use the owning Store connection, including the caller's workflow transaction."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def feedback_seed(self, limit: int) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "INSERT INTO feedback_reconcile(flow_id,source,next_at)"
                " SELECT f.id,s.source,%s FROM flows f JOIN flow_deliveries d ON d.flow_id=f.id"
                " CROSS JOIN (VALUES ('issue_comments'),('pr_comments'),('reviews')) s(source)"
                " WHERE f.state='active' AND d.state='open' AND d.pr_number IS NOT NULL"
                " AND NOT EXISTS (SELECT 1 FROM feedback_reconcile r"
                " WHERE r.flow_id=f.id AND r.source=s.source) ORDER BY f.id,s.source"
                " LIMIT %s ON CONFLICT DO NOTHING", (self.store.now(), limit),
            )

    def feedback_due(self, limit: int) -> list[FeedbackCheck]:
        with self.store.connection() as connection:
            return connection.cursor(row_factory=class_row(FeedbackCheck)).execute(
                "SELECT r.* FROM feedback_reconcile r JOIN flows f ON f.id=r.flow_id"
                " JOIN flow_deliveries d ON d.flow_id=f.id WHERE r.next_at<=%s"
                " AND f.state='active' AND d.state='open' AND d.pr_number IS NOT NULL"
                " ORDER BY r.next_at,r.flow_id,r.source LIMIT %s", (self.store.now(), limit),
            ).fetchall()

    def feedback_get(self, flow_id: int, source: FeedbackSource) -> FeedbackCheck | None:
        with self.store.connection() as connection:
            return connection.cursor(row_factory=class_row(FeedbackCheck)).execute(
                "SELECT * FROM feedback_reconcile WHERE flow_id=%s AND source=%s",
                (flow_id, source),
            ).fetchone()

    def feedback_finish(
        self, check: FeedbackCheck, cursor: str | None, delay: float, diagnostic: str | None = None,
    ) -> None:
        now = self.store.now()
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE feedback_reconcile SET cursor=%s,next_at=%s,failures=0,last_error=%s,"
                " last_success_at=CASE WHEN %s THEN %s ELSE last_success_at END"
                " WHERE flow_id=%s AND source=%s",
                (cursor, now + timedelta(seconds=delay), diagnostic, cursor is None, now,
                 check.flow_id, check.source),
            )

    def feedback_failed(self, check: FeedbackCheck, delay: float, reason: str) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE feedback_reconcile SET next_at=%s,failures=failures+1,last_error=%s"
                " WHERE flow_id=%s AND source=%s",
                (self.store.now() + timedelta(seconds=delay), reason, check.flow_id, check.source),
            )

    def seed(self, limit: int) -> None:
        """Incrementally register existing tracked PRs; never infer new Flows from GitHub."""
        with self.store.connection() as connection:
            connection.execute(
                "INSERT INTO flow_reconcile(flow_id,next_at)"
                " SELECT f.id,%s FROM flows f JOIN flow_deliveries d ON d.flow_id=f.id"
                " WHERE f.state<>'succeeded' AND d.pr_number IS NOT NULL AND d.state<>'merged'"
                " AND NOT EXISTS (SELECT 1 FROM flow_reconcile r WHERE r.flow_id=f.id)"
                " ORDER BY f.id LIMIT %s ON CONFLICT DO NOTHING", (self.store.now(), limit),
            )

    def due(self, limit: int) -> list[ReconcileState]:
        with self.store.connection() as connection:
            return connection.cursor(row_factory=class_row(ReconcileState)).execute(
                "SELECT r.* FROM flow_reconcile r JOIN flows f ON f.id=r.flow_id"
                " JOIN flow_deliveries d ON d.flow_id=f.id WHERE r.next_at<=%s"
                " AND f.state<>'succeeded' AND d.pr_number IS NOT NULL AND d.state<>'merged'"
                " ORDER BY r.next_at,r.flow_id LIMIT %s", (self.store.now(), limit),
            ).fetchall()

    def get(self, flow_id: int) -> ReconcileState | None:
        with self.store.connection() as connection:
            return connection.cursor(row_factory=class_row(ReconcileState)).execute(
                "SELECT * FROM flow_reconcile WHERE flow_id=%s", (flow_id,),
            ).fetchone()

    def succeeded(self, flow_id: int, delay: float, diagnostic: str | None = None) -> None:
        now = self.store.now()
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE flow_reconcile SET last_success_at=%s,next_at=%s,failures=0,last_error=%s"
                " WHERE flow_id=%s", (now, now + timedelta(seconds=delay), diagnostic, flow_id),
            )

    def failed(self, flow_id: int, delay: float, reason: str) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE flow_reconcile SET next_at=%s,failures=failures+1,last_error=%s"
                " WHERE flow_id=%s", (self.store.now() + timedelta(seconds=delay), reason, flow_id),
            )

    def defer(self, flow_id: int) -> None:
        """A stale read is neither a successful observation nor a transport failure."""
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE flow_reconcile SET next_at=%s,last_error='workflow changed during query'"
                " WHERE flow_id=%s", (self.store.now() + timedelta(seconds=5), flow_id),
            )
