"""Durable bounded source claims, retries and checkpoints for the console archive."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app.contracts.conversation import SessionHistory
from app.github.console import Observation, webhook_observations
from app.persistence.activity import ActivityStore
from app.persistence.records import Row
from app.persistence.repository import Repository

_WORKER_AVAILABILITY = ("no_worker", "no_session", "not_found", "unsupported")


class SyncClaim(BaseModel):
    """Pre-I/O ownership fence; worker records still resolve their own historical Attempts."""

    flow_id: int
    attempt_id: int = Field(strict=True, gt=0)
    source: str
    cursor: str | None
    claim_token: str
    failures: int
    repo: str
    issue_number: int | None
    pr_number: int | None
    installation_id: int | None
    created_at: datetime


class ConsoleSyncStore(Repository):
    """A source page and its checkpoint commit together; a crashed claim can be reclaimed."""

    def reset_attempt(self, flow_id: int) -> None:
        """Retire old remote claims, preserving append-only activity and worker coverage."""
        with self._db.connection() as connection:
            # Match finish(): publication before source bookkeeping.
            connection.execute(
                "SELECT flow_id FROM console_flows WHERE flow_id=%s FOR UPDATE", (flow_id,)
            )
            connection.execute(
                "DELETE FROM console_sync WHERE flow_id=%s"
                " AND source<>'worker' AND source NOT LIKE 'worker:%%'", (flow_id,),
            )

    def claim(self) -> SyncClaim | None:
        with self._db.transaction() as tx, tx.connection() as connection:
            row = connection.execute(
                "SELECT s.flow_id,s.source,CASE WHEN s.source='worker' AND s.limitations ?| %s"
                " THEN NULL ELSE s.cursor END AS cursor,s.failures,f.repo,f.issue_number,"
                "a.id AS attempt_id,a.delivery_pr_number AS pr_number,f.installation_id,f.created_at"
                " FROM console_sync s JOIN flows f ON f.id=s.flow_id"
                " JOIN attempts a ON a.id=f.current_attempt_id"
                " WHERE NOT f.history_only AND s.pending AND s.next_at<=%s"
                " AND (s.source='worker' OR NOT EXISTS (SELECT 1 FROM console_installation_backoff b"
                " WHERE b.installation_id=f.installation_id AND b.until_at>%s))"
                " AND (s.claim_until IS NULL OR s.claim_until<%s) ORDER BY s.next_at,s.flow_id,s.source"
                " LIMIT 1 FOR UPDATE OF s SKIP LOCKED",
                (list(_WORKER_AVAILABILITY), tx.now(), tx.now(), tx.now()),
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

    def remote_read_allowed(self, claim: SyncClaim) -> bool:
        """A queued page can lose its claim or PR while waiting for an I/O slot."""
        with self._db.connection() as connection:
            return connection.execute(
                "SELECT 1 FROM console_sync s JOIN flows f ON f.id=s.flow_id"
                " JOIN attempts a ON a.id=f.current_attempt_id"
                " WHERE s.flow_id=%s AND s.source=%s AND s.claim_token=%s AND s.pending"
                " AND NOT f.history_only AND a.id=%s AND a.delivery_pr_number IS NOT DISTINCT FROM %s",
                (claim.flow_id, claim.source, claim.claim_token, claim.attempt_id, claim.pr_number),
            ).fetchone() is not None

    @staticmethod
    def _owns_publication(connection: Connection[Row], claim: SyncClaim) -> bool:
        """Lock publication before source rows; worker pages span historical Attempts."""
        row = connection.execute(
            "SELECT facts FROM console_flows WHERE flow_id=%s FOR UPDATE", (claim.flow_id,)
        ).fetchone()
        return row is not None and not row["facts"]["history_only"] and (claim.source == "worker" or (
            row["facts"]["current_attempt_id"] == claim.attempt_id
            and row["facts"]["pr_number"] == claim.pr_number
        ))

    def finish(
        self,
        claim: SyncClaim,
        items: list[Observation],
        *,
        cursor: str | None,
        more: bool,
        limitations: list[str],
        unavailable_reason: str | None = None,
        sessions: Sequence[SessionHistory] = (),
        children: tuple[str, ...] | list[str] = (),
    ) -> bool:
        """Publish a claimed page atomically, rejecting retired remote ownership before any writes."""
        with self._db.transaction() as tx, tx.connection() as connection:
            if not self._owns_publication(connection, claim):
                return False
            source = connection.execute(
                "SELECT dirty,limitations FROM console_sync WHERE flow_id=%s AND source=%s"
                " AND claim_token=%s FOR UPDATE",
                (claim.flow_id, claim.source, claim.claim_token),
            ).fetchone()
            if source is None:
                return False
            if claim.source == "worker" and (items or sessions):
                attempt_ids = {session.attempt_id for session in sessions}
                for item in items:
                    if item.attempt_id is None:
                        raise ValueError("missing worker Attempt identity")
                    attempt_ids.add(item.attempt_id)
                owned = {
                    row["id"]
                    for row in connection.execute(
                        "SELECT id FROM attempts WHERE flow_id=%s AND id=ANY(%s)",
                        (claim.flow_id, list(attempt_ids)),
                    ).fetchall()
                }
                if attempt_ids != owned:
                    raise ValueError("worker Attempt does not belong to Flow")
            for item in items:
                if claim.source != "worker":
                    # Issue content and SHA/comment-ID child sources can be shared.
                    # Only sources explicitly bound to this PR prove Attempt ownership.
                    attempt_id = claim.attempt_id if claim.pr_number is not None and (
                        claim.source.startswith(("pr.", "inline.reactions:", "review.reactions:"))
                    ) and item.kind not in {"commit", "check", "status"} else None
                    item = item.model_copy(update={"attempt_id": attempt_id})
                ActivityStore(tx).append(claim.flow_id, item)
            # These child rows are current per-session coverage, never queued work.
            # Only a complete clean scan from that session's beginning repairs a gap.
            for session in sessions:
                child_source = (
                    f"worker:{session.attempt_id}:{session.session_generation}:{session.session_id}"
                )
                previous = connection.execute(
                    "SELECT reason,worker_scan_clean FROM console_sync"
                    " WHERE flow_id=%s AND source=%s FOR UPDATE",
                    (claim.flow_id, child_source),
                ).fetchone()
                scan_clean = session.status == "available" and (
                    session.starts_at_beginning
                    or (previous is not None and previous["worker_scan_clean"])
                )
                reason = previous["reason"] if previous is not None else None
                if session.status != "available":
                    reason = session.status
                elif session.reached_end and scan_clean:
                    reason = None
                available = reason is None
                connection.execute(
                    "INSERT INTO console_sync(flow_id,source,pending,status,reason,last_success_at,worker_scan_clean)"
                    " VALUES(%s,%s,FALSE,%s,%s,%s,%s) ON CONFLICT(flow_id,source) DO UPDATE"
                    " SET status=excluded.status,reason=excluded.reason,"
                    " worker_scan_clean=excluded.worker_scan_clean,"
                    " last_success_at=coalesce(excluded.last_success_at,console_sync.last_success_at)",
                    (
                        claim.flow_id,
                        child_source,
                        "available" if available else "partial",
                        reason,
                        tx.now() if available else None,
                        scan_clean and not session.reached_end,
                    ),
                )
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
            previous_limitations = set(source["limitations"])
            if claim.source == "worker":
                # claim() restarts old, unscoped availability flags at the first session.
                previous_limitations.difference_update(_WORKER_AVAILABILITY)
                if (
                    not more
                    and connection.execute(
                        "SELECT 1 FROM console_sync WHERE flow_id=%s AND source LIKE %s"
                        " AND status!='available' LIMIT 1",
                        (claim.flow_id, "worker:%"),
                    ).fetchone()
                ):
                    # A later clean session cannot repair an earlier missing one.
                    # The next event/explicit refresh retries the complete session pass.
                    next_cursor = None
            # A clean page or incremental EOF does not repair earlier coverage gaps.
            limitations = sorted(previous_limitations | set(limitations))
            # Availability describes this read only; it never becomes a coverage gap.
            if more or repeat:
                status = "pending"
            elif unavailable_reason is not None:
                status = "unavailable"
            else:
                status = "partial" if limitations else "available"
            connection.execute(
                "UPDATE console_sync SET cursor=%s,pending=%s,dirty=CASE WHEN %s THEN dirty ELSE FALSE END,"
                "claim_token=NULL,claim_until=NULL,failures=0,reason=%s,last_success_at=coalesce(%s,last_success_at),"
                "next_at=%s,status=%s,limitations=%s WHERE flow_id=%s AND source=%s",
                (
                    next_cursor,
                    more or repeat,
                    more,
                    unavailable_reason,
                    tx.now() if unavailable_reason is None else None,
                    tx.now() + timedelta(milliseconds=100),
                    status,
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
            if not self._owns_publication(connection, claim):
                return
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
        if type(number) is not int or number <= 0:
            number = None
        is_pr = event_type.startswith("pull_request") or "pull_request" in issue
        issue_number = None if is_pr else number
        sha = (payload.get("check_run") or payload.get("check_suite") or {}).get(
            "head_sha"
        ) or payload.get("sha")
        check = payload.get("check_run") or payload.get("check_suite") or {}
        pull_numbers = {
            p["number"] for p in check.get("pull_requests", [])
            if type(p.get("number")) is int and p["number"] > 0
        }
        if is_pr and number is not None:
            pull_numbers.add(number)
        observations = webhook_observations(event_type, payload)
        with self._db.transaction() as tx, tx.connection() as connection:
            flows = connection.execute(
                "SELECT f.id,a.id AS attempt_id,a.delivery_pr_number AS pr_number FROM flows f"
                " JOIN attempts a ON a.flow_id=f.id"
                " WHERE NOT f.history_only AND lower(f.repo)=lower(%s)"
                " AND ((f.issue_number=%s AND a.id=f.current_attempt_id)"
                " OR a.delivery_pr_number=ANY(%s) OR a.delivery_head_sha=%s) ORDER BY f.id",
                (repo, issue_number, sorted(pull_numbers), None if pull_numbers else sha),
            ).fetchall()
            owners: dict[int, set[int | None]] = {}
            matching_attempts: dict[int, set[int]] = {}
            for flow in flows:
                matching_attempts.setdefault(flow["id"], set()).add(flow["attempt_id"])
                owners.setdefault(flow["id"], set()).add(
                    flow["attempt_id"] if flow["pr_number"] in pull_numbers else None
                )
            archive = ActivityStore(tx)
            for flow_id, attempts in sorted(owners.items()):
                # A SHA alone, or a check shared by multiple Attempts, proves no one owner.
                attempt_id = next(iter(attempts)) if len(attempts) == 1 else None
                for item in observations:
                    archive.append(flow_id, item.model_copy(update={
                        "attempt_id": None if item.kind in {"commit", "check", "status"} else attempt_id,
                    }))
                # A retired PR can append history, not schedule reads against its replacement.
                publication = connection.execute(
                    "SELECT (facts->>'current_attempt_id')::bigint AS owner FROM console_flows"
                    " WHERE flow_id=%s FOR UPDATE", (flow_id,),
                ).fetchone()
                if publication and publication["owner"] in matching_attempts.get(flow_id, set()):
                    connection.execute("SELECT console_enqueue(%s)", (flow_id,))
