"""Durable webhook receipt, leased consumption and bounded retention."""

from __future__ import annotations

from datetime import datetime, timedelta
from random import uniform
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel

if TYPE_CHECKING:
    from app.store import Store

WEBHOOK_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_inbox (
    delivery_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload JSONB,
    body_digest TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at TIMESTAMPTZ NOT NULL,
    claim_token TEXT,
    claim_expires_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS ix_webhook_pending ON webhook_inbox(state, next_at, received_at);
"""


class DeliveryConflict(ValueError):
    """The sender reused a delivery identity for different bytes or an event type."""


class Webhook(BaseModel):
    delivery_id: str
    event_type: str
    payload: dict | None
    body_digest: str
    received_at: datetime
    state: Literal["pending", "processing", "done", "blocked"]
    attempts: int
    next_at: datetime
    claim_token: str | None
    claim_expires_at: datetime | None
    processed_at: datetime | None
    error_code: str | None


class WebhookStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    def receive(self, delivery_id: str, event_type: str, payload: dict, digest: str) -> None:
        """Commit receipt before responding; duplicates never reset processing state."""
        now = self.store.now()
        with self.store.transaction() as tx, tx.connection() as conn:
            conn.execute("SET LOCAL statement_timeout = '2s'")
            conn.execute(
                "INSERT INTO webhook_inbox(delivery_id,event_type,payload,body_digest,received_at,next_at) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (delivery_id, event_type, Jsonb(payload), digest, now, now),
            )
            row = conn.execute(
                "SELECT event_type,body_digest FROM webhook_inbox WHERE delivery_id=%s", (delivery_id,),
            ).fetchone()
            if row is None or (row["event_type"], row["body_digest"]) != (event_type, digest):
                raise DeliveryConflict("delivery identity conflicts with persisted receipt")

    def claim(self) -> Webhook | None:
        now = self.store.now()
        with self.store.connection() as conn:
            row = conn.execute(
                "WITH candidate AS (SELECT delivery_id FROM webhook_inbox WHERE "
                "(state='pending' AND next_at<=%s) OR (state='processing' AND claim_expires_at<=%s) "
                "ORDER BY received_at,delivery_id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "UPDATE webhook_inbox w SET state='processing',attempts=attempts+1,claim_token=%s,"
                "claim_expires_at=%s FROM candidate c WHERE w.delivery_id=c.delivery_id RETURNING w.*",
                (now, now, uuid4().hex, now + timedelta(seconds=60)),
            ).fetchone()
        return Webhook.model_validate(row) if row else None

    def lock(self, item: Webhook) -> bool:
        """Fence a claimed delivery for the complete business transaction."""
        with self.store.connection() as conn:
            return conn.execute(
                "SELECT delivery_id FROM webhook_inbox WHERE delivery_id=%s AND state='processing' "
                "AND claim_token=%s AND claim_expires_at>%s FOR UPDATE",
                (item.delivery_id, item.claim_token, self.store.now()),
            ).fetchone() is not None

    def complete(self, item: Webhook) -> bool:
        with self.store.connection() as conn:
            return conn.execute(
                "UPDATE webhook_inbox SET state='done',processed_at=%s,error_code=NULL,"
                "claim_token=NULL,claim_expires_at=NULL WHERE delivery_id=%s "
                "AND state='processing' AND claim_token=%s RETURNING delivery_id",
                (self.store.now(), item.delivery_id, item.claim_token),
            ).fetchone() is not None

    def fail(self, item: Webhook, error_code: str, *, permanent: bool = False) -> None:
        # Error codes are exception class names, never messages containing payload/secrets.
        blocked = permanent or item.attempts >= 10
        with self.store.connection() as conn:
            conn.execute(
                "UPDATE webhook_inbox SET state=%s,next_at=%s,error_code=%s,"
                "claim_token=NULL,claim_expires_at=NULL WHERE delivery_id=%s "
                "AND state='processing' AND claim_token=%s",
                ("blocked" if blocked else "pending",
                 self.store.now() + timedelta(seconds=min(60, uniform(0.8, 1.2) * 2 ** min(item.attempts - 1, 6))),
                 error_code, item.delivery_id, item.claim_token),
            )

    def list(self, limit: int = 50) -> list[Webhook]:
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM webhook_inbox ORDER BY received_at DESC,delivery_id LIMIT %s", (limit,),
            ).fetchall()
        return [Webhook.model_validate(row) for row in rows]

    def retry(self, delivery_id: str) -> bool:
        with self.store.connection() as conn:
            return conn.execute(
                "UPDATE webhook_inbox SET state='pending',attempts=0,next_at=%s,error_code=NULL "
                "WHERE delivery_id=%s AND state='blocked' AND payload IS NOT NULL RETURNING delivery_id",
                (self.store.now(), delivery_id),
            ).fetchone() is not None

    def cleanup(self) -> None:
        """Clear completed payloads after seven days, receipts after ninety; never pending data."""
        now = self.store.now()
        with self.store.transaction() as tx, tx.connection() as conn:
            rows = conn.execute(
                "UPDATE webhook_inbox SET payload=NULL WHERE delivery_id IN "
                "(SELECT delivery_id FROM webhook_inbox WHERE state='done' AND processed_at<%s "
                "AND payload IS NOT NULL ORDER BY processed_at LIMIT 500) RETURNING delivery_id",
                (now - timedelta(days=7),),
            ).fetchall()
            if rows:
                conn.execute("UPDATE events SET payload='{}'::jsonb WHERE delivery_id=ANY(%s)",
                             ([row["delivery_id"] for row in rows],))
            conn.execute(
                "DELETE FROM webhook_inbox WHERE delivery_id IN (SELECT delivery_id FROM webhook_inbox "
                "WHERE state='done' AND processed_at<%s ORDER BY processed_at LIMIT 500)",
                (now - timedelta(days=90),),
            )
