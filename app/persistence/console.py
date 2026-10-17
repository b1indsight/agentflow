"""Bounded console reads from the transactional current-state projection."""

import base64
import hashlib
import json
import re
import shlex

from pydantic import BaseModel, ConfigDict, ValidationError

from app.contracts.console import (
    ConsoleFailure,
    FlowDetail,
    FlowPage,
    FlowSummary,
    JobPage,
    JobSummary,
    Phase,
    RepositoryPage,
    SourceStatus,
)
from app.persistence.records import Row
from app.persistence.repository import Repository

PHASES: tuple[Phase, ...] = ("queued", "running", "review", "blocked", "done")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class PageCursor(BaseModel):
    """Scope and upper-ID watermark bind keyset continuation to one query."""

    model_config = ConfigDict(extra="forbid", strict=True)
    scope: str
    after: int
    ceiling: int

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, value: str, scope: str) -> "PageCursor":
        try:
            result = cls.model_validate_json(base64.b64decode(value, altchars=b"-_", validate=True))
            if result.scope != scope or not 0 <= result.after <= result.ceiling:
                raise ValueError("foreign cursor")
            return result
        except (ValueError, ValidationError) as error:
            raise ConsoleFailure("cursor_invalid") from error


def _scope(*values: object) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _filters(q: str) -> tuple[str, list[object]]:
    """Parse the documented search subset without silently broadening invalid queries."""
    clauses = ["TRUE"]
    values: list[object] = []
    seen = set()
    try:
        for token in shlex.split(q, posix=False):
            literal = token.startswith('"') and token.endswith('"')
            if ":" not in token or literal:
                token = token.strip('"')
                clauses.append(
                    "lower(concat_ws(' ',title,flow_id,facts->>'issue_number',"
                    "facts->>'pr_number',facts->>'repo')) LIKE %s ESCAPE '\\'"
                )
                values.append(
                    "%"
                    + token.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    + "%"
                )
                continue
            name, value = token.split(":", 1)
            name, value = name.lower(), value.strip('"')
            if name in seen or not value:
                raise ValueError("repeated or empty qualifier")
            seen.add(name)
            if name == "repo" and _REPO.fullmatch(value):
                clauses.append("lower(facts->>'repo')=%s")
                values.append(value.lower())
            elif name == "is" and value in ("active", "done"):
                clauses.append(
                    "facts->>'lifecycle'=" + ("'active'" if value == "active" else "'succeeded'")
                )
            elif name == "status" and value in PHASES:
                clauses.append("facts->>'phase'=%s")
                values.append(value)
            elif (
                name in ("issue", "pr")
                and value.isascii()
                and value.isdecimal()
                and 0 < int(value) < 2**31
            ):
                clauses.append("(facts->>%s)::bigint=%s")
                values.extend((name + "_number", int(value)))
            else:
                raise ValueError("unknown qualifier")
    except ValueError as error:
        raise ConsoleFailure("search_invalid", 422) from error
    return " AND ".join(clauses), values


def _summary(row: Row) -> FlowSummary:
    facts = row["facts"]
    repo, issue, pr = facts["repo"], facts["issue_number"], facts["pr_number"]
    # Repository identities originate in GitHub, but validate again before producing links.
    if not isinstance(repo, str) or not _REPO.fullmatch(repo):
        raise ConsoleFailure("repository_identity_invalid", 503)
    return FlowSummary(
        id=row["flow_id"],
        repo=repo,
        issue_number=issue,
        issue_url=f"https://github.com/{repo}/issues/{issue}",
        title=row["title"],
        pr_number=pr,
        pr_url=f"https://github.com/{repo}/pull/{pr}" if pr else None,
        lifecycle=facts["lifecycle"],
        phase=facts["phase"],
        action=facts["action"],
        worker_id=facts["worker_id"],
        worker_historical=bool(facts["worker_historical"]),
        revision_cycles=facts["revision_cycles"],
        branch=facts["branch"],
        created_at=facts["created_at"],
        updated_at=facts["updated_at"],
        state_version=row["state_version"],
    )


class ConsoleStore(Repository):
    """Read current state without per-card queries or network work inside transactions."""

    def flows(
        self, *, q: str = "", phase: Phase | None = None, cursor: str | None = None, limit: int = 50
    ) -> FlowPage:
        where, values = _filters(q)
        scope = _scope("flows", q, phase)
        with self._db.transaction() as tx, tx.connection() as connection:
            # Counts and page see the same committed local view; later pages remain live.
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            counts = {key: 0 for key in PHASES}
            for row in connection.execute(
                f"SELECT facts->>'phase' AS phase,count(*) AS total FROM console_flows WHERE {where} GROUP BY 1",
                values,
            ).fetchall():
                counts[row["phase"]] = row["total"]
            ceiling = connection.execute(
                "SELECT coalesce(max(flow_id),0) AS n FROM console_flows"
            ).fetchone()
            assert ceiling is not None
            position = (
                PageCursor.decode(cursor, scope)
                if cursor
                else PageCursor(scope=scope, after=ceiling["n"], ceiling=ceiling["n"])
            )
            column = " AND facts->>'phase'=%s" if phase else ""
            rows = connection.execute(
                "SELECT flow_id,facts-'delivery_body'-'delivery_title' AS facts,"
                "coalesce(title,facts->>'delivery_title') AS title,state_version FROM console_flows WHERE "
                + where
                + column
                + " AND flow_id<=%s AND flow_id<%s ORDER BY flow_id DESC LIMIT %s",
                (
                    *values,
                    *((phase,) if phase else ()),
                    position.ceiling,
                    position.after if cursor else position.ceiling + 1,
                    limit + 1,
                ),
            ).fetchall()
        more = len(rows) > limit
        return FlowPage(
            items=[_summary(row) for row in rows[:limit]],
            counts=counts,
            has_more=more,
            next_cursor=PageCursor(
                scope=scope, after=rows[limit - 1]["flow_id"], ceiling=position.ceiling
            ).encode()
            if more
            else None,
        )

    def detail(self, flow_id: int) -> FlowDetail:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT flow_id,facts-'delivery_body'-'delivery_title' AS facts,"
                "coalesce(title,facts->>'delivery_title') AS title,state_version"
                " FROM console_flows WHERE flow_id=%s",
                (flow_id,),
            ).fetchone()
        if row is None:
            raise ConsoleFailure("flow_not_found", 404)
        flow = _summary(row)
        explanations = {
            "queued": ("等待可用执行资源", "Worker 接收任务"),
            "running": ("任务执行、结果应用或恢复中", "等待执行或归档结果"),
            "review": ("交付已打开且无待执行任务", "等待 GitHub 审核或人工合并"),
            "blocked": ("任务已停止或存在执行/交付阻塞", "检查执行状态及 GitHub 交付"),
            "done": ("任务已归档完成", "无需后续动作"),
        }
        reason, condition = explanations[flow.phase]
        return FlowDetail(
            **flow.model_dump(),
            reason=reason,
            next_condition=condition,
            sources=self.sources(flow_id),
        )

    def sources(self, flow_id: int) -> list[SourceStatus]:
        """Summarize all child connections without hiding coverage behind a row limit."""
        with self._db.connection() as connection:
            rows = connection.execute(
                "WITH grouped AS (SELECT split_part(source,':',1) AS source,"
                " CASE WHEN bool_or(status='unavailable') THEN 'unavailable'"
                " WHEN bool_or(status='syncing') THEN 'syncing'"
                " WHEN bool_or(pending) THEN 'pending'"
                " WHEN bool_or(status='partial') THEN 'partial' ELSE 'available' END AS status,"
                " min(reason) AS reason,min(last_success_at) AS last_success_at"
                " FROM console_sync WHERE flow_id=%s GROUP BY split_part(source,':',1))"
                " SELECT source,status,reason,last_success_at,"
                " coalesce((SELECT jsonb_agg(DISTINCT limitation) FROM console_sync child,"
                " jsonb_array_elements_text(child.limitations) limitation WHERE child.flow_id=%s"
                " AND split_part(child.source,':',1)=grouped.source),'[]') AS limitations"
                " FROM grouped ORDER BY source",
                (flow_id, flow_id),
            ).fetchall()
        return [SourceStatus.model_validate(row) for row in rows]

    def repos(self, *, cursor: str | None = None, limit: int = 100) -> RepositoryPage:
        # A name cursor is independently scoped and cannot be used as a Flow cursor.
        after = ""
        if cursor:
            try:
                after = base64.b64decode(cursor, altchars=b"-_", validate=True).decode()
                if not _REPO.fullmatch(after):
                    raise ValueError("invalid repository cursor")
            except (ValueError, UnicodeError) as error:
                raise ConsoleFailure("cursor_invalid") from error
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT lower(facts->>'repo') AS repo FROM console_flows"
                " WHERE lower(facts->>'repo')>%s ORDER BY repo LIMIT %s",
                (after, limit + 1),
            ).fetchall()
        items = [row["repo"] for row in rows[:limit]]
        more = len(rows) > limit
        return RepositoryPage(
            items=items,
            has_more=more,
            next_cursor=base64.urlsafe_b64encode(items[-1].encode()).decode() if more else None,
        )

    def jobs(self, flow_id: int, *, cursor: str | None = None, limit: int = 50) -> JobPage:
        self.detail(flow_id)
        scope = _scope("jobs", flow_id)
        with self._db.connection() as connection:
            high = connection.execute(
                "SELECT coalesce(max(id),0) AS n FROM jobs WHERE flow_id=%s", (flow_id,)
            ).fetchone()
            assert high is not None
            position = (
                PageCursor.decode(cursor, scope)
                if cursor
                else PageCursor(scope=scope, after=high["n"], ceiling=high["n"])
            )
            rows = connection.execute(
                "SELECT id,flow_id,attempt,kind,state,assigned_worker_id AS worker_id,"
                "started_at,updated_at,created_at,CASE WHEN state IN ('succeeded','failed')"
                " THEN updated_at ELSE NULL END AS finished_at,CASE WHEN last_error IS NOT NULL"
                " OR prepare_last_error IS NOT NULL OR verify_last_error IS NOT NULL"
                " THEN 'execution_error' ELSE NULL END AS error_code FROM jobs"
                " WHERE flow_id=%s AND id<=%s AND id<%s ORDER BY id DESC LIMIT %s",
                (
                    flow_id,
                    position.ceiling,
                    position.after if cursor else position.ceiling + 1,
                    limit + 1,
                ),
            ).fetchall()
        more = len(rows) > limit
        return JobPage(
            items=[JobSummary.model_validate(row) for row in rows[:limit]],
            has_more=more,
            next_cursor=PageCursor(
                scope=scope, after=rows[limit - 1]["id"], ceiling=position.ceiling
            ).encode()
            if more
            else None,
        )

    def request_sync(self, flow_id: int, *, active_only: bool = True) -> None:
        with self._db.connection() as connection:
            connection.execute(
                "SELECT console_enqueue(flow_id) FROM console_flows WHERE flow_id=%s"
                + (" AND facts->>'lifecycle'='active'" if active_only else ""),
                (flow_id,),
            )

    def rebuild(self, flow_id: int) -> None:
        """Recompute from canonical facts; never replay or fabricate missing historical events."""
        with self._db.connection() as connection:
            connection.execute("SELECT console_refresh(%s,TRUE)", (flow_id,))
