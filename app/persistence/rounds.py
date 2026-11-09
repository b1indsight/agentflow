"""Historical round evidence and explicitly authorized archive corrections."""

from __future__ import annotations

from datetime import timedelta

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.domain.models import ArchiveReason, FlowRound, FlowState
from app.github import PullRequestMerge
from app.persistence.records import required
from app.persistence.repository import Repository


class RoundStore(Repository):
    """Keep historical conclusions separate from current execution authority."""

    def by_pr(self, repo: str, pr_number: int) -> FlowRound | None:
        """Find the archived round owning this repository-scoped PR identity."""
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(FlowRound)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM flow_rounds WHERE repo=%s AND pr_number=%s",
                (repo, pr_number),
            ).fetchone()

    def observe_merge(self, flow_id: int, round_no: int, merge: PullRequestMerge) -> bool:
        """Save verified merge facts, returning False only for an identical observation.

        The caller authenticates repository/installation provenance. This boundary
        still rejects a foreign PR or conflicting evidence; arrival time never
        substitutes for GitHub's actual merge time or authorizes new execution.
        """
        with self._db.transaction() as tx, tx.connection() as connection:
            flow = tx.lock_workflow(flow_id)
            if flow is None:
                raise ValueError("Flow does not exist")
            with connection.cursor(row_factory=class_row(FlowRound)) as cursor:
                round = cursor.execute(
                    "SELECT * FROM flow_rounds WHERE flow_id=%s AND round_no=%s FOR UPDATE",
                    (flow_id, round_no),
                ).fetchone()
            if round is None or round_no > flow.current_round_no:
                raise ValueError("archived round does not exist")
            if round.repo != flow.repo or round.pr_number != merge.number:
                raise ValueError("merge does not belong to archived round PR")
            if (round.merged_at is not None and round.merged_at != merge.merged_at) or (
                round.merge_commit_sha is not None
                and round.merge_commit_sha != merge.merge_commit_sha
            ):
                raise ValueError("merge event conflicts with recorded round evidence")
            if round.merged_at is not None and round.merge_commit_sha is not None:
                return False
            connection.execute(
                "UPDATE flow_rounds SET merged_at=%s,merge_commit_sha=%s"
                " WHERE flow_id=%s AND round_no=%s",
                (merge.merged_at, merge.merge_commit_sha, flow_id, round_no),
            )
            return True

    def correct_archive(
        self,
        flow_id: int,
        round_no: int,
        *,
        expected_version: int,
        actor: str,
        reason: str,
        confirmed: bool,
    ) -> FlowRound:
        """Correct abandonment only when saved merge evidence strictly predates archive.

        Worker -> Flow -> round locks serialize this operation with recovery/resume.
        The immutable snapshot remains intact, and the audit is inserted before the
        effective conclusion changes so the round trigger can publish both together.
        Stale, repeated or insufficiently evidenced requests raise ValueError.
        """
        if confirmed is not True:
            raise ValueError("archive correction requires explicit confirmation")
        actor, reason = actor.strip(), reason.strip()
        if not actor or not reason:
            raise ValueError("archive correction requires a nonempty actor and reason")
        with self._db.transaction() as tx, tx.connection() as connection:
            flow = tx.lock_workflow(flow_id)
            if flow is None:
                raise ValueError("Flow does not exist")
            with connection.cursor(row_factory=class_row(FlowRound)) as cursor:
                round = cursor.execute(
                    "SELECT * FROM flow_rounds WHERE flow_id=%s AND round_no=%s FOR UPDATE",
                    (flow_id, round_no),
                ).fetchone()
                if round is None or round_no > flow.current_round_no:
                    raise ValueError("archived round does not exist")
                if round.version != expected_version:
                    raise ValueError("archive correction version conflict")
                if round.archive_reason is not ArchiveReason.ABANDONED:
                    raise ValueError("only an abandoned round can be corrected")
                if (
                    round.repo != flow.repo
                    or round.pr_number is None
                    or round.merged_at is None
                    or not (round.merge_commit_sha or "").strip()
                ):
                    raise ValueError("archive correction requires saved PR merge evidence")
                # GitHub timestamps have second precision: even the next-second
                # boundary cannot establish a strict ordering against local archive.
                if round.merged_at + timedelta(seconds=1) >= round.archived_at:
                    raise ValueError("merge evidence does not prove merge before archive")
                version = round.version + 1
                evidence = {
                    "repo": round.repo,
                    "pr_number": round.pr_number,
                    "merged_at": round.merged_at.isoformat(),
                    "merge_commit_sha": round.merge_commit_sha,
                    "merge_time_precision_seconds": 1,
                    "archived_at": round.archived_at.isoformat(),
                    "original_reason": round.original_reason.value,
                    "before": {
                        "archive_reason": round.archive_reason.value,
                        "version": round.version,
                    },
                    "after": {"archive_reason": ArchiveReason.SUCCEEDED.value, "version": version},
                }
                connection.execute(
                    "INSERT INTO flow_round_corrections"
                    " (flow_id,round_no,version,actor,reason,evidence,corrected_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (flow_id, round_no, version, actor, reason, Jsonb(evidence), tx.now()),
                )
                corrected = cursor.execute(
                    "UPDATE flow_rounds SET archive_reason=%s,version=%s"
                    " WHERE flow_id=%s AND round_no=%s RETURNING *",
                    (ArchiveReason.SUCCEEDED, version, flow_id, round_no),
                ).fetchone()
            if flow.current_round_no == round_no and flow.state is FlowState.ARCHIVED:
                # Do not call update_flow: it also advances updated_at and retires work.
                connection.execute(
                    "UPDATE flows SET archive_reason=%s WHERE id=%s",
                    (ArchiveReason.SUCCEEDED, flow_id),
                )
            return required(corrected)
