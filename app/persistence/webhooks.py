"""Durable webhook receipt, leased consumption and bounded retention."""

from __future__ import annotations

from datetime import datetime, timedelta
from random import uniform
from typing import Literal
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel

from app.persistence.repository import Repository


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


class WebhookStore(Repository):
    """Own durable receipt consumption and transaction-bound delivery audit deduplication."""

    def receive(self, delivery_id: str, event_type: str, payload: dict, digest: str) -> None:
        """Commit receipt before responding; duplicates never reset processing state."""
        now = self._db.now()
        with self._db.transaction() as tx, tx.connection() as conn:
            conn.execute("SET LOCAL statement_timeout = '2s'")
            # Allocate the sequence only after older receipts for this repository commit.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(COALESCE(%s::jsonb "
                "#>> '{repository,full_name}', %s), 1))", (Jsonb(payload), delivery_id),
            )
            conn.execute(
                "INSERT INTO events(delivery_id,event_type,payload,body_digest,received_at,next_at,"
                "inbox_position,state,attempts) VALUES (%s,%s,%s,%s,%s,%s,nextval('events_inbox_position_seq'),"
                "'pending',0) ON CONFLICT(delivery_id) DO UPDATE SET payload=excluded.payload,"
                "body_digest=excluded.body_digest,received_at=excluded.received_at,next_at=excluded.next_at,"
                "inbox_position=excluded.inbox_position,state='pending',attempts=0 "
                "WHERE events.inbox_position IS NULL AND events.event_type=excluded.event_type",
                (delivery_id, event_type, Jsonb(payload), digest, now, now),
            )
            row = conn.execute(
                "SELECT event_type,body_digest FROM events WHERE delivery_id=%s", (delivery_id,),
            ).fetchone()
            if row is None or (row["event_type"], row["body_digest"]) != (event_type, digest):
                raise DeliveryConflict("delivery identity conflicts with persisted receipt")

    def claim(self) -> Webhook | None:
        now = self._db.now()
        with self._db.connection() as conn:
            row = conn.execute(
                "WITH candidate AS (SELECT delivery_id FROM events head WHERE "
                "((state='pending' AND next_at<=%s) OR (state='processing' AND claim_expires_at<=%s)) "
                "AND NOT EXISTS (SELECT 1 FROM events older WHERE older.state != 'done' "
                "AND older.inbox_position < head.inbox_position AND "
                "older.payload #>> '{repository,full_name}' = head.payload #>> '{repository,full_name}') "
                "ORDER BY inbox_position FOR UPDATE SKIP LOCKED LIMIT 1) "
                "UPDATE events w SET state='processing',attempts=attempts+1,claim_token=%s,"
                "claim_expires_at=%s FROM candidate c WHERE w.delivery_id=c.delivery_id RETURNING w.*",
                (now, now, uuid4().hex, now + timedelta(seconds=60)),
            ).fetchone()
        return Webhook.model_validate(row) if row else None

    def lock(self, item: Webhook) -> bool:
        """Fence a claimed delivery for the complete business transaction."""
        with self._db.connection() as conn:
            return conn.execute(
                "SELECT delivery_id FROM events WHERE delivery_id=%s AND state='processing' "
                "AND claim_token=%s AND claim_expires_at>%s FOR UPDATE",
                (item.delivery_id, item.claim_token, self._db.now()),
            ).fetchone() is not None

    def complete(self, item: Webhook) -> bool:
        with self._db.connection() as conn:
            return conn.execute(
                "UPDATE events SET state='done',processed_at=%s,error_code=NULL,"
                "claim_token=NULL,claim_expires_at=NULL WHERE delivery_id=%s "
                "AND state='processing' AND claim_token=%s RETURNING delivery_id",
                (self._db.now(), item.delivery_id, item.claim_token),
            ).fetchone() is not None

    def fail(self, item: Webhook, error_code: str, *, permanent: bool = False) -> None:
        # Error codes are exception class names, never messages containing payload/secrets.
        blocked = permanent or item.attempts >= 10
        with self._db.connection() as conn:
            conn.execute(
                "UPDATE events SET state=%s,next_at=%s,error_code=%s,"
                "claim_token=NULL,claim_expires_at=NULL WHERE delivery_id=%s "
                "AND state='processing' AND claim_token=%s",
                ("blocked" if blocked else "pending",
                 self._db.now() + timedelta(seconds=min(60, uniform(0.8, 1.2) * 2 ** min(item.attempts - 1, 6))),
                 error_code, item.delivery_id, item.claim_token),
            )

    def list(self, limit: int = 50) -> list[Webhook]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE inbox_position IS NOT NULL ORDER BY received_at DESC,delivery_id LIMIT %s",
                (limit,),
            ).fetchall()
        return [Webhook.model_validate(row) for row in rows]

    def retry(self, delivery_id: str) -> bool:
        with self._db.connection() as conn:
            return conn.execute(
                "UPDATE events SET state='pending',attempts=0,next_at=%s,error_code=NULL "
                "WHERE delivery_id=%s AND state='blocked' AND payload IS NOT NULL RETURNING delivery_id",
                (self._db.now(), delivery_id),
            ).fetchone() is not None

    def cleanup(self) -> None:
        """Clear completed payloads after seven days, receipts after ninety; never pending data."""
        now = self._db.now()
        with self._db.transaction() as tx, tx.connection() as conn:
            conn.execute(
                "UPDATE events SET payload=NULL WHERE delivery_id IN "
                "(SELECT delivery_id FROM events WHERE state='done' AND processed_at<%s "
                "AND payload IS NOT NULL ORDER BY processed_at LIMIT 500 FOR UPDATE SKIP LOCKED)",
                (now - timedelta(days=7),),
            )
            conn.execute(
                "WITH expired AS (SELECT delivery_id,recorded_at FROM events WHERE state='done' AND processed_at<%s "
                "ORDER BY processed_at LIMIT 500 FOR UPDATE SKIP LOCKED), forgotten AS ("
                "DELETE FROM events USING expired x WHERE events.delivery_id=x.delivery_id AND x.recorded_at IS NULL) "
                "UPDATE events SET payload=NULL,inbox_position=NULL,body_digest=NULL,received_at=NULL,state=NULL,"
                "attempts=NULL,next_at=NULL,claim_token=NULL,claim_expires_at=NULL,processed_at=NULL,error_code=NULL "
                "FROM expired x WHERE events.delivery_id=x.delivery_id AND x.recorded_at IS NOT NULL",
                (now - timedelta(days=90),),
            )

    def event_seen(self, delivery_id: str) -> bool:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM events WHERE delivery_id=%s AND recorded_at IS NOT NULL", (delivery_id,)
            ).fetchone()
        return row is not None

    def record_event(
        self, delivery_id: str, event_type: str, flow_id: int | None, payload: dict
    ) -> bool:
        """Atomically reserve a delivery and report whether this caller won."""
        with self._db.connection() as connection:
            row = connection.execute(
                "INSERT INTO events(delivery_id, event_type, flow_id, payload, recorded_at)"
                " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (delivery_id) DO UPDATE"
                " SET recorded_at=excluded.recorded_at,flow_id=excluded.flow_id WHERE events.recorded_at IS NULL"
                " RETURNING delivery_id",
                (delivery_id, event_type, flow_id, Jsonb(payload), self._db.now()),
            ).fetchone()
        return row is not None
