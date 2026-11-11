"""A validation error can retire only the exact, unstarted and still-authorized dispatch."""

from datetime import timedelta

import pytest

from app.contracts.protocol import ExecutionStopped, Failed
from app.contracts.receipts import decode_result_inbox
from app.controler.workflow.service import ControlService
from app.domain.models import FlowState, JobKind, JobState
from tests.workspace_fixtures import register_worker, report_settled


@pytest.mark.parametrize("race", [None, "started", "result", "stop", "epoch", "lease", "binding"])
def test_assignment_rejection_preserves_competing_evidence(store, race):
    flow = store.flows.create_flow("owner/repo", 1, 1)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    register_worker(store, "worker", repo=flow.repo, epoch="epoch")
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("worker", "token", "epoch")
    assert store.stops.authorize_dispatch(job.id, "worker", "token", "epoch")
    if race == "started":
        assert store.workers.start_assignment(job.id, attempt.id, "worker", "token", "epoch")
    elif race == "result":
        store.workers.record_assignment_result(
            job.id,
            "worker",
            "token",
            "epoch",
            Failed(job_id=job.id, assignment_token="token", error="original"),
        )
    elif race == "stop":
        ControlService(database=store.database).cancel(flow)
    elif race == "epoch":
        register_worker(store, "worker", repo=flow.repo, epoch="replacement")
    elif race == "lease":
        store._db._clock = lambda: job.created_at + timedelta(days=1)
    elif race == "binding":
        ControlService(database=store.database).cancel(flow)
        stop = store.stops.pending("worker")
        assert store.stops.confirm(
            "worker",
            ExecutionStopped(
                **stop.fence().model_dump(),
                execution_nonce="owned",
            ),
            "epoch",
        )
        report_settled(store, job.id, epoch="epoch", stopped=True)
        assert store.workers.get_flow_execution(flow.id).released_at is not None
    before = store.jobs.get_job(job.id)
    rejected = store.workers.reject_assignment(job.id, "worker", "token", "epoch")
    after = store.jobs.get_job(job.id)
    assert rejected is (race is None)
    if race is not None:
        assert after == before
    else:
        inbox = decode_result_inbox(
            after.result_payload,
            job_id=job.id,
            assignment_token="token",
            result_digest=after.result_digest,
        )
        assert inbox.source == "control"
        assert inbox.terminal.error == "assignment payload failed validation"
        assert after.started_at is None and after.result_applied_at is None
        assert not store.workers.reject_assignment(job.id, "worker", "token", "epoch")
        # Explicit validation refusal created no local journal to ACK or stop.
        assert store.jobs.complete_persisted_result(job.id, JobState.FAILED)
        assert all(w.current_job_id is None for w in store.workers.list_workspaces("worker"))
        ControlService(database=store.database).cancel(flow)
        assert store.stops.pending_for_worker("worker") == []
        assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
