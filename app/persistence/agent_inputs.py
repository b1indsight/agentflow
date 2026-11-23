"""Queue owner messages on their existing, attempt-scoped conversation records."""

import re

from psycopg.types.json import Jsonb

from app.contracts.input_images import image_ref
from app.domain.models import Flow, Job, JobState
from app.github.console import Observation
from app.persistence.activity import ActivityStore
from app.persistence.repository import Repository


class AgentInputStore(Repository):
    """Writers hold the workflow lock; taking input and freezing a job share one transaction."""

    def save_images(self, flow_id: int, images: list[str]) -> None:
        with self._db.connection() as conn:
            for url in images:
                conn.execute("INSERT INTO flow_input_images VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                             (flow_id, image_ref(url), url))

    def image_budget_exceeded(self, flow_id: int) -> bool:
        # Pending inputs can be combined into one assignment. Bound the worst-case wire size.
        with self._db.connection() as conn:
            row = conn.execute("SELECT sum(octet_length(data_url)) AS size FROM flow_input_images WHERE flow_id=%s",
                               (flow_id,)).fetchone()
            assert row is not None
            return bool(row["size"] > 8 * 1024 * 1024)

    def images_for(self, flow_id: int, text: str) -> list[str]:
        refs = re.findall(r"^image:[a-f0-9]{64}$", text, re.MULTILINE)
        if not refs:
            return []
        with self._db.connection() as conn:
            return [row["data_url"] for row in conn.execute(
                "SELECT data_url FROM flow_input_images WHERE flow_id=%s AND ref=ANY(%s) ORDER BY array_position(%s,ref)",
                (flow_id, refs, refs),
            )]

    def previous(self, flow_id: int, request_id: str) -> str | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT body FROM console_activity WHERE flow_id=%s AND source='agent_task' AND key=%s"
                " ORDER BY version DESC LIMIT 1", (flow_id, f"agent_task:{request_id}"),
            ).fetchone()
            if row is None:
                # A legacy request may be identified only by its original job payload.
                row = conn.execute(
                    "SELECT payload->>'instruction' AS body FROM jobs WHERE flow_id=%s AND "
                    "COALESCE(payload->>'agent_task_request_id',payload->>'console_request_id')=%s "
                    "ORDER BY id LIMIT 1", (flow_id, request_id),
                ).fetchone()
            return row["body"] if row else None

    def record(self, flow: Flow, request_id: str, instruction: str) -> None:
        key = f"agent_task:{request_id}"
        now = self._db.now()
        ActivityStore(self._db).append(flow.id, Observation(
            key=key, source="agent_task", kind="message", attempt_id=flow.current_attempt_id,
            author="user", body=instruction, occurred_at=now, updated_at=now,
            metadata={"attempt_id": flow.current_attempt_id},
        ))
        if flow.archive_reason is None:
            with self._db.connection() as conn:
                conn.execute("UPDATE console_activity SET awaiting_agent=TRUE WHERE flow_id=%s AND key=%s",
                             (flow.id, key))

    def ready_flows(self) -> list[int]:
        with self._db.connection() as conn:
            return [row["flow_id"] for row in conn.execute(
                "SELECT DISTINCT a.flow_id FROM console_activity a JOIN flows f ON f.id=a.flow_id"
                " AND f.current_attempt_id=a.attempt_id WHERE a.awaiting_agent AND f.archive_reason IS NULL"
                " AND f.state IN ('review','blocked') ORDER BY a.flow_id",
            )]

    def take(self, flow: Flow) -> list[str]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "WITH taken AS (UPDATE console_activity SET awaiting_agent=FALSE"
                " WHERE flow_id=%s AND attempt_id=%s AND awaiting_agent RETURNING version,body)"
                " SELECT body FROM taken ORDER BY version", (flow.id, flow.current_attempt_id),
            )
            return [row["body"] for row in rows]

    def initial_content(self, flow: Flow, job: Job, content: str) -> str:
        # A preparation can be replayed before Started. Keep the claimed input on the job.
        with self._db.connection() as conn:
            current = self._db.lock_job(job.id)
            assert current is not None
            notes = (current.payload or {}).get("owner_messages", [])
            if (current.state is JobState.RUNNING and current.assignment_token == job.assignment_token
                    and current.attempt_id == flow.current_attempt_id and current.started_at is None
                    and current.dispatch_authorized_at is None and (pending := self.take(flow))):
                notes = [*notes, *pending]
                conn.execute("UPDATE jobs SET payload=COALESCE(payload,'{}') || %s WHERE id=%s",
                             (Jsonb({"owner_messages": notes}), job.id))
            return "\n\n".join([content, *notes])
