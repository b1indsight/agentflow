"""Durable bounded source claims, retries and checkpoints for the console archive."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app.contracts.conversation import SessionHistory
from app.github.console import Observation, webhook_observations
from app.persistence.activity import ActivityStore
from app.persistence.repository import Repository

_WORKER_AVAILABILITY = ("no_worker", "no_session", "not_found", "unsupported")


class SyncClaim(BaseModel):
    """Pre-I/O ownership fence; worker records still resolve their own historical Attempts."""

    flow_id: int
    round_no: int = Field(strict=True, gt=0)
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

    def reset_round(self, flow_id: int) -> None:
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
                "f.current_round_no AS round_no,d.pr_number,f.installation_id,f.created_at"
                " FROM console_sync s JOIN flows f ON f.id=s.flow_id"
                " LEFT JOIN flow_deliveries d ON d.flow_id=f.id WHERE s.pending AND s.next_at<=%s"
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
            # Always lock publication before source bookkeeping (also used by fact triggers).
            publication = connection.execute(
                "SELECT facts FROM console_flows WHERE flow_id=%s FOR UPDATE", (claim.flow_id,)
            ).fetchone()
            source = connection.execute(
                "SELECT dirty,limitations FROM console_sync WHERE flow_id=%s AND source=%s"
                " AND claim_token=%s FOR UPDATE",
                (claim.flow_id, claim.source, claim.claim_token),
            ).fetchone()
            if source is None or publication is None:
                return False
            # The publication fence sees committed resume/PR changes without taking a
            # Flow lock after it. Worker history intentionally spans retired rounds.
            if claim.source != "worker" and (
                publication["facts"]["current_round_no"] != claim.round_no
                or publication["facts"]["pr_number"] != claim.pr_number
            ):
                return False
            rounds: dict[int, int] = {}
            if claim.source == "worker" and items:
                attempt_ids: list[int] = []
                for item in items:
                    identity = item.metadata.get("attempt_id")
                    if type(identity) is not int or identity <= 0:
                        raise ValueError("missing worker Attempt identity")
                    attempt_ids.append(identity)
                rounds = {
                    row["id"]: row["round_no"]
                    for row in connection.execute(
                        "SELECT id,round_no FROM attempts WHERE flow_id=%s AND id=ANY(%s)",
                        (claim.flow_id, attempt_ids),
                    ).fetchall()
                }
                if any(identity not in rounds for identity in attempt_ids):
                    raise ValueError("worker Attempt does not belong to Flow")
            round_no: int | None
            for item in items:
                if claim.source == "worker":
                    attempt_id = item.metadata["attempt_id"]
                    assert isinstance(attempt_id, int)
                    round_no = rounds[attempt_id]
                else:
                    # Issue content and SHA/comment-ID child sources can be shared.
                    # Only sources explicitly bound to this PR prove round ownership.
                    round_no = claim.round_no if claim.pr_number is not None and (
                        claim.source.startswith(("pr.", "inline.reactions:", "review.reactions:"))
                    ) and item.kind not in {"commit", "check", "status"} else None
                ActivityStore(tx).append(claim.flow_id, item.model_copy(update={"round_no": round_no}))
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
                "SELECT f.id,f.current_round_no,d.pr_number FROM flows f"
                " LEFT JOIN flow_deliveries d ON d.flow_id=f.id"
                " WHERE lower(f.repo)=lower(%s) AND (f.issue_number=%s"
                " OR d.pr_number=ANY(%s) OR (%s::text IS NOT NULL AND d.head_sha=%s)) ORDER BY f.id",
                (repo, issue_number, sorted(pull_numbers), sha, sha),
            ).fetchall()
            owners: dict[int, set[int | None]] = {
                flow["id"]: {flow["current_round_no"] if flow["pr_number"] in pull_numbers else None}
                for flow in flows
            }
            archive = ActivityStore(tx)
            for pr_number in sorted(pull_numbers):
                owner = archive.archived_pr(repo, pr_number)
                if owner is not None:
                    flow_id, archived_round_no = owner
                    owners.setdefault(flow_id, set()).add(archived_round_no)
            for flow_id, rounds in sorted(owners.items()):
                # A SHA alone, or a check shared by multiple rounds, proves no one round.
                round_no = next(iter(rounds)) if len(rounds) == 1 else None
                for item in observations:
                    archive.append(flow_id, item.model_copy(update={
                        "round_no": None if item.kind in {"commit", "check", "status"} else round_no,
                    }))
                connection.execute("SELECT console_enqueue(%s)", (flow_id,))
