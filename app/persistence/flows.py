"""Flow and Attempt facts using the current database transaction."""

from __future__ import annotations

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.domain.models import (
    ArchiveReason,
    Attempt,
    DeliveryState,
    Flow,
    FlowRound,
    FlowState,
)
from app.persistence.records import required
from app.persistence.repository import Repository


class FlowStore(Repository):
    """Flow and Attempt facts using the current database transaction."""

    def create_flow(self, repo: str, issue_number: int, installation_id: int | None) -> Flow:
        now = self._db.now()
        with (
            self._db.transaction() as tx,
            tx.connection() as connection,
            connection.cursor(row_factory=class_row(Flow)) as cursor,
        ):
            tx.serialize_issue(repo, issue_number)
            row = cursor.execute(
                "INSERT INTO flows(repo, issue_number, installation_id, state, created_at,"
                " updated_at) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (repo, issue_number, installation_id, FlowState.QUEUED, now, now),
            ).fetchone()
            FlowStore(tx).select_owner(required(row))
        return required(row)

    def get_flow(self, flow_id: int) -> Flow | None:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Flow)) as cursor,
        ):
            return cursor.execute("SELECT * FROM flows WHERE id=%s", (flow_id,)).fetchone()

    def get_active_flow(self, repo: str, issue_number: int) -> Flow | None:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Flow)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM flows WHERE repo=%s AND issue_number=%s AND state<>'archived'"
                " ORDER BY id DESC LIMIT 1",
                (repo, issue_number),
            ).fetchone()

    def get_flow_by_pr(self, repo: str, pr_number: int) -> Flow | None:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Flow)) as cursor,
        ):
            return cursor.execute(
                "SELECT f.* FROM flows f JOIN flow_deliveries d ON d.flow_id=f.id"
                " WHERE f.repo=%s AND d.pr_number=%s ORDER BY f.id DESC LIMIT 1",
                (repo, pr_number),
            ).fetchone()

    def update_flow(self, flow: Flow) -> None:
        # Review processing reads flows too; resolve its repository after module initialization.
        from app.persistence.reviews import ReviewStore

        flow.updated_at = self._db.now()
        with self._db.transaction() as tx, tx.connection() as connection:
            if (
                flow.state is FlowState.ARCHIVED
                and FlowStore(tx).get_round(flow.id, flow.current_round_no) is None
            ):
                raise ValueError("archive requires settled round evidence")
            changed = connection.execute(
                "UPDATE flows SET installation_id=%s, state=%s, current_attempt_id=%s,"
                " archive_reason=%s,archive_summary=%s,archived_at=%s,updated_at=%s"
                " WHERE id=%s AND current_round_no=%s",
                (
                    flow.installation_id,
                    flow.state,
                    flow.current_attempt_id,
                    flow.archive_reason,
                    flow.archive_summary,
                    flow.archived_at,
                    flow.updated_at,
                    flow.id,
                    flow.current_round_no,
                ),
            )
            if changed.rowcount != 1:
                raise ValueError("Flow round is no longer current")
            if flow.state in {FlowState.FINALIZING, FlowState.ARCHIVED}:
                ReviewStore(tx).cancel(flow.id)

    def list_flows(self, *, active_only: bool = False) -> list[Flow]:
        query = "SELECT * FROM flows"
        if active_only:
            query += " WHERE state<>'archived'"
        query += " ORDER BY id DESC"
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Flow)) as cursor,
        ):
            return cursor.execute(query).fetchall()

    def create_attempt(self, flow_id: int) -> Attempt:
        now = self._db.now()
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Attempt)) as cursor,
        ):
            row = cursor.execute(
                "INSERT INTO attempts(flow_id,round_no,revision_cycles,created_at,updated_at)"
                " SELECT id,current_round_no,0,%s,%s FROM flows WHERE id=%s RETURNING *",
                (now, now, flow_id),
            ).fetchone()
        return required(row)

    def get_attempt(self, attempt_id: int) -> Attempt | None:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Attempt)) as cursor,
        ):
            return cursor.execute("SELECT * FROM attempts WHERE id=%s", (attempt_id,)).fetchone()

    def get_current_attempt(self, flow: Flow) -> Attempt | None:
        return self.get_attempt(flow.current_attempt_id) if flow.current_attempt_id else None

    def list_attempts(self, flow_id: int) -> list[Attempt]:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Attempt)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM attempts WHERE flow_id=%s ORDER BY id", (flow_id,)
            ).fetchall()

    def update_attempt(self, attempt: Attempt) -> None:
        attempt.updated_at = self._db.now()
        with self._db.connection() as connection:
            connection.execute(
                "UPDATE attempts SET codex_session_id=%s, revision_cycles=%s,"
                " session_generation=%s,last_revision_summary=%s,"
                " updated_at=%s WHERE id=%s",
                (
                    attempt.codex_session_id,
                    attempt.revision_cycles,
                    attempt.session_generation,
                    attempt.last_revision_summary,
                    attempt.updated_at,
                    attempt.id,
                ),
            )

    def select_owner(self, flow: Flow) -> None:
        """Creation/resume alone select the Issue's display owner, including after archive."""
        with self._db.connection() as connection:
            connection.execute(
                "INSERT INTO issue_flow_owners(repo,issue_number,flow_id) VALUES (%s,%s,%s)"
                " ON CONFLICT(repo,issue_number) DO UPDATE SET flow_id=excluded.flow_id,"
                " version=issue_flow_owners.version+1",
                (flow.repo, flow.issue_number, flow.id),
            )

    def get_round(self, flow_id: int, round_no: int) -> FlowRound | None:
        with self._db.connection() as connection:
            return (
                connection.cursor(row_factory=class_row(FlowRound))
                .execute(
                    "SELECT * FROM flow_rounds WHERE flow_id=%s AND round_no=%s",
                    (flow_id, round_no),
                )
                .fetchone()
            )

    def try_archive(self, flow_id: int) -> bool:
        """Finish a requested round only after stops, results and disposition settle.

        Abandonment/investigation retain the workspace after confirmed stop; merged
        deliveries additionally require the original worker's verified archive receipt.
        Call after result/stop markers in their transaction, never before them.
        """
        from app.persistence.deliveries import DeliveryStore
        from app.persistence.workers import WorkerStore

        with self._db.transaction() as tx, tx.connection() as connection:
            flow = tx.lock_workflow(flow_id)
            if (
                flow is None
                or flow.state is not FlowState.FINALIZING
                or flow.archive_reason is None
            ):
                return False
            delivery = DeliveryStore(tx).get_delivery(flow_id)
            if (
                connection.execute(
                    "SELECT 1 FROM jobs WHERE flow_id=%s AND round_no=%s AND"
                    " (state IN ('queued','running') OR (result_received_at IS NOT NULL AND result_applied_at IS NULL)"
                    " OR (kind<>'inference' AND (started_at IS NOT NULL OR dispatch_authorized_at IS NOT NULL"
                    " OR (kind='archive' AND payload->'archive' IS NOT NULL))"
                    " AND result_received_at IS NULL AND NOT EXISTS (SELECT 1 FROM execution_stops s"
                    " WHERE s.job_id=jobs.id AND s.assignment_token=jobs.assignment_token AND s.state='confirmed')))"
                    " UNION ALL SELECT 1 FROM execution_stops WHERE flow_id=%s AND state<>'confirmed'"
                    " UNION ALL SELECT 1 FROM result_effects e JOIN jobs j ON j.id=e.job_id"
                    " WHERE j.flow_id=%s AND j.round_no=%s AND ((e.state NOT IN ('succeeded','skipped')"
                    " AND NOT (e.state='failed' AND e.dispatched_at IS NULL))"
                    " OR (e.dispatched_at IS NOT NULL AND e.receipt IS NULL)) LIMIT 1",
                    (flow_id, flow.current_round_no, flow_id, flow_id, flow.current_round_no),
                ).fetchone()
                is not None
            ):
                return False
            if delivery is not None and delivery.state is DeliveryState.MERGED:
                if flow.archive_reason is not ArchiveReason.SUCCEEDED:
                    return False
                if (
                    connection.execute(
                        "SELECT 1 FROM jobs j JOIN result_applications r ON r.job_id=j.id"
                        " WHERE j.flow_id=%s AND j.round_no=%s AND j.kind='archive' AND j.state='done'"
                        " AND j.result_applied_at IS NOT NULL"
                        " AND r.plan->'archive'->>'cleanup' IN ('removed','released','already_absent')",
                        (flow_id, flow.current_round_no),
                    ).fetchone()
                    is None
                ):
                    return False
            elif flow.archive_reason is ArchiveReason.SUCCEEDED and (
                not (flow.archive_summary or "").strip()
                or (delivery is not None and delivery.pr_number is not None)
            ):
                return False
            workers = WorkerStore(tx)
            execution = workers.get_flow_execution(flow_id)
            if execution is not None and execution.released_at is None:
                if not workers.release_flow_execution(flow_id):
                    return False
                execution = workers.get_flow_execution(flow_id)
            flow.archived_at = tx.now()
            connection.execute(
                "INSERT INTO flow_rounds(flow_id,round_no,repo,pr_number,original_reason,archive_reason,"
                " archive_summary,archived_at,delivery,execution,merged_at,merge_commit_sha)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    flow.id,
                    flow.current_round_no,
                    flow.repo,
                    delivery.pr_number if delivery else None,
                    flow.archive_reason,
                    flow.archive_reason,
                    flow.archive_summary,
                    flow.archived_at,
                    Jsonb(delivery.model_dump(mode="json")) if delivery else None,
                    Jsonb(execution.model_dump(mode="json")) if execution else None,
                    delivery.merged_at if delivery else None,
                    delivery.merge_commit_sha if delivery else None,
                ),
            )
            flow.state = FlowState.ARCHIVED
            FlowStore(tx).update_flow(flow)
            return True
