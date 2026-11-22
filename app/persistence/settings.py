"""Durable console settings with independent optimistic concurrency checks."""

from enum import StrEnum

from psycopg import sql
from psycopg.rows import class_row

from app.domain.models import CodingModelSettings
from app.persistence.records import required
from app.persistence.repository import Repository


class SettingsConflict(Exception):
    """Another settings write has advanced the caller's revision."""


class TextSetting(StrEnum):
    """Nullable text columns that can be changed independently of a settings group."""

    SKILL_REPOSITORY = "skill_repository"


class SettingsStore(Repository):
    """Persist individual settings and the revision-guarded coding defaults."""

    def get_value(self, key: TextSetting) -> str | None:
        with self._db.connection() as connection:
            row = required(connection.execute(
                sql.SQL("SELECT {} AS value FROM runtime_settings WHERE id=1").format(sql.Identifier(key.value)),
            ).fetchone())
            return row["value"]

    def set_value(self, key: TextSetting, value: str | None, *, expected: str | None) -> None:
        """Replace a single value only if it still matches the caller's snapshot (including NULL)."""
        with self._db.connection() as connection:
            updated = connection.execute(
                sql.SQL("UPDATE runtime_settings SET {col}=%s WHERE id=1 AND {col} IS NOT DISTINCT FROM %s")
                .format(col=sql.Identifier(key.value)), (value, expected),
            )
            if not updated.rowcount:
                raise SettingsConflict

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
