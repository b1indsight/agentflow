"""Closing a PR pauses its original binding and retires intent, not execution evidence."""

from datetime import timedelta

import pytest

from app.contracts.protocol import PROTOCOL_VERSION, InitialResult, Succeeded, terminal_digest
from app.contracts.result_models import ResultPending
from app.control.gateway import ProtocolViolation, WorkerGateway
from app.control.lifecycle import pause_delivery
from app.control.reconcile import Reconciler
from app.control.router import EventRouter
from app.control.service import ControlService
from app.domain.delivery_models import PreparedDelivery
from app.domain.github_models import (
    GitHubAdapterError,
    GitHubErrorKind,
    PullRequestSnapshot,
    PullRequestStatus,
    RemoteRef,
)
from app.domain.models import DeliveryState, FlowState, JobKind, JobState, VcsKind
from app.domain.review_models import BatchState
from app.persistence.stop_store import StopStore
from tests.test_coordinator import RecordingGitHub, coordinator
from tests.test_review_batches import batch_for, feedback, ingest, settle
from tests.test_review_batches import (
    review_flow as review_flow,  # noqa: PLC0414 - fixture re-export
)
from tests.test_revision_delivery import verified_result
from tests.test_revision_recovery import accept
from tests.test_revision_recovery import (
    pending_revision as pending_revision,  # noqa: PLC0414 - fixture re-export
)
from tests.test_workflow_transactions import accepted_coding


@pytest.fixture
def lifecycle_flow(store, review_flow):
    flow, clock = review_flow
    delivery = store.get_delivery(flow.id)
    delivery.head_sha, delivery.base_branch = "b" * 40, "main"
    store.update_delivery(delivery)
    return flow, clock


class GitHub:
    def __init__(self, flow, state="closed", **updates):
        self.remote = PullRequestStatus(title="PR", body="",
            repo=flow.repo, number=17, state=state, merged=False, head_branch=f"flow/{flow.id}",
            head_sha="b" * 40, base_branch="main", base_sha="a" * 40, head_repository=flow.repo,
        ).model_copy(update=updates)
        self.before_return = None
        self.calls = 0

    async def get_pull_request(self, *args):
        self.calls += 1
        if self.before_return:
            self.before_return()
        return self.remote

    async def get_ref(self, installation, repo, branch, *, retry_transient=False):
        return RemoteRef(branch=branch, sha=self.remote.head_sha)


async def check(store, flow, github):
    store.reconcile.hint(flow.id)
    await Reconciler(store, github, ControlService(store), settle_seconds=0)._sweep()


@pytest.mark.parametrize("started", [False, True])
async def test_closed_binding_survives_expiry_and_original_started_job_can_resume(store, lifecycle_flow, started):
    flow, clock = lifecycle_flow
    store.worker_connected("worker", PROTOCOL_VERSION, "old-epoch")
    store.enqueue_job(flow.id, JobKind.INITIAL)
    job = store.assign_job("worker", "token", "old-epoch")
    if started:
        assert store.start_assignment(job.id, flow.current_attempt_id, "worker", "token", "old-epoch")
    else:
        assert store.requeue_assignment(job.id, "worker", "token", "old-epoch")
    await check(store, flow, GitHub(flow, "closed"))
    store.worker_disconnected("worker", "offline while PR paused", "old-epoch")
    clock.now += timedelta(seconds=601)
    store.sweep_expired_assignments()
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.get_flow_execution(flow.id).released_at is None
    store.worker_connected("worker", PROTOCOL_VERSION, "new-epoch")
    if started:
        assert store.resume_assignment(job.id, "worker", "token", "new-epoch")
        assert store.get_job(job.id).state is JobState.RUNNING
    else:
        await check(store, flow, GitHub(flow, "open"))
        assert store.get_delivery(flow.id).state is DeliveryState.OPEN


async def test_closed_missing_journal_preserves_late_result_and_reopen(store, pending_revision):
    flow, job, task, github, subject = pending_revision
    assert store.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        pause_delivery(tx, tx.get_delivery(flow.id))
    store.worker_disconnected("worker-a", "disconnected during closed PR", "epoch")
    store.worker_connected("worker-a", PROTOCOL_VERSION, "restart")
    gateway = WorkerGateway(store, subject, execution_control=StopStore(store), worker_tokens={})
    # Missing journal is not proof of execution failure: its original result may still arrive.
    with pytest.raises(ProtocolViolation, match="journal is missing"):
        await gateway._reject_missing_journal("worker-a", "restart", job.id)
    assert store.get_job(job.id).result_received_at is None
    assert store.get_job(job.id).state is JobState.RUNNING
    assert store.get_flow_execution(flow.id).released_at is None

    assert store.resume_assignment(job.id, "worker-a", "token", "restart")
    message = Succeeded(job_id=job.id, assignment_token="token", connection_epoch="restart",
                        result=verified_result(task))
    assert store.record_assignment_result(job.id, "worker-a", "token", "restart",
        message.model_dump(mode="json"), terminal_digest(message)) == "pending"
    assert await subject.results.apply(job.id) is JobState.DONE
    remote = GitHub(flow, "open", head_branch=task.branch, head_sha=task.expected_head_sha)
    github.get_pull_request = remote.get_pull_request
    await check(store, flow, github)
    assert store.get_delivery(flow.id).state is DeliveryState.OPEN
    assert store.get_flow_execution(flow.id).released_at is None


@pytest.mark.parametrize("recovery", ["explicit", "lease", "restart"])
async def test_reopen_retires_unstarted_jobs_requeued_after_pause(store, lifecycle_flow, recovery):
    flow, clock = lifecycle_flow
    ingest(store, flow, feedback(clock, signal="request_changes"))
    clock.now += timedelta(seconds=76)
    old = settle(store)[0]
    # This regression concerns a remote coding preparation surviving a PR pause.
    store.worker_connected("worker", PROTOCOL_VERSION, "epoch")
    assert store.assign_job("worker", "token", "epoch").id == old.id
    await check(store, flow, GitHub(flow, "closed"))
    if recovery == "explicit":
        store.requeue_assignment(old.id, "worker", "token", "epoch")
    elif recovery == "lease":
        clock.now += timedelta(seconds=61)
        store.sweep_expired_assignments()
    else:
        store.recover_abandoned_assignments()
        clock.now += timedelta(seconds=61)
        store.sweep_expired_assignments()
    assert store.get_job(old.id).state is JobState.QUEUED
    await check(store, flow, GitHub(flow, "open"))
    assert store.get_delivery(flow.id).state is DeliveryState.OPEN
    assert store.get_job(old.id).state is JobState.FAILED
    fresh = settle(store)[0]
    store.worker_connected("worker", PROTOCOL_VERSION, "new")
    assert store.reviews.pending_triage()[0].id == fresh.id


async def test_lifecycle_hint_during_query_schedules_a_fresh_observation(store, lifecycle_flow):
    flow, _ = lifecycle_flow
    github = GitHub(flow, "closed")
    github.before_return = lambda: store.reconcile.hint(flow.id)
    await check(store, flow, github)
    assert store.reconcile.get(flow.id).next_at <= store.now()
    github.before_return = None
    github.remote = github.remote.model_copy(update={"state": "open"})
    await Reconciler(store, github, ControlService(store), settle_seconds=0)._sweep()
    assert github.calls == 2 and store.get_delivery(flow.id).state is DeliveryState.OPEN


async def test_close_retires_queued_work_and_keeps_running_executor_slot(store, lifecycle_flow):
    flow, _ = lifecycle_flow
    store.enqueue_job(flow.id, JobKind.INITIAL)
    store.worker_connected("worker", PROTOCOL_VERSION, "epoch")
    running = store.assign_job("worker", "token", "epoch")
    queued = store.enqueue_job(flow.id, JobKind.REVISION)
    github = GitHub(flow)
    await check(store, flow, github)
    version = store.get_delivery(flow.id).version
    await check(store, flow, github)
    assert store.get_delivery(flow.id).state is DeliveryState.CLOSED
    assert store.get_delivery(flow.id).version == version
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.get_flow_execution(flow.id).released_at is None
    assert store.get_job(running.id).state is JobState.RUNNING
    assert store.get_job(queued.id).state is JobState.FAILED
    assert await coordinator(store, github).prepare(queued) is JobState.DONE
    assert all(job.kind is not JobKind.ARCHIVE for job in store.list_jobs(flow.id))
    with pytest.raises(ValueError, match="closed|CLOSED"):
        ControlService(store).retry(flow)


async def test_reopen_refreezes_feedback_through_judge_and_keeps_binding(store, lifecycle_flow):
    flow, clock = lifecycle_flow
    ingest(store, flow, feedback(clock, signal="request_changes", submitted=True))
    clock.now += timedelta(seconds=76)
    old = settle(store)[0]
    await check(store, flow, GitHub(flow))
    assert batch_for(store, old).state is BatchState.CANCELLED
    # Closed PRs still retain genuine edits, but never dispatch work.
    clock.now += timedelta(seconds=1)
    ingest(store, flow, feedback(clock, body="Edited while closed"), "closed-edit")
    assert settle(store) == []
    await check(store, flow, GitHub(flow, "open"))
    assert store.get_delivery(flow.id).state is DeliveryState.OPEN
    new = settle(store)[0]
    assert new.id != old.id and new.job_id is None
    batch = batch_for(store, new)
    assert batch.context is None and batch.decision is None
    assert batch.feedback[0].body == "Edited while closed"


async def test_reopen_waits_for_old_executor_and_pending_result(store, lifecycle_flow):
    flow, _ = lifecycle_flow
    store.enqueue_job(flow.id, JobKind.INITIAL)
    store.worker_connected("worker", PROTOCOL_VERSION, "epoch")
    store.assign_job("worker", "token", "epoch")
    await check(store, flow, GitHub(flow))
    await check(store, flow, GitHub(flow, "open"))
    assert store.get_delivery(flow.id).state is DeliveryState.CLOSED
    assert "executor" in store.reconcile.get(flow.id).last_error


@pytest.mark.parametrize("changed", [{"head_sha": "c" * 40}, {"base_branch": "other"}])
async def test_human_drift_pauses_without_adopting_a_new_baseline(store, lifecycle_flow, changed):
    flow, _ = lifecycle_flow
    await check(store, flow, GitHub(flow, "open", **changed))
    delivery = store.get_delivery(flow.id)
    assert delivery.state is DeliveryState.CLOSED and delivery.head_sha == "b" * 40
    assert delivery.base_branch == "main"
    await check(store, flow, GitHub(flow, "open", **changed))
    assert store.get_delivery(flow.id).state is DeliveryState.CLOSED
    assert "head/base" in store.reconcile.get(flow.id).last_error


async def test_delayed_close_observation_cannot_replace_newer_reopen(store, lifecycle_flow):
    flow, _ = lifecycle_flow
    await check(store, flow, GitHub(flow))
    github = GitHub(flow)

    def reopen():
        delivery = store.get_delivery(flow.id)
        delivery.state = DeliveryState.OPEN
        delivery.version += 1
        store.update_delivery(delivery)

    github.before_return = reopen
    await check(store, flow, github)
    assert store.get_delivery(flow.id).state is DeliveryState.OPEN


@pytest.mark.parametrize("state", [FlowState.FAILED, FlowState.CANCELLED])
async def test_reopen_never_reactivates_terminal_flow(store, lifecycle_flow, state):
    flow, _ = lifecycle_flow
    await check(store, flow, GitHub(flow))
    flow.state = state
    store.update_flow(flow)
    await check(store, flow, GitHub(flow, "open"))
    assert store.get_flow(flow.id).state is state
    assert store.get_delivery(flow.id).state is DeliveryState.CLOSED


@pytest.mark.parametrize("action", ["closed", "reopened", "synchronize"])
async def test_lifecycle_webhooks_are_durable_query_hints(store, lifecycle_flow, action):
    flow, _ = lifecycle_flow
    from types import SimpleNamespace

    router = EventRouter(store, ControlService(store), SimpleNamespace())
    payload = {"action": action, "repository": {"full_name": flow.repo},
               "pull_request": {"number": 17, "merged": False}}
    router.dispatch("pull_request", "hint", payload)
    router.dispatch("pull_request", "hint", payload)
    assert store.event_seen("hint")
    assert store.reconcile.get(flow.id) is not None
    assert store.get_delivery(flow.id).state is DeliveryState.OPEN


async def test_close_during_capability_preparation_never_dispatches(store, pending_revision, monkeypatch):
    flow, job, _, github, subject = pending_revision
    original = github.create_push_capability

    async def close_before_return(*args):
        secret = await original(*args)
        with store.transaction() as tx:
            tx.lock_workflow(flow.id)
            pause_delivery(tx, tx.get_delivery(flow.id))
        return secret

    monkeypatch.setattr(github, "create_push_capability", close_before_return)
    gateway = WorkerGateway(store, subject, execution_control=StopStore(store), worker_tokens={})
    prepared, secret = await gateway._prepare_assignment(job, "worker-a", "token", "epoch")
    assert prepared is JobState.DONE and secret is None
    assert store.get_job(job.id).started_at is None
    assert store.get_flow_execution(flow.id).released_at is None


async def test_inflight_revision_is_acknowledged_then_verified_before_reopen(store, pending_revision, monkeypatch):
    flow, job, task, github, subject = pending_revision
    assert store.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    github.sha = "revision"
    remote = GitHub(flow, head_sha="revision")
    await check(store, flow, remote)
    result = verified_result(task, commit_sha="revision", remote_sha="revision")
    assert accept(store, job, result) == "pending"
    assert await subject.results.apply(job.id) is JobState.DONE
    assert store.get_delivery(flow.id).state is DeliveryState.CLOSED
    assert store.get_delivery(flow.id).head_sha == "baseline"
    assert github.comments == [] and github.review_requests == []
    assert len(store.reviews.pending(flow.id)) == 1
    remote.remote = remote.remote.model_copy(update={"state": "open"})
    monkeypatch.setattr(github, "get_pull_request", remote.get_pull_request)
    await check(store, flow, github)
    assert store.get_delivery(flow.id).state is DeliveryState.OPEN
    assert store.get_delivery(flow.id).head_sha == "revision"
    assert store.get_flow_execution(flow.id).released_at is None
    queued = settle(store)[0]
    assert queued.job_id is None


async def test_close_after_comment_dispatch_recovers_only_the_existing_marker(store, monkeypatch):
    job, _ = accepted_coding(store)
    github = RecordingGitHub()
    original = github.comment

    async def close_and_lose_response(*args):
        await original(*args)
        with store.transaction() as tx:
            tx.lock_workflow(job.flow_id)
            pause_delivery(tx, tx.get_delivery(job.flow_id))
        raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(github, "comment", close_and_lose_response)
    subject = coordinator(store, github)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    future = store.now() + timedelta(seconds=300)
    monkeypatch.setattr(store, "_clock", lambda: future)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert len(github.comments) == 1
    assert store.reviews.pending(job.flow_id) == []
    assert all(j.kind is not JobKind.REVISION for j in store.list_jobs(job.flow_id))


async def test_initial_close_before_final_commit_keeps_verified_identity_and_can_reopen(store):
    flow = ControlService(store).on_trigger("owner/repo", 8, 42)
    store.worker_connected("worker", PROTOCOL_VERSION, "epoch")
    job = store.assign_job("worker", "token", "epoch")
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    delivery = store.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.state, delivery.head_sha, delivery.base_branch = DeliveryState.OPENING, "abc123", "trunk"
    delivery.title, delivery.body = "Title", "Body\n\nCloses #8"
    store.update_delivery(delivery)
    prepared = PreparedDelivery(status="prepared", head_sha="abc123", delivery_version=delivery.version)
    store.reserve_tool_call(call_id="initial", flow_id=flow.id, attempt_id=attempt.id,
                           job_id=job.id, name="flow.open_pr", arguments={"title": "Title", "body": "Body"})
    store.complete_tool_call("initial", prepared)
    assert store.start_assignment(job.id, attempt.id, "worker", "token", "epoch")
    result = InitialResult(session_id="session-1", last_message="done", exit_code=0,
        vcs_backend=VcsKind.GIT, branch=delivery.branch, commit_sha="abc123", remote_sha="abc123",
        pushed=True, open_pr_call_id="initial", delivery_receipt=prepared)
    terminal = Succeeded(job_id=job.id, assignment_token="token", connection_epoch="epoch", result=result)
    assert store.record_assignment_result(job.id, "worker", "token", "epoch",
        terminal.model_dump(mode="json"), terminal_digest(terminal)) == "pending"
    github = RecordingGitHub()
    github.pull_request = PullRequestSnapshot(number=17, state="closed", head_branch=delivery.branch,
                                              head_sha="abc123", base_branch="trunk")
    subject = coordinator(store, github)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert store.get_delivery(flow.id).state is DeliveryState.CLOSED
    assert store.get_delivery(flow.id).pr_number == 17
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.get_flow_execution(flow.id).released_at is None
    assert github.opened == github.labels == github.review_requests == []
    github.pull_request = github.pull_request.model_copy(update={"state": "open"})
    await check(store, flow, github)
    assert store.get_delivery(flow.id).state is DeliveryState.OPEN
    assert store.get_current_attempt(flow).codex_session_id == "session-1"


def test_close_and_queued_retirement_roll_back_together(store, lifecycle_flow, monkeypatch):
    flow, _ = lifecycle_flow
    queued = store.enqueue_job(flow.id, JobKind.INITIAL)

    def fail(*args, **kwargs):
        raise RuntimeError("before commit")

    from app.persistence.store import Store

    monkeypatch.setattr(Store, "cancel_jobs", fail)
    with pytest.raises(RuntimeError), store.transaction() as tx:
        tx.lock_workflow(flow.id)
        pause_delivery(tx, tx.get_delivery(flow.id))
    assert store.get_delivery(flow.id).state is DeliveryState.OPEN
    assert store.get_job(queued.id).state is JobState.QUEUED


def test_query_hints_cannot_bypass_a_rate_limit_deadline(store, lifecycle_flow):
    flow, _ = lifecycle_flow
    store.reconcile.hint(flow.id)
    store.reconcile.failed(flow.id, 600, "rate_limited")
    deadline = store.reconcile.get(flow.id).next_at
    store.reconcile.hint(flow.id)
    assert store.reconcile.get(flow.id).next_at == deadline
