"""Persistence for prepared results and ordered, fenced external effects."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.contracts.result_models import EffectReceipt, EffectRecord, EffectState, ResultPlan
from app.domain.github_models import GitHubAdapterError
from app.domain.github_retry import retry_delay
from app.domain.models import Job

if TYPE_CHECKING:
    from app.persistence.store import Store


RESULT_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_source ON jobs(source_job_id, source_key)
  WHERE source_job_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_codex_runs_source ON codex_runs(source_job_id)
  WHERE source_job_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS result_applications (
  job_id BIGINT PRIMARY KEY REFERENCES jobs(id),
  digest TEXT NOT NULL,
  plan JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS result_effects (
  job_id BIGINT NOT NULL REFERENCES result_applications(job_id),
  effect_key TEXT NOT NULL,
  position INTEGER NOT NULL,
  payload JSONB NOT NULL,
  state TEXT NOT NULL,
  receipt JSONB,
  claimed_by TEXT,
  claim_expires_at TIMESTAMPTZ,
  dispatched_at TIMESTAMPTZ,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL,
  last_error TEXT,
  PRIMARY KEY(job_id, effect_key),
  UNIQUE(job_id, position)
);
CREATE INDEX IF NOT EXISTS ix_result_effects_due ON result_effects(next_attempt_at)
  WHERE state NOT IN ('succeeded','failed','skipped');
"""


class ResultStore:
    """Share the owning Store's connection, including its explicit transaction when preparing."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def get_plan(self, job_id: int) -> ResultPlan | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT plan FROM result_applications WHERE job_id=%s",
                (job_id,),
            ).fetchone()
            return ResultPlan.model_validate(row["plan"]) if row else None

    def defer_verification(self, job: Job, error: GitHubAdapterError) -> None:
        """Retain a server deadline only while this accepted result still needs verification."""
        with self.store.transaction() as tx:
            tx.lock_workflow(job.flow_id)
            current = tx.lock_job(job.id)
            if (current is None or current.result_digest != job.result_digest
                    or current.result_applied_at is not None or tx.results.get_plan(job.id) is not None):
                return
            failures = current.verify_failures + 1
            next_at = tx.now() + timedelta(seconds=retry_delay(failures, tx.now(), error))
            with tx.connection() as connection:
                connection.execute(
                    "UPDATE jobs SET verify_failures=%s,verify_next_at=GREATEST(verify_next_at,%s),"
                    " verify_last_error=%s,last_error=%s WHERE id=%s",
                    (failures, next_at, str(error), str(error), job.id),
                )

    def prepare(self, plan: ResultPlan) -> None:
        """Reserve a frozen plan and its effects atomically; caller holds the workflow/job locks."""
        now = self.store.now()
        with self.store.connection() as connection:
            inserted = connection.execute(
                "INSERT INTO result_applications(job_id,digest,plan,created_at)"
                " VALUES (%s,%s,%s,%s) ON CONFLICT(job_id) DO NOTHING RETURNING job_id",
                (plan.job_id, plan.digest, Jsonb(plan.model_dump(mode="json")), now),
            ).fetchone()
            if inserted is None:
                previous = self.get_plan(plan.job_id)
                if previous is None or previous.digest != plan.digest:
                    raise ValueError("result plan digest conflicts with persisted result")
                return
            connection.execute(
                "UPDATE jobs SET verify_failures=0,verify_next_at=NULL,verify_last_error=NULL WHERE id=%s",
                (plan.job_id,),
            )
            for position, effect in enumerate(plan.effects):
                connection.execute(
                    "INSERT INTO result_effects(job_id,effect_key,position,payload,state,"
                    " next_attempt_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        plan.job_id,
                        effect.key,
                        position,
                        Jsonb(effect.model_dump(mode="json")),
                        EffectState.PENDING,
                        now,
                    ),
                )

    def list_effects(self, job_id: int) -> list[EffectRecord]:
        with (
            self.store.connection() as connection,
            connection.cursor(row_factory=class_row(EffectRecord)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM result_effects WHERE job_id=%s ORDER BY position",
                (job_id,),
            ).fetchall()

    def claim(self, job_id: int, token: str) -> EffectRecord | None:
        """Claim only the first unfinished effect, including reconciliation of an expired sender."""
        now = self.store.now()
        with (
            self.store.connection() as connection,
            connection.cursor(row_factory=class_row(EffectRecord)) as cursor,
        ):
            return cursor.execute(
                "UPDATE result_effects SET state=%s,claimed_by=%s,claim_expires_at=%s,"
                " attempts=attempts+1 WHERE (job_id,position)=(SELECT job_id,position"
                " FROM result_effects WHERE job_id=%s AND state NOT IN (%s,%s)"
                " ORDER BY position LIMIT 1) AND state<>%s AND next_attempt_at<=%s"
                " AND (claimed_by IS NULL OR claim_expires_at<=%s) RETURNING *",
                (
                    EffectState.RUNNING,
                    token,
                    now + timedelta(minutes=5),
                    job_id,
                    EffectState.SUCCEEDED,
                    EffectState.SKIPPED,
                    EffectState.FAILED,
                    now,
                    now,
                ),
            ).fetchone()

    def mark_dispatched(self, record: EffectRecord) -> None:
        """Persist the uncertain-outcome boundary before making a potentially non-idempotent call."""
        with self.store.connection() as connection:
            result = connection.execute(
                "UPDATE result_effects SET dispatched_at=COALESCE(dispatched_at,%s)"
                " WHERE job_id=%s AND effect_key=%s AND claimed_by=%s AND state=%s",
                (
                    self.store.now(),
                    record.job_id,
                    record.effect_key,
                    record.claimed_by,
                    EffectState.RUNNING,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("effect claim expired before dispatch")
        record.dispatched_at = record.dispatched_at or self.store.now()

    def finish(
        self,
        record: EffectRecord,
        state: EffectState,
        *,
        receipt: EffectReceipt | None = None,
        error: str | None = None,
        delay: float = 0,
        definitely_unsent: bool = False,
    ) -> None:
        with self.store.connection() as connection:
            result = connection.execute(
                "UPDATE result_effects SET state=%s,receipt=%s,last_error=%s,"
                " next_attempt_at=%s,claimed_by=NULL,claim_expires_at=NULL,"
                " dispatched_at=CASE WHEN %s THEN NULL ELSE dispatched_at END"
                " WHERE job_id=%s AND effect_key=%s AND claimed_by=%s AND state=%s",
                (
                    state,
                    Jsonb(receipt.model_dump(mode="json")) if receipt else None,
                    error,
                    self.store.now() + timedelta(seconds=delay),
                    definitely_unsent,
                    record.job_id,
                    record.effect_key,
                    record.claimed_by,
                    EffectState.RUNNING,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("effect completion lost its claim")

    def skip_remaining(self, job_id: int, reason: str) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE result_effects SET state=%s,last_error=%s,claimed_by=NULL,"
                " claim_expires_at=NULL WHERE job_id=%s AND state NOT IN (%s,%s)",
                (EffectState.SKIPPED, reason, job_id, EffectState.SUCCEEDED, EffectState.FAILED),
            )

    def retire_for_merge(self, job_id: int) -> None:
        """Skip unsent work, retaining active senders and uncertain writes for read-only recovery."""
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE result_effects SET state=CASE WHEN dispatched_at IS NULL THEN %s ELSE %s END,"
                " last_error=%s,claimed_by=NULL,claim_expires_at=NULL,next_attempt_at=%s"
                " WHERE job_id=%s AND state NOT IN (%s,%s)"
                " AND (claimed_by IS NULL OR claim_expires_at<=%s)"
                " AND (dispatched_at IS NULL OR state=%s)",
                (EffectState.SKIPPED, EffectState.UNCERTAIN, "PR merged; no further writes",
                 self.store.now(), job_id, EffectState.SUCCEEDED, EffectState.SKIPPED,
                 self.store.now(), EffectState.FAILED),
            )

    def get_pending_plan(self, flow_id: int) -> ResultPlan | None:
        """Find a unique unapplied inbox plan; the caller holds the workflow lock."""
        with self.store.connection() as connection:
            jobs = connection.execute(
                "SELECT id FROM jobs WHERE flow_id=%s AND result_received_at IS NOT NULL"
                " AND result_applied_at IS NULL FOR UPDATE",
                (flow_id,),
            ).fetchall()
            return self.get_plan(jobs[0]["id"]) if len(jobs) == 1 else None

    def retry_failed_effects(self, job_id: int) -> bool:
        """Rearm definite rejections after control validates the plan under its workflow lock."""
        with self.store.connection() as connection:
            effects = self.list_effects(job_id)
            failed = [effect for effect in effects if effect.state is EffectState.FAILED]
            if (not failed or any(effect.dispatched_at is not None for effect in failed)
                    or any(effect.state in {EffectState.RUNNING, EffectState.UNCERTAIN}
                           for effect in effects)):
                return False
            connection.execute(
                "UPDATE result_effects SET state=%s,next_attempt_at=%s,last_error=NULL"
                " WHERE job_id=%s AND state=%s",
                (EffectState.RETRY, self.store.now(), job_id, EffectState.FAILED),
            )
            connection.execute("UPDATE jobs SET last_error=NULL WHERE id=%s", (job_id,))
            return True

    def recover_claims(self) -> None:
        """Called once after the old single-serve process has stopped, before accepting work."""
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE result_effects SET state=CASE WHEN dispatched_at IS NULL THEN %s ELSE %s END,"
                " claimed_by=NULL,claim_expires_at=NULL WHERE state=%s",
                (EffectState.PENDING, EffectState.UNCERTAIN, EffectState.RUNNING),
            )
