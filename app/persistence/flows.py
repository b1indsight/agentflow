"""Flow and Attempt facts using the current database transaction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from psycopg.rows import class_row

from app.domain.models import (
    Attempt,
    Flow,
    FlowState,
)
from app.persistence.records import required

if TYPE_CHECKING:
    from app.persistence.database import Database


class FlowStore:
    """Flow and Attempt facts using the current database transaction."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create_flow(self, repo: str, issue_number: int, installation_id: int | None) -> Flow:
        now = self._db.now()
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Flow)) as cursor,
        ):
            row = cursor.execute(
                "INSERT INTO flows(repo, issue_number, installation_id, state, created_at,"
                " updated_at) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (repo, issue_number, installation_id, FlowState.ACTIVE, now, now),
            ).fetchone()
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
                "SELECT * FROM flows WHERE repo=%s AND issue_number=%s AND state NOT IN"
                " ('succeeded','cancelled','failed') ORDER BY id DESC LIMIT 1",
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
            connection.execute(
                "UPDATE flows SET installation_id=%s, state=%s, current_attempt_id=%s,"
                " updated_at=%s WHERE id=%s",
                (
                    flow.installation_id,
                    flow.state,
                    flow.current_attempt_id,
                    flow.updated_at,
                    flow.id,
                ),
            )
            if flow.state in {FlowState.CANCELLED, FlowState.FAILED, FlowState.SUCCEEDED}:
                ReviewStore(tx).cancel(flow.id)

    def list_flows(self, *, active_only: bool = False) -> list[Flow]:
        query = "SELECT * FROM flows"
        if active_only:
            query += " WHERE state NOT IN ('succeeded','cancelled','failed')"
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
                "INSERT INTO attempts(flow_id, revision_cycles, created_at, updated_at)"
                " VALUES (%s,%s,%s,%s) RETURNING *",
                (flow_id, 0, now, now),
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
