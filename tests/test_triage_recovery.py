"""Recovery regressions found in PR #20's independent review."""

import asyncio
from datetime import timedelta

import pytest

from app.control.gateway import WorkerGateway
from app.control.service import ControlService
from app.models import FlowState, JobState, TriageVerdict, Verdict
from app.protocol import PROTOCOL_VERSION, Succeeded, TriageResult, terminal_digest
from app.review_models import BatchState
from app.stop_store import StopStore
from app.store import Store
from tests.review_memory import judge_decision
from tests.test_coordinator import RecordingGitHub, coordinator
from tests.test_review_batches import claim, feedback, ingest
from tests.test_review_batches import review_flow as review_flow  # noqa: PLC0414 - pytest fixture


def assign_triage(store, flow, clock):
    ingest(store, flow, feedback(clock))
    job = claim(store, clock)[0]
    store.worker_connected("worker-a", PROTOCOL_VERSION, "epoch-a")
    assigned = store.assign_job("worker-a", "token", "epoch-a")
    assert assigned.id == job.id
    return assigned


async def apply_triage(store, flow, job, subject, verdict, mutate=None):
    task = await subject.prepare(job)
    assert store.start_assignment(job.id, flow.current_attempt_id, "worker-a", "token", "epoch-a")
    subject.mark_started(job, task, "worker-a")
    result = TriageResult(batch_id=task.batch_id, feedback_refs=task.feedback,
                          decision=judge_decision(verdict, task.context))
    if mutate:
        mutate(result)
    message = Succeeded(job_id=job.id, assignment_token="token", connection_epoch="epoch-a",
                        result=result)
    assert store.record_assignment_result(job.id, "worker-a", "token", "epoch-a",
        message.model_dump(mode="json"), terminal_digest(message)) == "pending"
    await subject.results.apply(job.id)
    return task


async def test_restart_during_snapshot_preparation_requeues_unstarted_work(store, review_flow):
    flow, clock = review_flow
    job = assign_triage(store, flow, clock)

    class InterruptedGitHub(RecordingGitHub):
        async def get_issue(self, *args):
            raise RuntimeError("serve stopped while fetching context")

    with pytest.raises(RuntimeError, match="serve stopped"):
        await coordinator(store, InterruptedGitHub()).prepare(job)
    assert store.reviews.for_job(job).context is None
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        restarted.recover_abandoned_assignments()
        clock.now += timedelta(seconds=61)
        assert restarted.sweep_expired_assignments() == (1, 0)
        restarted.worker_connected("worker-a", PROTOCOL_VERSION, "epoch-b")
        replacement = restarted.assign_job("worker-a", "next-token", "epoch-b")
        assert replacement.id == job.id
        assert (await coordinator(restarted, RecordingGitHub()).prepare(replacement)).context
    finally:
        restarted.close()


@pytest.mark.parametrize("mismatch", ["refs", "digest"])
async def test_rejected_current_result_remains_retryable(store, review_flow, mismatch):
    flow, clock = review_flow
    job = assign_triage(store, flow, clock)
    github = RecordingGitHub()

    def invalidate(result):
        if mismatch == "refs":
            result.feedback_refs = []
        else:
            result.decision.context_digest = "0" * 64

    await apply_triage(store, flow, job, coordinator(store, github),
        TriageVerdict(verdict=Verdict.IGNORE), invalidate)
    assert store.get_job(job.id).state is JobState.FAILED
    assert store.get_flow(flow.id).state is FlowState.FAILED
    assert github.comments == []
    assert store.reviews.pending(flow.id)
    ControlService(store).retry(flow)
    replacement = store.list_jobs(flow.id)[-1]
    assert replacement.id != job.id
    assert replacement.state is JobState.QUEUED
    assert store.reviews.for_job(replacement).state is BatchState.READY


@pytest.mark.parametrize("sha", ["head_sha", "base_sha"])
async def test_retry_replaces_stale_context_and_keeps_the_original_audit(store, review_flow, sha):
    class ChangedGitHub(RecordingGitHub):
        reads = 0

        async def get_pull_request(self, *args):
            self.reads += 1
            pr = await super().get_pull_request(*args)
            if self.reads == 3:
                # New feedback arrives after the old snapshot but before its rejected publication.
                clock.now += timedelta(seconds=1)
                ingest(store, flow, feedback(clock, body="Update the bound again."), "edited")
                ingest(store, flow, feedback(clock, 102, body="Explain the new bound."), "new-feedback")
            return pr.model_copy(update={sha: "new-sha"}) if self.reads >= 3 else pr

    flow, clock = review_flow
    job = assign_triage(store, flow, clock)
    github = ChangedGitHub()
    subject = coordinator(store, github)
    task = await apply_triage(store, flow, job, subject,
                             TriageVerdict(verdict=Verdict.ANSWER, answer="The bound is ten."))
    assert store.get_flow(flow.id).state is FlowState.FAILED
    old = store.reviews.for_job(store.get_job(job.id))
    ControlService(store).retry(flow)
    queued = store.list_jobs(flow.id)[-1]
    fresh = store.reviews.for_job(queued)
    assert fresh.id != old.id
    assert fresh.context is None
    assert [(item.github_id, item.version) for item in fresh.feedback] == [(101, 2), (102, 1)]
    store.worker_connected("worker-a", PROTOCOL_VERSION, "epoch-a")
    replacement = store.assign_job("worker-a", "token", "epoch-a")
    await apply_triage(store, flow, replacement, subject,
                      TriageVerdict(verdict=Verdict.ANSWER, answer="Answer from the new snapshot."))
    assert len(github.comments) == 1
    assert github.comments[0].startswith("Answer from the new snapshot.")
    assert not store.reviews.pending(flow.id)
    preserved = store.reviews.get_batch(old.id)
    assert preserved.context == task.context
    assert preserved.decision == old.decision
    assert store.get_job(job.id).state is JobState.FAILED


@pytest.mark.parametrize("reassigned", [False, True])
async def test_expired_assignment_cannot_freeze_its_fetched_context(store, review_flow, reassigned):
    flow, clock = review_flow
    job = assign_triage(store, flow, clock)

    class ExpiringGitHub(RecordingGitHub):
        async def get_issue(self, *args):
            clock.now += timedelta(seconds=61)
            assert store.sweep_expired_assignments() == (1, 0)
            if reassigned:
                store.worker_connected("worker-a", PROTOCOL_VERSION, "epoch-b")
                assert store.assign_job("worker-a", "new-token", "epoch-b").id == job.id
            return await super().get_issue(*args)

    prepared = await coordinator(store, ExpiringGitHub()).prepare(job)
    assert isinstance(prepared, JobState)
    assert store.reviews.for_job(store.get_job(job.id)).context is None
    assert store.get_job(job.id).state is (JobState.RUNNING if reassigned else JobState.QUEUED)


@pytest.mark.parametrize("phase", ["context", "capability"])
async def test_gateway_renews_lease_while_preparing_and_fetching_capability(
    store, review_flow, monkeypatch, phase,
):
    flow, clock = review_flow
    job = assign_triage(store, flow, clock)
    release = asyncio.Event()

    class SlowGitHub(RecordingGitHub):
        async def get_issue(self, *args):
            if phase == "context":
                await release.wait()
            return await super().get_issue(*args)

    subject = coordinator(store, SlowGitHub())
    if phase == "capability":
        async def secret(*args):
            await release.wait()
        monkeypatch.setattr(subject, "assignment_secret", secret)
    gateway = WorkerGateway(store, subject, execution_control=StopStore(store), worker_tokens={"worker-a": "x"})
    original_wait = asyncio.wait
    ticks = 0

    async def advance_time(tasks, *, timeout):
        nonlocal ticks
        if ticks < 4:
            ticks += 1
            clock.now += timedelta(seconds=20)
            assert store.sweep_expired_assignments() == (0, 0)
            if ticks == 4:
                release.set()
            await asyncio.sleep(0)
            return set(), set(tasks)
        return await original_wait(tasks, timeout=timeout)

    monkeypatch.setattr("app.control.gateway.asyncio.wait", advance_time)
    task, _ = await gateway._prepare_assignment(job, "worker-a", "token", "epoch-a")
    assert task.context
    assert ticks == 4
    assert store.get_job(job.id).lease_expires_at > clock.now


def test_preparation_cannot_renew_an_expired_lease_before_the_sweeper_runs(store, review_flow):
    flow, clock = review_flow
    job = assign_triage(store, flow, clock)
    clock.now += timedelta(seconds=61)
    assert not store.heartbeat_assignment(job.id, "worker-a", "token", "epoch-a")
    assert store.get_job(job.id).lease_expires_at == job.lease_expires_at
    assert store.sweep_expired_assignments() == (1, 0)
