"""Inference Job exhaustion notices with independent activity-ledger snapshot paging."""

import base64
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.contracts.console import ConsoleFailure, InferenceNotice, InferenceNoticePage
from app.domain.inference_models import InferenceSummary
from app.github.console import Observation
from app.persistence.activity import ActivityStore
from app.persistence.records import Row
from app.persistence.repository import Repository


class NoticeCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: Literal["inference-notices"] = "inference-notices"
    flow: int = Field(gt=0)
    snapshot: int = Field(ge=0)
    time: datetime
    key: str = Field(min_length=1, max_length=512)

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, value: str, flow_id: int) -> "NoticeCursor":
        try:
            result = cls.model_validate_json(base64.b64decode(value, altchars=b"-_", validate=True))
            if result.flow != flow_id:
                raise ValueError("foreign cursor")
            return result
        except (ValueError, ValidationError):
            raise ConsoleFailure("cursor_invalid") from None


def _notice(row: Row) -> InferenceNotice:
    metadata = row["metadata"]
    historical = (not row["live"] or row["batch_state"] not in {"ready", "waiting"}
                  or (row["batch_state"] == "ready" and row["has_decision"]))
    if historical:
        condition = "该批次已继续处理、结束或被取代；这是一条历史通知，不会因此重新推理。"
    elif row["batch_state"] == "ready":
        condition = "批次仍待推理；后续扫描可在有可用推理后端时继续尝试。"
    else:
        condition = "批次正在等待新的有效 PR 反馈等现有触发；仅修复配置或刷新页面不会重新推理。"
    return InferenceNotice(
        key=row["key"], batch_id=metadata["batch_id"], occurred_at=row["occurred_at"],
        updated_at=row["updated_at"], attempts=metadata["attempts"], max_attempts=metadata["max_attempts"],
        last_failure_code=metadata["last_failure_code"], last_worker_id=metadata["last_worker_id"],
        backend=metadata["backend"], stop_reason=metadata["stop_reason"],
        elapsed_ms=metadata["elapsed_ms"], historical=historical, next_condition=condition,
    )


class InferenceNoticeStore(Repository):
    """Append stable summaries and page the latest version of each batch within a snapshot."""

    def append(self, flow_id: int, batch_id: int, summary: InferenceSummary) -> bool:
        if not summary.notify:
            return False
        return ActivityStore(self._db).append(flow_id, Observation(
            key=f"inference-exhausted:{batch_id}", source="flow", kind="inference_exhausted",
            author="controller", occurred_at=summary.finished_at, updated_at=summary.finished_at,
            title="推理尝试未成功", metadata={
                "batch_id": batch_id, "logical_call_id": summary.logical_call_id,
                "attempts": summary.attempts, "max_attempts": summary.max_attempts,
                "stop_reason": summary.stop_reason, "last_failure_code": summary.last_failure_code,
                "last_worker_id": summary.last_worker_id, "backend": summary.backend,
                "elapsed_ms": summary.elapsed_ms,
            },
        ))

    def page(self, flow_id: int, *, cursor: str | None = None, snapshot: int | None = None,
             limit: int = 20) -> InferenceNoticePage:
        if not 1 <= limit <= 100:
            raise ConsoleFailure("limit_invalid")
        position = NoticeCursor.decode(cursor, flow_id) if cursor else None
        with self._db.connection() as connection:
            head = connection.execute(
                "SELECT activity_version FROM console_flows WHERE flow_id=%s", (flow_id,),
            ).fetchone()
            if head is None:
                raise ConsoleFailure("flow_not_found", 404)
            if position is not None:
                if snapshot is not None and snapshot != position.snapshot:
                    raise ConsoleFailure("cursor_invalid")
                snapshot = position.snapshot
            if snapshot is None:
                snapshot = head["activity_version"]
            if not 0 <= snapshot <= head["activity_version"]:
                raise ConsoleFailure("snapshot_invalid")
            after = " AND (current.updated_at,current.key)<(%s,%s)" if position else ""
            parameters: list[object] = [flow_id, snapshot]
            if position:
                parameters.extend((position.time, position.key))
            parameters.append(limit + 1)
            rows = connection.execute(
                "WITH current AS (SELECT DISTINCT ON (key) flow_id,key,metadata,occurred_at,updated_at"
                " FROM console_activity WHERE flow_id=%s AND version<=%s"
                " AND source='flow' AND kind='inference_exhausted' ORDER BY key,version DESC)"
                " SELECT current.*,b.state AS batch_state,b.decision IS NOT NULL AS has_decision,"
                " coalesce(f.state IN ('queued','running','review') AND f.archive_reason IS NULL"
                " AND d.state='open' AND f.current_attempt_id=b.attempt_id,FALSE) AS live"
                " FROM current JOIN flows f ON f.id=current.flow_id"
                " LEFT JOIN flow_deliveries d ON d.flow_id=current.flow_id"
                " LEFT JOIN review_batches b ON b.id=(current.metadata->>'batch_id')::bigint"
                " AND b.flow_id=current.flow_id WHERE TRUE"
                + after
                + " ORDER BY current.updated_at DESC,current.key DESC LIMIT %s",
                parameters,
            ).fetchall()
        more = len(rows) > limit
        items = [_notice(row) for row in rows[:limit]]
        return InferenceNoticePage(
            items=items, snapshot=snapshot, has_more=more,
            next_cursor=NoticeCursor(flow=flow_id, snapshot=snapshot, time=items[-1].updated_at,
                                     key=items[-1].key).encode() if more else None,
        )
