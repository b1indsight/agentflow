"""Deterministic effect persistence for coordinator fault tests; SQL has separate integration tests."""

from datetime import timedelta

from app.models import Flow, utcnow
from app.result_models import EffectReceipt, EffectRecord, EffectState, ResultPlan


class MemoryResults:
    def __init__(self) -> None:
        self.clock = utcnow()
        self.plans: dict[int, ResultPlan] = {}
        self.records: dict[int, list[EffectRecord]] = {}

    def get_plan(self, job_id: int) -> ResultPlan | None:
        return self.plans.get(job_id)

    def retry_revision_effects(self, flow: Flow) -> bool:
        # Verified revision recovery is exercised against PostgreSQL.
        assert not any(plan.revision is not None for plan in self.plans.values())
        return False

    def prepare(self, plan: ResultPlan) -> None:
        if plan.job_id in self.plans:
            assert self.plans[plan.job_id].digest == plan.digest
            return
        self.plans[plan.job_id] = plan.model_copy(deep=True)
        self.records[plan.job_id] = [
            EffectRecord(
                job_id=plan.job_id,
                effect_key=effect.key,
                position=index,
                payload=effect,
                state=EffectState.PENDING,
                next_attempt_at=self.clock,
            )
            for index, effect in enumerate(plan.effects)
        ]

    def list_effects(self, job_id: int) -> list[EffectRecord]:
        return [record.model_copy(deep=True) for record in self.records.get(job_id, [])]

    def claim(self, job_id: int, token: str) -> EffectRecord | None:
        for record in self.records[job_id]:
            if record.state in {EffectState.SUCCEEDED, EffectState.SKIPPED}:
                continue
            if (
                record.state is EffectState.FAILED
                or record.next_attempt_at > self.clock
                or (
                    record.claimed_by is not None
                    and record.claim_expires_at is not None
                    and record.claim_expires_at > self.clock
                )
            ):
                return None
            record.state = EffectState.RUNNING
            record.claimed_by = token
            record.claim_expires_at = self.clock + timedelta(minutes=5)
            record.attempts += 1
            return record.model_copy(deep=True)
        return None

    def _owned(self, record: EffectRecord) -> EffectRecord:
        persisted = self.records[record.job_id][record.position]
        assert persisted.claimed_by == record.claimed_by
        assert persisted.state is EffectState.RUNNING
        return persisted

    def mark_dispatched(self, record: EffectRecord) -> None:
        persisted = self._owned(record)
        persisted.dispatched_at = persisted.dispatched_at or self.clock
        record.dispatched_at = persisted.dispatched_at

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
        persisted = self._owned(record)
        persisted.state, persisted.receipt, persisted.last_error = state, receipt, error
        persisted.next_attempt_at = self.clock + timedelta(seconds=delay)
        persisted.claimed_by = persisted.claim_expires_at = None
        if definitely_unsent:
            persisted.dispatched_at = None

    def skip_remaining(self, job_id: int, reason: str) -> None:
        for record in self.records.get(job_id, []):
            if record.state not in {EffectState.SUCCEEDED, EffectState.FAILED}:
                record.state, record.last_error = EffectState.SKIPPED, reason
                record.claimed_by = record.claim_expires_at = None

    def recover_claims(self) -> None:
        for records in self.records.values():
            for record in records:
                if record.state is EffectState.RUNNING:
                    record.state = (
                        EffectState.UNCERTAIN if record.dispatched_at else EffectState.PENDING
                    )
                    record.claimed_by = record.claim_expires_at = None
