"""Repository label-check deadlines, independent of Flow and PR lifecycles."""

from datetime import datetime

from psycopg.rows import class_row
from pydantic import BaseModel

from app.persistence.repository import Repository


class LabelCheck(BaseModel):
    names: tuple[str, ...]
    next_at: datetime


class LabelBackoff(BaseModel):
    next_at: datetime


class LabelStore(Repository):
    """Retain one checkpoint per App/installation/repository across controller restarts."""

    def deferred_until(self, app_id: int) -> datetime | None:
        with self._db.connection() as connection:
            saved = connection.cursor(row_factory=class_row(LabelBackoff)).execute(
                "SELECT next_at FROM repository_label_backoff WHERE app_id=%s", (app_id,),
            ).fetchone()
            return saved.next_at if saved else None

    def defer(self, app_id: int, next_at: datetime) -> None:
        """Preserve scan-wide backoff even when discovery failed before finding a repository."""
        with self._db.connection() as connection:
            connection.execute(
                "INSERT INTO repository_label_backoff(app_id,next_at) VALUES(%s,%s)"
                " ON CONFLICT(app_id) DO UPDATE"
                " SET next_at=GREATEST(repository_label_backoff.next_at,EXCLUDED.next_at)",
                (app_id, next_at),
            )

    def get(self, app_id: int, installation_id: int, repo: str) -> LabelCheck | None:
        with self._db.connection() as connection:
            return connection.cursor(row_factory=class_row(LabelCheck)).execute(
                "SELECT names,next_at FROM repository_label_checks"
                " WHERE app_id=%s AND installation_id=%s AND repo=%s",
                (app_id, installation_id, repo.casefold()),
            ).fetchone()

    def schedule(
        self, app_id: int, installation_id: int, repo: str,
        names: tuple[str, ...], next_at: datetime,
    ) -> None:
        with self._db.connection() as connection:
            connection.execute(
                "INSERT INTO repository_label_checks(app_id,installation_id,repo,names,next_at)"
                " VALUES(%s,%s,%s,%s,%s) ON CONFLICT(app_id,installation_id,repo)"
                " DO UPDATE SET names=EXCLUDED.names,next_at=EXCLUDED.next_at",
                (app_id, installation_id, repo.casefold(), list(names), next_at),
            )
