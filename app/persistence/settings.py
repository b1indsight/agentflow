"""Durable coding defaults with optimistic concurrency for independent settings tabs."""

from psycopg.rows import class_row

from app.domain.models import CodingModelSettings
from app.persistence.records import required
from app.persistence.repository import Repository


class SettingsConflict(Exception):
    """Another settings write has advanced the caller's revision."""


class SettingsStore(Repository):
    """Read and atomically replace the one server-wide coding configuration."""

    def get(self) -> CodingModelSettings:
        with self._db.connection() as connection:
            return required(connection.cursor(row_factory=class_row(CodingModelSettings)).execute(
                "SELECT codex_model,amp_model,codex_effort,amp_effort,revision FROM runtime_settings WHERE id=1",
            ).fetchone())

    def update(
        self, codex_model: str | None, amp_model: str | None, *,
        codex_effort: str | None, amp_effort: str | None, expected_revision: int,
    ) -> CodingModelSettings:
        with self._db.connection() as connection:
            row = connection.cursor(row_factory=class_row(CodingModelSettings)).execute(
                "UPDATE runtime_settings SET codex_model=%s,amp_model=%s,codex_effort=%s,amp_effort=%s,revision=revision+1"
                " WHERE id=1 AND revision=%s RETURNING codex_model,amp_model,codex_effort,amp_effort,revision",
                (codex_model, amp_model, codex_effort, amp_effort, expected_revision),
            ).fetchone()
            if row is None:
                raise SettingsConflict
            return row
