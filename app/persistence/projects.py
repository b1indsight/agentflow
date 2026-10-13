"""Durable, replaceable Issue status projections, separate from worker results."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, TypedDict, Unpack

import psycopg
from psycopg import sql
from psycopg.rows import class_row
from pydantic import BaseModel

from app.domain.project_models import ProjectStatus
from app.github import GitHubError, ProjectConfig, retry_delay

if TYPE_CHECKING:
    from app.persistence.database import Database


class Projection(BaseModel):
    id: int
    repo: str
    issue_number: int
    project_id: str
    content_id: str | None
    flow_id: int
    item_id: str | None
    config_key: str
    desired_status: ProjectStatus
    desired_version: int
    synced_version: int
    next_at: datetime
    failures: int
    last_error: str | None
    blocked: bool
    cursor: str | None
    scan_incomplete: bool
    add_started: bool


def config_key(config: ProjectConfig) -> str:
    return json.dumps(config.model_dump(mode="json"), sort_keys=True)


class ProjectionUpdate(TypedDict, total=False):
    """Locally writable checkpoints; destination identity and desired versions stay fenced."""

    content_id: str | None
    item_id: str | None
    cursor: str | None
    scan_incomplete: bool
    add_started: bool
    blocked: bool
    last_error: str | None
    failures: int
    next_at: datetime


class ProjectStore:
    """Persist projections and partial sync checkpoints without overwriting newer desired facts."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def seed(self, repo: str, config: ProjectConfig, limit: int) -> None:
        now = self._db.now()
        with self._db.connection() as connection:
            connection.execute(
                "INSERT INTO project_destinations VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (repo, config.project_id, now),
            )
            connection.execute(
                "INSERT INTO project_projections(repo,issue_number,project_id,flow_id,config_key,desired_status,next_at)"
                " SELECT lower(repo),issue_number,%s,id,%s,'todo',%s FROM ("
                " SELECT DISTINCT ON (f.issue_number) f.* FROM flows f JOIN project_destinations d"
                " ON d.repo=%s AND d.project_id=%s WHERE lower(f.repo)=d.repo"
                " AND (f.state='active' OR f.updated_at>=d.enabled_at)"
                " AND NOT EXISTS (SELECT 1 FROM project_projections p WHERE p.repo=d.repo"
                " AND p.project_id=d.project_id AND p.issue_number=f.issue_number)"
                " ORDER BY f.issue_number,(f.state='active') DESC,f.id DESC) owners"
                " ORDER BY id LIMIT %s ON CONFLICT DO NOTHING",
                (config.project_id, config_key(config), now, repo, config.project_id, limit),
            )

    def list_projections(self, *, after: int = 0, limit: int = 100, repo: str | None = None) -> list[Projection]:
        with self._db.connection() as connection:
            return connection.cursor(row_factory=class_row(Projection)).execute(
                "SELECT * FROM project_projections WHERE id>%s AND (%s::text IS NULL OR repo=%s)"
                " ORDER BY id LIMIT %s", (after, repo, repo, limit),
            ).fetchall()

    def get(self, identity: int) -> Projection:
        with self._db.connection() as connection:
            row = connection.cursor(row_factory=class_row(Projection)).execute(
                "SELECT * FROM project_projections WHERE id=%s", (identity,),
            ).fetchone()
        assert row is not None
        return row

    def due(self, configs: dict[str, ProjectConfig], limit: int) -> list[Projection]:
        if not configs:
            return []
        with self._db.connection() as connection:
            return connection.cursor(row_factory=class_row(Projection)).execute(
                "SELECT * FROM project_projections WHERE NOT blocked AND next_at<=%s"
                " AND (repo,project_id) IN (SELECT * FROM unnest(%s::text[],%s::text[]))"
                " ORDER BY next_at,id LIMIT %s",
                (self._db.now(), list(configs), [c.project_id for c in configs.values()], limit),
            ).fetchall()

    def refresh(self, identity: int, config: ProjectConfig) -> Projection:
        with self._db.transaction() as tx, tx.connection() as connection:
            row = connection.cursor(row_factory=class_row(Projection)).execute(
                "SELECT * FROM project_projections WHERE id=%s FOR UPDATE", (identity,),
            ).fetchone()
            assert row is not None
            fact = connection.execute(
                "SELECT f.id,f.installation_id,CASE"
                " WHEN f.state='succeeded' THEN 'done' WHEN f.state IN ('failed','cancelled') THEN 'blocked'"
                " WHEN d.state='closed' OR j.state='failed' OR (j.kind='archive'"
                " AND j.recovery_deadline IS NOT NULL AND j.result_received_at IS NULL) OR EXISTS (SELECT 1 FROM result_effects e"
                " JOIN jobs ej ON ej.id=e.job_id WHERE ej.flow_id=f.id AND ej.result_applied_at IS NULL"
                " AND e.state='failed') THEN 'blocked'"
                " WHEN d.state='merged' THEN 'in_progress'"
                " WHEN d.pr_number IS NULL AND x.worker_id IS NULL THEN 'todo'"
                " WHEN EXISTS (SELECT 1 FROM jobs pending WHERE pending.flow_id=f.id"
                " AND (pending.state IN ('queued','running') OR (pending.result_received_at IS NOT NULL"
                " AND pending.result_applied_at IS NULL))) THEN 'in_progress'"
                " WHEN d.state='open' THEN 'in_review' ELSE 'in_progress' END AS status"
                " FROM flows f LEFT JOIN flow_deliveries d ON d.flow_id=f.id"
                " LEFT JOIN flow_executions x ON x.flow_id=f.id AND x.released_at IS NULL"
                " LEFT JOIN LATERAL (SELECT state,kind,recovery_deadline,result_received_at"
                " FROM jobs WHERE flow_id=f.id ORDER BY id DESC LIMIT 1) j ON TRUE"
                " WHERE lower(f.repo)=%s AND f.issue_number=%s ORDER BY (f.state='active') DESC,f.id DESC LIMIT 1",
                (row.repo, row.issue_number),
            ).fetchone()
            assert fact is not None
            key = config_key(config)
            changed = (row.flow_id, row.desired_status, row.config_key) != (fact["id"], fact["status"], key)
            if changed:
                reset = row.config_key != key
                connection.execute(
                    "UPDATE project_projections SET flow_id=%s,desired_status=%s,config_key=%s,"
                    " desired_version=desired_version+1,next_at=CASE WHEN %s OR (failures=0 AND NOT blocked)"
                    " THEN %s ELSE next_at END,blocked=CASE WHEN %s THEN FALSE ELSE blocked END,"
                    " failures=CASE WHEN %s THEN 0 ELSE failures END WHERE id=%s",
                    (fact["id"], fact["status"], key, reset, tx.now(), reset, reset, identity),
                )
            if fact["installation_id"] != config.installation_id:
                connection.execute("UPDATE project_projections SET blocked=TRUE,last_error=%s WHERE id=%s",
                                   ("project installation does not match owner Flow", identity))
            return ProjectStore(tx).get(identity)

    def save(self, row: Projection, **values: Unpack[ProjectionUpdate]) -> None:
        """Update only supplied sync fields while retaining the projection's config fence."""
        if not values:
            raise ValueError("projection update requires at least one field")
        with self._db.connection() as connection:
            connection.execute(sql.SQL("UPDATE project_projections SET {} WHERE id=%s AND config_key=%s").format(
                sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(key)) for key in values)),
                (*values.values(), row.id, row.config_key),
            )

    def acknowledge(self, row: Projection, interval: float) -> None:
        with self._db.connection() as connection:
            connection.execute(
                "UPDATE project_projections SET synced_version=GREATEST(synced_version,%s),"
                " failures=0,last_error=NULL,next_at=CASE WHEN desired_version=%s THEN %s ELSE %s END"
                " WHERE id=%s AND config_key=%s",
                (row.desired_version, row.desired_version, self._db.now()+timedelta(seconds=interval),
                 self._db.now(), row.id, row.config_key),
            )

    def failed(self, row: Projection, error: GitHubError) -> None:
        attempts = row.failures + 1
        self.save(row, failures=attempts, last_error=str(error), blocked=not error.retryable,
                  next_at=self._db.now()+timedelta(seconds=retry_delay(attempts, self._db.now(), error)))

    def retry(self, repo: str) -> int:
        with self._db.connection() as connection:
            return connection.execute(
                "UPDATE project_projections SET blocked=FALSE,failures=0,last_error=NULL,next_at=%s,"
                " cursor=NULL,scan_incomplete=FALSE WHERE repo=%s", (self._db.now(), repo.casefold()),
            ).rowcount

    @asynccontextmanager
    async def claim(self, row: Projection) -> AsyncIterator[bool]:
        # A dedicated session lock spans I/O without holding workflow transactions or pool slots.
        async with await psycopg.AsyncConnection.connect(self._db.database_url, autocommit=True) as connection:
            cursor = await connection.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,1))",
                                              (f"{row.project_id}:{row.repo}#{row.issue_number}",))
            acquired = await cursor.fetchone()
            yield bool(acquired and acquired[0])
