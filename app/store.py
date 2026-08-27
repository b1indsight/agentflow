"""SQLite 持久层：DDL + 仓储。见 docs/plan/m0.md §4。

TODO(verify) V7：M0 用 stdlib sqlite3（同步）。FastAPI 是 async，若并发写吃紧
再换 aiosqlite 或 asyncio.to_thread 包装。当前操作都很短，直接同步调用。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from app.models import (
    Attempt,
    Flow,
    FlowState,
    Job,
    JobKind,
    JobState,
    ReviewItem,
    ReviewItemKind,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL, issue_number INTEGER NOT NULL,
  installation_id INTEGER,
  state TEXT NOT NULL,
  current_attempt_id INTEGER REFERENCES attempts(id),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_flows_active ON flows(repo, issue_number)
  WHERE state NOT IN ('archived','cancelled','failed');

-- 一条 flow 挂多个候选 attempt（§9-2 多方案分支）；MVP 恰好一个。
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  flow_id INTEGER NOT NULL REFERENCES flows(id),
  branch TEXT, codex_session_id TEXT, pr_number INTEGER,
  revision_cycles INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attempts_flow ON attempts(flow_id);
CREATE INDEX IF NOT EXISTS ix_attempts_pr ON attempts(pr_number);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  flow_id INTEGER NOT NULL REFERENCES flows(id),
  kind TEXT NOT NULL, state TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0, pid INTEGER, heartbeat_at TEXT,
  payload TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS events (
  delivery_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
  flow_id INTEGER REFERENCES flows(id), payload TEXT, processed_at TEXT NOT NULL
);

-- flows → runs 一对多，归属到具体 attempt（§9-2）。
CREATE TABLE IF NOT EXISTS codex_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id),
  kind TEXT NOT NULL, prompt TEXT, log_path TEXT, exit_code INTEGER,
  started_at TEXT, ended_at TEXT
);

CREATE TABLE IF NOT EXISTS review_threads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id),
  kind TEXT NOT NULL, github_id INTEGER NOT NULL, thread_id TEXT,
  path TEXT, line INTEGER, original_commit_id TEXT, diff_hunk TEXT,
  author TEXT, body TEXT,
  relayed INTEGER NOT NULL DEFAULT 0, resolved INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_gid ON review_threads(attempt_id, kind, github_id);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _required[T](value: T | None) -> T:
    """Fail fast when an SQLite insert/read-back invariant is violated."""
    if value is None:
        raise RuntimeError("database insert did not produce a readable record")
    return value


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")

    def init_db(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ flows

    def create_flow(self, repo: str, issue_number: int, installation_id: int | None) -> Flow:
        now = utcnow()
        cur = self._conn.execute(
            "INSERT INTO flows(repo, issue_number, installation_id, state, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (repo, issue_number, installation_id, FlowState.PENDING, _iso(now), _iso(now)),
        )
        self._conn.commit()
        return _required(self.get_flow(_required(cur.lastrowid)))

    def get_flow(self, flow_id: int) -> Flow | None:
        row = self._conn.execute("SELECT * FROM flows WHERE id=?", (flow_id,)).fetchone()
        return self._row_to_flow(row) if row else None

    def get_active_flow(self, repo: str, issue_number: int) -> Flow | None:
        row = self._conn.execute(
            "SELECT * FROM flows WHERE repo=? AND issue_number=? AND state NOT IN"
            " ('archived','cancelled','failed') ORDER BY id DESC LIMIT 1",
            (repo, issue_number),
        ).fetchone()
        return self._row_to_flow(row) if row else None

    def get_flow_by_pr(self, repo: str, pr_number: int) -> Flow | None:
        # PR 归属到 attempt（§9-2）；经 attempts 反查 flow。
        row = self._conn.execute(
            "SELECT f.* FROM flows f JOIN attempts a ON a.flow_id=f.id"
            " WHERE f.repo=? AND a.pr_number=? ORDER BY f.id DESC LIMIT 1",
            (repo, pr_number),
        ).fetchone()
        return self._row_to_flow(row) if row else None

    def update_flow(self, flow: Flow) -> None:
        flow.updated_at = utcnow()
        self._conn.execute(
            "UPDATE flows SET installation_id=?, state=?, current_attempt_id=?, updated_at=?"
            " WHERE id=?",
            (
                flow.installation_id, flow.state, flow.current_attempt_id,
                _iso(flow.updated_at), flow.id,
            ),
        )
        self._conn.commit()

    def list_flows(self, *, active_only: bool = False) -> list[Flow]:
        sql = "SELECT * FROM flows"
        if active_only:
            sql += " WHERE state NOT IN ('archived','cancelled','failed')"
        sql += " ORDER BY id DESC"
        return [self._row_to_flow(r) for r in self._conn.execute(sql).fetchall()]

    def _row_to_flow(self, r: sqlite3.Row) -> Flow:
        return Flow(
            id=r["id"], repo=r["repo"], issue_number=r["issue_number"],
            installation_id=r["installation_id"], state=FlowState(r["state"]),
            current_attempt_id=r["current_attempt_id"],
            created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]),  # type: ignore[arg-type]
        )

    # --------------------------------------------------------------- attempts

    def create_attempt(self, flow_id: int) -> Attempt:
        now = utcnow()
        cur = self._conn.execute(
            "INSERT INTO attempts(flow_id, revision_cycles, created_at, updated_at)"
            " VALUES (?,?,?,?)",
            (flow_id, 0, _iso(now), _iso(now)),
        )
        self._conn.commit()
        return _required(self.get_attempt(_required(cur.lastrowid)))

    def get_attempt(self, attempt_id: int) -> Attempt | None:
        row = self._conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        return self._row_to_attempt(row) if row else None

    def get_current_attempt(self, flow: Flow) -> Attempt | None:
        return self.get_attempt(flow.current_attempt_id) if flow.current_attempt_id else None

    def list_attempts(self, flow_id: int) -> list[Attempt]:
        rows = self._conn.execute(
            "SELECT * FROM attempts WHERE flow_id=? ORDER BY id", (flow_id,)
        ).fetchall()
        return [self._row_to_attempt(r) for r in rows]

    def update_attempt(self, attempt: Attempt) -> None:
        attempt.updated_at = utcnow()
        self._conn.execute(
            "UPDATE attempts SET branch=?, codex_session_id=?, pr_number=?, revision_cycles=?,"
            " updated_at=? WHERE id=?",
            (
                attempt.branch, attempt.codex_session_id, attempt.pr_number,
                attempt.revision_cycles, _iso(attempt.updated_at), attempt.id,
            ),
        )
        self._conn.commit()

    def _row_to_attempt(self, r: sqlite3.Row) -> Attempt:
        return Attempt(
            id=r["id"], flow_id=r["flow_id"], branch=r["branch"],
            codex_session_id=r["codex_session_id"], pr_number=r["pr_number"],
            revision_cycles=r["revision_cycles"],
            created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]),  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------- jobs

    def enqueue_job(self, flow_id: int, kind: JobKind, payload: dict | None = None) -> Job:
        now = utcnow()
        cur = self._conn.execute(
            "INSERT INTO jobs(flow_id, kind, state, attempt, payload, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (flow_id, kind, JobState.QUEUED, 0, json.dumps(payload) if payload else None,
             _iso(now), _iso(now)),
        )
        self._conn.commit()
        return _required(self.get_job(_required(cur.lastrowid)))

    def get_job(self, job_id: int) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def claim_job(self) -> Job | None:
        """取一个 queued job 标为 running（附 pid）。M0 单 worker，无需行锁。"""
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE state=? ORDER BY id LIMIT 1", (JobState.QUEUED,)
        ).fetchone()
        if not row:
            return None
        now = utcnow()
        self._conn.execute(
            "UPDATE jobs SET state=?, pid=?, attempt=attempt+1, heartbeat_at=?, updated_at=?"
            " WHERE id=?",
            (JobState.RUNNING, os.getpid(), _iso(now), _iso(now), row["id"]),
        )
        self._conn.commit()
        return self.get_job(row["id"])

    def heartbeat(self, job_id: int) -> None:
        now = utcnow()
        self._conn.execute(
            "UPDATE jobs SET heartbeat_at=?, updated_at=? WHERE id=?", (_iso(now), _iso(now), job_id)
        )
        self._conn.commit()

    def finish_job(self, job_id: int, state: JobState) -> None:
        self._conn.execute(
            "UPDATE jobs SET state=?, updated_at=? WHERE id=?", (state, _iso(utcnow()), job_id)
        )
        self._conn.commit()

    def recover_stale_jobs(self, stale_after_seconds: float = 300.0) -> int:
        """启动时 / 定期：running 但心跳过期（进程已死）→ 重排队。§10-1。"""
        cutoff = utcnow() - timedelta(seconds=stale_after_seconds)
        rows = self._conn.execute(
            "SELECT id, heartbeat_at FROM jobs WHERE state=?", (JobState.RUNNING,)
        ).fetchall()
        recovered = 0
        for r in rows:
            hb = _dt(r["heartbeat_at"])
            if hb is None or hb < cutoff:
                self._conn.execute(
                    "UPDATE jobs SET state=?, pid=NULL, updated_at=? WHERE id=?",
                    (JobState.QUEUED, _iso(utcnow()), r["id"]),
                )
                recovered += 1
        self._conn.commit()
        return recovered

    def _row_to_job(self, r: sqlite3.Row) -> Job:
        return Job(
            id=r["id"], flow_id=r["flow_id"], kind=JobKind(r["kind"]), state=JobState(r["state"]),
            attempt=r["attempt"], pid=r["pid"], heartbeat_at=_dt(r["heartbeat_at"]),
            payload=json.loads(r["payload"]) if r["payload"] else None,
            created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]),  # type: ignore[arg-type]
        )

    # ----------------------------------------------------------------- events

    def event_seen(self, delivery_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM events WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        return row is not None

    def record_event(
        self, delivery_id: str, event_type: str, flow_id: int | None, payload: dict
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO events(delivery_id, event_type, flow_id, payload, processed_at)"
            " VALUES (?,?,?,?,?)",
            (delivery_id, event_type, flow_id, json.dumps(payload), _iso(utcnow())),
        )
        self._conn.commit()

    # --------------------------------------------------------- review_threads

    def upsert_review_item(self, item: ReviewItem) -> None:
        self._conn.execute(
            "INSERT INTO review_threads(attempt_id, kind, github_id, thread_id, path, line,"
            " original_commit_id, diff_hunk, author, body, relayed, resolved, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(attempt_id, kind, github_id) DO UPDATE SET"
            " thread_id=excluded.thread_id, body=excluded.body",
            (
                item.attempt_id, item.kind, item.github_id, item.thread_id, item.path, item.line,
                item.original_commit_id, item.diff_hunk, item.author, item.body,
                int(item.relayed), int(item.resolved), _iso(item.created_at),
            ),
        )
        self._conn.commit()

    def unrelayed_items(self, attempt_id: int) -> list[ReviewItem]:
        rows = self._conn.execute(
            "SELECT * FROM review_threads WHERE attempt_id=? AND relayed=0 ORDER BY id",
            (attempt_id,),
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def mark_relayed(self, item_ids: list[int]) -> None:
        self._conn.executemany(
            "UPDATE review_threads SET relayed=1 WHERE id=?", [(i,) for i in item_ids]
        )
        self._conn.commit()

    def mark_resolved(self, attempt_id: int, github_id: int) -> None:
        self._conn.execute(
            "UPDATE review_threads SET resolved=1 WHERE attempt_id=? AND github_id=?",
            (attempt_id, github_id),
        )
        self._conn.commit()

    def _row_to_item(self, r: sqlite3.Row) -> ReviewItem:
        return ReviewItem(
            id=r["id"], attempt_id=r["attempt_id"], kind=ReviewItemKind(r["kind"]),
            github_id=r["github_id"], author=r["author"], body=r["body"], thread_id=r["thread_id"],
            path=r["path"], line=r["line"], original_commit_id=r["original_commit_id"],
            diff_hunk=r["diff_hunk"], relayed=bool(r["relayed"]), resolved=bool(r["resolved"]),
            created_at=_dt(r["created_at"]),  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------- codex_runs

    def record_codex_run(
        self, attempt_id: int, kind: str, prompt: str, log_path: str, exit_code: int
    ) -> None:
        now = utcnow()
        self._conn.execute(
            "INSERT INTO codex_runs(attempt_id, kind, prompt, log_path, exit_code, started_at,"
            " ended_at) VALUES (?,?,?,?,?,?,?)",
            (attempt_id, kind, prompt, log_path, exit_code, _iso(now), _iso(now)),
        )
        self._conn.commit()
