"""Flow delivery identity and the idempotent tool-call ledger."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.domain.delivery_models import DeliveryReceipt
from app.domain.models import (
    DeliveryState,
    FlowDelivery,
    FlowToolCall,
)
from app.persistence.records import required

if TYPE_CHECKING:
    from app.persistence.store import Store


class DeliveryStore:
    """Flow delivery identity and the idempotent tool-call ledger."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def get_or_create_delivery(self, flow_id: int, branch: str) -> FlowDelivery:
        now = self.store.now()
        with (
            self.store.connection() as connection,
            connection.cursor(row_factory=class_row(FlowDelivery)) as cursor,
        ):
            row = cursor.execute(
                "INSERT INTO flow_deliveries(flow_id, branch, state, version, created_at,"
                " updated_at) VALUES (%s,%s,%s,0,%s,%s)"
                " ON CONFLICT(flow_id) DO UPDATE SET branch=flow_deliveries.branch"
                " RETURNING *",
                (flow_id, branch, DeliveryState.NONE, now, now),
            ).fetchone()
        delivery = required(row)
        if delivery.branch != branch:
            raise ValueError(f"flow {flow_id} is already bound to branch {delivery.branch}")
        return delivery

    def get_delivery(self, flow_id: int) -> FlowDelivery | None:
        with (
            self.store.connection() as connection,
            connection.cursor(row_factory=class_row(FlowDelivery)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM flow_deliveries WHERE flow_id=%s", (flow_id,)
            ).fetchone()

    def update_delivery(self, delivery: FlowDelivery) -> None:
        delivery.updated_at = self.store.now()
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE flow_deliveries SET branch=%s, base_branch=%s, state=%s,"
                " pr_number=%s, url=%s, head_sha=%s, title=%s, body=%s,"
                " merged_at=%s, merge_commit_sha=%s,"
                " version=%s, updated_at=%s"
                " WHERE flow_id=%s",
                (
                    delivery.branch,
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
                    delivery.flow_id,
                ),
            )

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

        now = self.store.now()
        with (
            self.store.connection() as connection,
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
        now = self.store.now()
        with self.store.connection() as connection:
            result = connection.execute(
                "UPDATE flow_tool_calls SET response=%s, updated_at=%s WHERE call_id=%s",
                (Jsonb(response.model_dump(mode="json")), now, call_id),
            )
        if result.rowcount != 1:
            raise ValueError(f"tool call {call_id} does not exist")

    def get_tool_call(self, call_id: str) -> FlowToolCall | None:
        with (
            self.store.connection() as connection,
            connection.cursor(row_factory=class_row(FlowToolCall)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM flow_tool_calls WHERE call_id=%s", (call_id,)
            ).fetchone()
