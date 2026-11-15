"""Build lifecycle fixtures through the same locked events as runtime callers."""

from app.domain.flow_policy import FlowEvent
from app.domain.models import Flow, Job, JobKind
from app.persistence.records import required
from app.persistence.store import Store


def apply_flow_event(store: Store, flow: Flow, event: FlowEvent) -> Flow:
    """Re-read after claims or enqueue, which can leave a fixture's snapshot stale."""
    with store.flows.locked(flow.id) as context:
        return store.flows.using(context.database).transition(required(context.flow), event)


def enqueue_job(store: Store, flow_id: int, kind: JobKind, payload: dict | None = None) -> Job:
    """Queue fixture work using fresh facts and the same lock context as runtime callers."""
    with store.flows.locked(flow_id) as context:
        return store.jobs.using(context.database).enqueue_job(required(context.flow), kind, payload)
