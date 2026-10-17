"""Durable bounded source claims, retries and checkpoints for the console archive."""

from datetime import datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel

from app.github.console import Observation, webhook_observations
from app.persistence.activity import ActivityStore
from app.persistence.repository import Repository


class SyncClaim(BaseModel):
    flow_id: int
    source: str
    cursor: str | None
    claim_token: str
    failures: int
    repo: str
    issue_number: int
    pr_number: int | None
    installation_id: int | None
    created_at: datetime


class ConsoleSyncStore(Repository):
    """A source page and its checkpoint commit together; a crashed claim can be reclaimed."""

    def claim(self) -> SyncClaim | None:
        with self._db.transaction() as tx, tx.connection() as connection:
            row = connection.execute(
                "SELECT s.flow_id,s.source,s.cursor,s.failures,f.repo,f.issue_number,"
                "d.pr_number,f.installation_id,f.created_at FROM console_sync s JOIN flows f ON f.id=s.flow_id"
                " LEFT JOIN flow_deliveries d ON d.flow_id=f.id WHERE s.pending AND s.next_at<=%s"
                " AND (s.source='worker' OR NOT EXISTS (SELECT 1 FROM console_installation_backoff b"
                " WHERE b.installation_id=f.installation_id AND b.until_at>%s))"
                " AND (s.claim_until IS NULL OR s.claim_until<%s) ORDER BY s.next_at,s.flow_id,s.source"
                " LIMIT 1 FOR UPDATE OF s SKIP LOCKED",
                (tx.now(), tx.now(), tx.now()),
            ).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            connection.execute(
                "UPDATE console_sync SET claim_token=%s,claim_until=%s,status='syncing'"
                " WHERE flow_id=%s AND source=%s",
                (token, tx.now() + timedelta(seconds=60), row["flow_id"], row["source"]),
            )
        return SyncClaim(**row, claim_token=token)

    def finish(
        self,
        claim: SyncClaim,
        items: list[Observation],
        *,
        cursor: str | None,
        more: bool,
        limitations: list[str],
        children: tuple[str, ...] | list[str] = (),
    ) -> bool:
        with self._db.transaction() as tx, tx.connection() as connection:
            # Always lock publication before source bookkeeping (also used by fact triggers).
            connection.execute(
                "SELECT flow_id FROM console_flows WHERE flow_id=%s FOR UPDATE", (claim.flow_id,)
            )
            source = connection.execute(
                "SELECT dirty FROM console_sync WHERE flow_id=%s AND source=%s"
                " AND claim_token=%s FOR UPDATE",
                (claim.flow_id, claim.source, claim.claim_token),
            ).fetchone()
            if source is None:
                return False
            for item in items:
                ActivityStore(tx).append(claim.flow_id, item)
            for child in children:
                connection.execute(
                    "INSERT INTO console_sync(flow_id,source) VALUES(%s,%s) ON CONFLICT(flow_id,source)"
                    " DO UPDATE SET dirty=console_sync.pending,pending=TRUE,cursor=CASE WHEN console_sync.pending THEN console_sync.cursor ELSE NULL END,"
                    " next_at=CASE WHEN console_sync.pending THEN console_sync.next_at ELSE %s END,"
                    " status=CASE WHEN console_sync.pending THEN console_sync.status ELSE 'pending' END",
                    (claim.flow_id, child, tx.now()),
                )
            repeat = not more and source["dirty"]
            next_cursor = None if repeat and claim.source != "worker" else cursor
            connection.execute(
                "UPDATE console_sync SET cursor=%s,pending=%s,dirty=CASE WHEN %s THEN dirty ELSE FALSE END,"
                "claim_token=NULL,claim_until=NULL,failures=0,reason=NULL,last_success_at=%s,"
                "next_at=%s,status=%s,limitations=%s WHERE flow_id=%s AND source=%s",
                (
                    next_cursor,
                    more or repeat,
                    more,
                    tx.now(),
                    tx.now() + timedelta(milliseconds=100),
                    "pending" if more or repeat else "partial" if limitations else "available",
                    Jsonb(limitations),
                    claim.flow_id,
                    claim.source,
                ),
            )
        return True

    def fail(
        self, claim: SyncClaim, code: str, *, delay: float | None, reset_cursor: bool = False
    ) -> None:
        with self._db.transaction() as tx, tx.connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM console_sync WHERE flow_id=%s AND source=%s AND claim_token=%s FOR UPDATE",
                    (claim.flow_id, claim.source, claim.claim_token),
                ).fetchone()
                is None
            ):
                return
            if code == "rate_limited" and delay is not None and claim.installation_id is not None:
                connection.execute(
                    "INSERT INTO console_installation_backoff(installation_id,until_at) VALUES(%s,%s)"
                    " ON CONFLICT(installation_id) DO UPDATE SET until_at=greatest("
                    " console_installation_backoff.until_at,excluded.until_at)",
                    (claim.installation_id, tx.now() + timedelta(seconds=delay)),
                )
            connection.execute(
                "UPDATE console_sync SET pending=%s,status='unavailable',reason=%s,failures=failures+1,"
                "cursor=CASE WHEN %s THEN NULL ELSE cursor END,claim_token=NULL,claim_until=NULL,next_at=%s"
                " WHERE flow_id=%s AND source=%s AND claim_token=%s",
                (
                    delay is not None,
                    code,
                    reset_cursor,
                    tx.now() + timedelta(seconds=delay or 0),
                    claim.flow_id,
                    claim.source,
                    claim.claim_token,
                ),
            )

    def observe_webhook(self, event_type: str, payload: dict) -> None:
        """Archive explicit observed versions inside the webhook's business transaction."""
        repo = payload.get("repository", {}).get("full_name")
        if not isinstance(repo, str):
            return
        issue = payload.get("issue") or payload.get("pull_request") or {}
        number = issue.get("number")
        sha = (payload.get("check_run") or payload.get("check_suite") or {}).get(
            "head_sha"
        ) or payload.get("sha")
        check = payload.get("check_run") or payload.get("check_suite") or {}
        pull_numbers = [
            p["number"] for p in check.get("pull_requests", []) if isinstance(p.get("number"), int)
        ]
        with self._db.connection() as connection:
            flows = connection.execute(
                "SELECT f.id FROM flows f LEFT JOIN flow_deliveries d ON d.flow_id=f.id"
                " WHERE lower(f.repo)=lower(%s) AND (f.issue_number=%s OR d.pr_number=%s"
                " OR d.pr_number=ANY(%s) OR (%s::text IS NOT NULL AND d.head_sha=%s)) ORDER BY f.id",
                (repo, number, number, pull_numbers, sha, sha),
            ).fetchall()
        observations = webhook_observations(event_type, payload)
        for flow in flows:
            for item in observations:
                ActivityStore(self._db).append(flow["id"], item)
            with self._db.connection() as connection:
                connection.execute("SELECT console_enqueue(%s)", (flow["id"],))
