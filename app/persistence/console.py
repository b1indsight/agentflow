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


def _explanation(row: Row) -> tuple[str, str]:
    """Explain persisted blockers, without exposing errors or promising retry eligibility."""
    facts = row["facts"]
    if facts["lifecycle"] == "succeeded":
        return "Flow 已归档完成", "无需后续动作"
    if facts["lifecycle"] == "cancelled":
        return "Flow 已取消", "确认在途任务的停止确认与结果对账完成后，再评估是否重试"
    if facts["lifecycle"] == "failed":
        return "Flow 已失败", "检查执行记录与恢复状态，排除故障后由运维核验是否允许重试"
    # A closed delivery is a hold: retry must not bypass reopen/baseline reconciliation.
    if facts["delivery_state"] == "closed":
        return (
            "PR 已关闭，编码与修订已暂停",
            "重新打开 PR 并完成执行状态及 head/base 对账后才能继续",
        )
    label = {"initial": "首轮编码", "revision": "修订", "archive": "归档"}.get(
        row["diagnostic_kind"], "任务"
    )
    job = f"任务 #{row['diagnostic_job_id']}（{label}）"
    if row["effect_failed"]:
        return (
            f"{job}的 GitHub 写入失败，结果尚未应用",
            "核对远端写入结果与权限，排除故障后由运维核验是否允许重试；不要重复执行编码",
        )
    if row["result_pending"]:
        if row["verification_retry"]:
            return (
                f"{job}的结果校验暂时失败，已安排重试",
                "等待 Control 按退避计划自动重试校验；持续失败时检查 GitHub 权限与可用性",
            )
        return (
            f"{job}的结果已收到，尚未完成校验与应用",
            "等待 Control 完成结果校验与应用及相关 GitHub 写入",
        )
    if row["diagnostic_state"] == "failed":
        condition = (
            "检查执行记录；归档只能在原 Worker 与执行状态确认安全后由运维重试"
            if row["diagnostic_kind"] == "archive"
            else "检查执行记录与恢复状态，排除故障后由运维核验是否允许重试"
        )
        return f"{job}：{label}失败", condition
    if row["diagnostic_state"] == "running":
        if row["recovering"]:
            return (
                f"{job}：{label}执行状态待确认",
                "等待原 Worker 恢复连接并确认执行或结果；不能强制释放绑定或启动重复执行",
            )
        if not row["execution_started"]:
            if row["preparation_retry"]:
                return (
                    f"{job}准备失败，已安排重试",
                    "等待 Control 按退避计划自动重试准备；持续失败时检查执行记录",
                )
            return (
                f"{job}等待 Worker 确认开始",
                "等待 Worker 确认执行；未收到开始确认不代表可以安全重派",
            )
        return f"{job}：{label}正在执行", "等待 Worker 返回执行结果"
    if row["diagnostic_state"] == "queued":
        if row["diagnostic_kind"] == "archive":
            return f"{job}等待原 Worker 接收", "等待原 Worker 在线且无未确认执行，再接收归档任务"
        if facts["worker_id"] is None or facts["worker_historical"]:
            return f"{job}等待分配 Worker", "等待可用 Worker 接收任务"
        return f"{job}等待绑定的 Worker 接收", "等待该 Worker 可接收任务并完成派发"
    if facts["delivery_state"] == "merged":
        return "PR 已合并，尚未完成归档", "检查归档执行记录与原 Worker 绑定，等待归档完成"
    if facts["phase"] == "review":
        return (
            "PR 已打开，当前没有待执行任务",
            "等待 GitHub 审核或人工合并；新修订请求会触发后续任务",
        )
    if facts["phase"] == "queued":
        return "尚未绑定可用执行资源", "检查任务排队与 Worker 可用状态"
    return "当前没有可确认的执行进展", "刷新详情并检查执行记录；不根据阶段推断执行进度"


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
                "SELECT c.flow_id,c.facts-'delivery_body'-'delivery_title' AS facts,"
                "coalesce(c.title,c.facts->>'delivery_title') AS title,c.state_version,diagnostic.*"
                " FROM console_flows c LEFT JOIN LATERAL ("
                " SELECT j.id AS diagnostic_job_id,j.kind AS diagnostic_kind,"
                " j.state AS diagnostic_state,j.started_at IS NOT NULL AS execution_started,"
                " j.recovery_deadline IS NOT NULL AS recovering,"
                " j.prepare_next_at IS NOT NULL AS preparation_retry,"
                " j.verify_next_at IS NOT NULL AS verification_retry,"
                " (j.result_received_at IS NOT NULL AND j.result_applied_at IS NULL) AS result_pending,"
                " EXISTS (SELECT 1 FROM result_effects e WHERE e.job_id=j.id"
                " AND j.result_applied_at IS NULL AND e.state='failed') AS effect_failed"
                " FROM jobs j WHERE j.flow_id=c.flow_id"
                " ORDER BY effect_failed DESC,result_pending DESC,(j.state='running') DESC,"
                " (j.state='queued') DESC,j.id DESC LIMIT 1"
                ") diagnostic ON TRUE WHERE c.flow_id=%s",
                (flow_id,),
            ).fetchone()
        if row is None:
            raise ConsoleFailure("flow_not_found", 404)
        flow = _summary(row)
        reason, condition = _explanation(row)
        return FlowDetail(
            **flow.model_dump(),
            reason=reason,
            next_condition=condition,
            sources=self.sources(flow_id),
        )

    def sources(self, flow_id: int) -> list[SourceStatus]:
        """Summarize all child connections without hiding coverage behind a row limit."""
        # Worker children describe historical coverage, not current connection health.
        # Keep their gaps in the status, but let the root own freshness/current errors.
        with self._db.connection() as connection:
            rows = connection.execute(
                "WITH grouped AS (SELECT split_part(source,':',1) AS source,"
                " CASE WHEN bool_or(status='unavailable') THEN 'unavailable'"
                " WHEN bool_or(status='syncing') THEN 'syncing'"
                " WHEN bool_or(pending) THEN 'pending'"
                " WHEN bool_or(status='partial') THEN 'partial' ELSE 'available' END AS status,"
                " coalesce(min(reason) FILTER (WHERE source='worker' AND status='unavailable'),"
                " min(reason)) AS reason,"
                " CASE WHEN split_part(source,':',1)='worker'"
                " THEN min(last_success_at) FILTER (WHERE source='worker')"
                " ELSE min(last_success_at) END AS last_success_at"
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
