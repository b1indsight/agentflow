"""Flow delivery identity and the idempotent tool-call ledger."""

from __future__ import annotations

from collections.abc import Mapping

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.domain.delivery_models import DeliveryReceipt
from app.domain.models import (
    DeliveryState,
    FlowDelivery,
    FlowToolCall,
)
from app.persistence.records import required
from app.persistence.repository import Repository

_DELIVERY = (
    "flow_id,id AS attempt_id,delivery_branch AS branch,delivery_base_branch AS base_branch,delivery_state AS state,"
    "delivery_pr_number AS pr_number,delivery_url AS url,delivery_head_sha AS head_sha,delivery_title AS title,"
    "delivery_body AS body,delivery_merged_at AS merged_at,delivery_merge_commit_sha AS merge_commit_sha,"
    "delivery_version AS version,delivery_created_at AS created_at,delivery_updated_at AS updated_at"
)


class DeliveryStore(Repository):
    """Flow delivery identity and the idempotent tool-call ledger."""

    def get_or_create_delivery(self, attempt_id: int, branch: str) -> FlowDelivery:
        """Bind this attempt's immutable branch, or return the existing binding."""
        now = self._db.now()
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(FlowDelivery)) as cursor,
        ):
            row = cursor.execute(
                "UPDATE attempts SET delivery_branch=%s,delivery_state=%s,delivery_version=0,"
                " delivery_created_at=%s,delivery_updated_at=%s WHERE id=%s AND delivery_branch IS NULL"
                f" RETURNING {_DELIVERY}",
                (branch, DeliveryState.NONE, now, now, attempt_id),
            ).fetchone()
            if row is None:
                row = cursor.execute(f"SELECT {_DELIVERY} FROM attempts WHERE id=%s", (attempt_id,)).fetchone()
        delivery = required(row)
        if delivery.branch != branch:
            raise ValueError(f"attempt {attempt_id} is already bound to branch {delivery.branch}")
        return delivery

    def get_delivery(self, flow_id: int) -> FlowDelivery | None:
        """Read only the selected attempt's delivery, including while archived."""
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(FlowDelivery)) as cursor,
        ):
            return cursor.execute(
                f"SELECT {_DELIVERY} FROM attempts WHERE delivery_branch IS NOT NULL"
                " AND id=(SELECT current_attempt_id FROM flows WHERE id=%s)", (flow_id,),
            ).fetchone()

    def get_attempt_delivery(self, attempt_id: int) -> FlowDelivery | None:
        """Read a specific attempt without following its flow's current pointer."""
        with self._db.connection() as connection:
            return connection.cursor(row_factory=class_row(FlowDelivery)).execute(
                f"SELECT {_DELIVERY} FROM attempts WHERE id=%s AND delivery_branch IS NOT NULL", (attempt_id,),
            ).fetchone()

    def delivery_by_pr(self, repo: str, pr_number: int) -> FlowDelivery | None:
        """Find the original delivery even if the flow has since selected a new attempt."""
        with self._db.connection() as connection:
            rows = connection.cursor(row_factory=class_row(FlowDelivery)).execute(
                f"SELECT {_DELIVERY} FROM attempts WHERE delivery_pr_number=%s AND delivery_branch IS NOT NULL"
                " AND flow_id IN (SELECT id FROM flows WHERE lower(repo)=lower(%s)) LIMIT 2", (pr_number, repo),
            ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"multiple attempts claim PR {repo}#{pr_number}")
        return rows[0] if rows else None

    def update_delivery(self, delivery: FlowDelivery) -> None:
        delivery.updated_at = self._db.now()
        with self._db.connection() as connection:
            result = connection.execute(
                "UPDATE attempts SET delivery_base_branch=%s,delivery_state=%s,"
                " delivery_pr_number=%s,delivery_url=%s,delivery_head_sha=%s,delivery_title=%s,delivery_body=%s,"
                " delivery_merged_at=%s,delivery_merge_commit_sha=%s,delivery_version=%s,delivery_updated_at=%s"
                " WHERE id=%s AND flow_id=%s AND delivery_branch=%s",
                (
                    delivery.base_branch,
                    delivery.state,
                    delivery.pr_number,
                    delivery.url,
                    delivery.head_sha,
                    delivery.title,
                    delivery.body,
                    delivery.merged_at,
                    delivery.merge_commit_sha,
                    delivery.version,
                    delivery.updated_at,
                    delivery.attempt_id,
                    delivery.flow_id,
                    delivery.branch,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("delivery does not belong to this attempt")

    def reserve_tool_call(
        self,
        *,
        call_id: str,
        flow_id: int,
        attempt_id: int,
        job_id: int,
        name: str,
        arguments: Mapping[str, object],
    ) -> tuple[FlowToolCall, bool]:
        """Create an idempotency record or return an exactly matching prior call."""

        now = self._db.now()
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(FlowToolCall)) as cursor,
        ):
            row = cursor.execute(
                "INSERT INTO flow_tool_calls(call_id, flow_id, attempt_id, job_id, name,"
                " arguments, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT(call_id) DO NOTHING RETURNING *",
                (call_id, flow_id, attempt_id, job_id, name, Jsonb(dict(arguments)), now, now),
            ).fetchone()
            created = row is not None
            if row is None:
                row = cursor.execute(
                    "SELECT * FROM flow_tool_calls WHERE call_id=%s", (call_id,)
                ).fetchone()
        call = required(row)
        if (
            call.flow_id != flow_id
            or call.attempt_id != attempt_id
            or call.job_id != job_id
            or call.name != name
            or call.arguments != dict(arguments)
        ):
            raise ValueError(f"tool call {call_id} conflicts with its persisted request")
        return call, created

    def complete_tool_call(
        self, call_id: str, response: DeliveryReceipt
    ) -> None:
        now = self._db.now()
        with self._db.connection() as connection:
            result = connection.execute(
                "UPDATE flow_tool_calls SET response=%s, updated_at=%s WHERE call_id=%s",
                (Jsonb(response.model_dump(mode="json")), now, call_id),
            )
        if result.rowcount != 1:
            raise ValueError(f"tool call {call_id} does not exist")

    def get_tool_call(self, call_id: str) -> FlowToolCall | None:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(FlowToolCall)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM flow_tool_calls WHERE call_id=%s", (call_id,)
            ).fetchone()
