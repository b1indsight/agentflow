"""Closing a PR pauses its original binding and retires intent, not execution evidence."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.contracts.protocol import PROTOCOL_VERSION, InitialTaskResult, Succeeded, terminal_digest
from app.contracts.result_models import ResultPending
from app.controler.events.router import EventRouter
from app.controler.reconciliation.pull_requests import Reconciler
from app.controler.workers.conversation import ConversationBroker
from app.controler.workers.gateway import ProtocolViolation, WorkerGateway
from app.controler.workflow.lifecycle import pause_delivery
from app.controler.workflow.service import ControlService
from app.domain.delivery_models import PreparedDelivery
from app.domain.models import DeliveryState, FlowState, JobKind, JobState, VcsKind
from app.domain.review_models import BatchState
from app.github import GitHubError, GitHubErrorKind, Issue, PullRequest, RemoteRef, Repository
from app.persistence.jobs import JobStore
from tests.github import RecordingGitHub, record_call
from tests.test_coordinator import coordinator
from tests.test_review_batches import batch_for, feedback, ingest, settle
from tests.test_review_batches import (
    review_flow as review_flow,  # noqa: PLC0414 - fixture re-export
)
from tests.test_revision_delivery import verified_result
from tests.test_revision_recovery import accept
from tests.test_revision_recovery import (
    pending_revision as pending_revision,  # noqa: PLC0414 - fixture re-export
)
from tests.test_revision_recovery import (
    revision_resources as revision_resources,  # noqa: PLC0414 - fixture re-export
)
from tests.test_workflow_transactions import accepted_coding


@pytest.fixture
def lifecycle_flow(store, review_flow):
    flow, clock = review_flow
    delivery = store.deliveries.get_delivery(flow.id)
    delivery.head_sha, delivery.base_branch = "b" * 40, "main"
    store.deliveries.update_delivery(delivery)
    return flow, clock


class GitHub(RecordingGitHub):
    def __init__(self, flow, state="closed", **updates):
        super().__init__()
        self.remote = PullRequest(title="PR", body="",
            repo=flow.repo, number=17, state=state, merged=False, head_branch=f"flow/{flow.id}",
            head_sha="b" * 40, base_branch="main", base_sha="a" * 40, head_repository=flow.repo,
        ).model_copy(update=updates)
        self.before_return = None
        self.calls = 0

@pytest.fixture
def lifecycle_resources(recording_resources, monkeypatch):
    async def refetch(pr):
        state = record_call(pr.repository, "fetch_pull_request", number=pr.number)
        state.calls += 1
        if state.before_return:
            state.before_return()
        return PullRequest(pr.repository, **state.remote.model_dump(exclude_unset=True))

    async def fetch_ref(repo, branch, *, retry_transient=False):
        state = record_call(repo, "fetch_ref", branch=branch)
        return RemoteRef(branch=branch, sha=state.remote.head_sha)

    monkeypatch.setattr(PullRequest, "refetch", refetch)
    monkeypatch.setattr(Repository, "fetch_ref", fetch_ref)


async def check(store, flow, github):
    store.reconcile.hint(flow.id)
    await Reconciler(
        github,
        ControlService(
            database=store.database,
        ),
        settle_seconds=0,
        database=store.database,
    )._sweep()


@pytest.mark.parametrize("started", [False, True])
async def test_closed_binding_survives_expiry_and_original_started_job_can_resume(store, lifecycle_flow, started, lifecycle_resources):
    flow, clock = lifecycle_flow
    store.workers.worker_connected("worker", PROTOCOL_VERSION, "old-epoch")
    store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    job = store.workers.assign_job("worker", "token", "old-epoch")
    if started:
        assert store.workers.start_assignment(job.id, flow.current_attempt_id, "worker", "token", "old-epoch")
    else:
        assert store.workers.requeue_assignment(job.id, "worker", "token", "old-epoch")
    await check(store, flow, GitHub(flow, "closed"))
    store.workers.worker_disconnected("worker", "offline while PR paused", "old-epoch")
    clock.now += timedelta(seconds=601)
    store.workers.sweep_expired_assignments()
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.workers.get_flow_execution(flow.id).released_at is None
    store.workers.worker_connected("worker", PROTOCOL_VERSION, "new-epoch")
    if started:
        assert store.workers.resume_assignment(job.id, "worker", "token", "new-epoch")
        assert store.jobs.get_job(job.id).state is JobState.RUNNING
    else:
        await check(store, flow, GitHub(flow, "open"))
        assert store.deliveries.get_delivery(flow.id).state is DeliveryState.OPEN


async def test_closed_missing_journal_preserves_late_result_and_reopen(store, pending_revision, monkeypatch):
    flow, job, task, github, subject = pending_revision
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        pause_delivery(tx.deliveries.get_delivery(flow.id), deliveries=tx.deliveries, jobs=tx.jobs)
    store.workers.worker_disconnected("worker-a", "disconnected during closed PR", "epoch")
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "restart")
    gateway = WorkerGateway(
        subject, database=store.database,
        worker_tokens={},
        tools=subject.tools,
        results=subject.results,
        conversations=ConversationBroker(),
    )
    # Missing journal is not proof of execution failure: its original result may still arrive.
    with pytest.raises(ProtocolViolation, match="journal is missing"):
        await gateway._reject_missing_journal("worker-a", "restart", job.id)
    assert store.jobs.get_job(job.id).result_received_at is None
    assert store.jobs.get_job(job.id).state is JobState.RUNNING
    assert store.workers.get_flow_execution(flow.id).released_at is None

    assert store.workers.resume_assignment(job.id, "worker-a", "token", "restart")
    message = Succeeded(job_id=job.id, assignment_token="token", connection_epoch="restart",
                        result=verified_result(task))
    assert store.workers.record_assignment_result(job.id, "worker-a", "token", "restart",
        message.model_dump(mode="json"), terminal_digest(message)) == "pending"
    assert await subject.results.apply(job.id) is JobState.DONE
    remote = GitHub(flow, "open", head_branch=task.branch, head_sha=task.expected_head_sha)
    async def reopened(pr):
        record_call(pr.repository, "fetch_pull_request", number=pr.number)
        return PullRequest(pr.repository, **remote.remote.model_dump(exclude_unset=True))

    monkeypatch.setattr(PullRequest, "refetch", reopened)
    await check(store, flow, github)
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.OPEN
    assert store.workers.get_flow_execution(flow.id).released_at is None


@pytest.mark.parametrize("recovery", ["explicit", "lease", "restart"])
async def test_reopen_retires_unstarted_jobs_requeued_after_pause(store, lifecycle_flow, recovery, lifecycle_resources):
    flow, clock = lifecycle_flow
    ingest(store, flow, feedback(clock, signal="request_changes"))
    clock.now += timedelta(seconds=76)
    old = settle(store)[0]
    # This regression concerns a remote coding preparation surviving a PR pause.
    store.workers.worker_connected("worker", PROTOCOL_VERSION, "epoch")
    assert store.workers.assign_job("worker", "token", "epoch").id == old.id
    await check(store, flow, GitHub(flow, "closed"))
    if recovery == "explicit":
        store.workers.requeue_assignment(old.id, "worker", "token", "epoch")
    elif recovery == "lease":
        clock.now += timedelta(seconds=61)
        store.workers.sweep_expired_assignments()
    else:
        store.workers.recover_abandoned_assignments()
        clock.now += timedelta(seconds=61)
        store.workers.sweep_expired_assignments()
    assert store.jobs.get_job(old.id).state is JobState.QUEUED
    await check(store, flow, GitHub(flow, "open"))
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.OPEN
    assert store.jobs.get_job(old.id).state is JobState.FAILED
    fresh = settle(store)[0]
    store.workers.worker_connected("worker", PROTOCOL_VERSION, "new")
    assert store.reviews.pending_triage()[0].id == fresh.id


async def test_lifecycle_hint_during_query_schedules_a_fresh_observation(store, lifecycle_flow, lifecycle_resources):
    flow, _ = lifecycle_flow
    github = GitHub(flow, "closed")
    github.before_return = lambda: store.reconcile.hint(flow.id)
    await check(store, flow, github)
    assert store.reconcile.get(flow.id).next_at <= store.now()
    github.before_return = None
    github.remote = github.remote.model_copy(update={"state": "open"})
    await Reconciler(
        github,
        ControlService(
            database=store.database,
        ),
        settle_seconds=0,
        database=store.database,
    )._sweep()
    assert github.calls == 2 and store.deliveries.get_delivery(flow.id).state is DeliveryState.OPEN


async def test_close_retires_queued_work_and_keeps_running_executor_slot(store, lifecycle_flow, lifecycle_resources):
    flow, _ = lifecycle_flow
    store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    store.workers.worker_connected("worker", PROTOCOL_VERSION, "epoch")
    running = store.workers.assign_job("worker", "token", "epoch")
    queued = store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    github = GitHub(flow)
    await check(store, flow, github)
    version = store.deliveries.get_delivery(flow.id).version
    await check(store, flow, github)
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.CLOSED
    assert store.deliveries.get_delivery(flow.id).version == version
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert store.jobs.get_job(running.id).state is JobState.RUNNING
    assert store.jobs.get_job(queued.id).state is JobState.FAILED
    assert await coordinator(store, github).prepare(queued) is JobState.DONE
    assert all(job.kind is not JobKind.ARCHIVE for job in store.jobs.list_jobs(flow.id))
    with pytest.raises(ValueError, match="closed|CLOSED"):
        ControlService(
            database=store.database,
        ).retry(flow)


async def test_closed_pr_http_observation_replaces_awaiting_review_in_console(store, lifecycle_flow, tmp_path, monkeypatch):
    import httpx
    from githubkit import GitHub as SDK

    from app.github import GitHubApp
    from app.persistence.console import ConsoleStore
    from tests.test_github_client import install_client, settings

    # Exercise real REST decoding and PostgreSQL projection together, not a resource refetch fake.
    flow, _ = lifecycle_flow
    console = ConsoleStore(store.database)
    assert console.detail(flow.id).action == "awaiting_review"
    payload = {
        "number": 17, "state": "closed", "title": "PR", "body": None,
        "head": {"ref": f"flow/{flow.id}", "sha": "b" * 40, "repo": {"full_name": flow.repo}},
        "base": {"ref": "main", "sha": "a" * 40, "repo": {"full_name": flow.repo}},
        "merged": False, "merged_at": None, "merge_commit_sha": "c" * 40,
    }
    sdk = SDK("test-token", auto_retry=None, async_transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload),
    ))
    install_client(monkeypatch, lambda _, **kwargs: sdk)
    await check(store, flow, GitHubApp(settings(tmp_path)))
    assert store.reconcile.get(flow.id).failures == 0
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.CLOSED
    detail = console.detail(flow.id)
    assert detail.action == "delivery_closed" and detail.phase == "done"
    assert "PR 已关闭" in detail.reason
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    assert not store.jobs.list_jobs(flow.id)


async def test_reopen_refreezes_feedback_through_judge_and_keeps_binding(store, lifecycle_flow, lifecycle_resources):
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
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.OPEN
    new = settle(store)[0]
    assert new.id != old.id and new.job_id is None
    batch = batch_for(store, new)
    assert batch.context is None and batch.decision is None
    assert batch.feedback[0].body == "Edited while closed"


async def test_reopen_waits_for_old_executor_and_pending_result(store, lifecycle_flow, lifecycle_resources):
    flow, _ = lifecycle_flow
    store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    store.workers.worker_connected("worker", PROTOCOL_VERSION, "epoch")
    store.workers.assign_job("worker", "token", "epoch")
    await check(store, flow, GitHub(flow))
    await check(store, flow, GitHub(flow, "open"))
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.CLOSED
    assert "executor" in store.reconcile.get(flow.id).last_error


@pytest.mark.parametrize("changed", [{"head_sha": "c" * 40}, {"base_branch": "other"}])
async def test_human_drift_pauses_without_adopting_a_new_baseline(store, lifecycle_flow, changed, lifecycle_resources):
    flow, _ = lifecycle_flow
    await check(store, flow, GitHub(flow, "open", **changed))
    delivery = store.deliveries.get_delivery(flow.id)
    assert delivery.state is DeliveryState.CLOSED and delivery.head_sha == "b" * 40
    assert delivery.base_branch == "main"
    await check(store, flow, GitHub(flow, "open", **changed))
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.CLOSED
    assert "head/base" in store.reconcile.get(flow.id).last_error


@pytest.mark.parametrize("state", [FlowState.FAILED, FlowState.CANCELLED])
async def test_reopen_never_reactivates_terminal_flow(store, lifecycle_flow, state, lifecycle_resources):
    flow, _ = lifecycle_flow
    await check(store, flow, GitHub(flow))
    flow.state = state
    store.flows.update_flow(flow)
    await check(store, flow, GitHub(flow, "open"))
    assert store.flows.get_flow(flow.id).state is state
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.CLOSED


@pytest.mark.parametrize("action", ["closed", "reopened", "synchronize"])
async def test_lifecycle_webhooks_are_durable_query_hints(store, lifecycle_flow, action):
    flow, _ = lifecycle_flow
    router = EventRouter(
        ControlService(
            database=store.database,
        ),
        SimpleNamespace(),
        database=store.database,
    )
    payload = {"action": action, "repository": {"full_name": flow.repo},
               "pull_request": {"number": 17, "merged": False}}
    router.dispatch("pull_request", "hint", payload)
    router.dispatch("pull_request", "hint", payload)
    assert store.webhooks.event_seen("hint")
    assert store.reconcile.get(flow.id) is not None
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.OPEN


async def test_close_during_capability_preparation_never_dispatches(store, pending_revision, monkeypatch):
    flow, job, _, _github, subject = pending_revision
    original = Repository.create_push_capability

    async def close_before_return(repo):
        secret = await original(repo)
        with store.transaction() as tx:
            tx.lock_workflow(flow.id)
            pause_delivery(
                tx.deliveries.get_delivery(flow.id), deliveries=tx.deliveries, jobs=tx.jobs
            )
        return secret

    monkeypatch.setattr(Repository, "create_push_capability", close_before_return)
    gateway = WorkerGateway(
        subject, database=store.database,
        worker_tokens={},
        tools=subject.tools,
        results=subject.results,
        conversations=ConversationBroker(),
    )
    prepared = await gateway._prepare_assignment(job, "worker-a", "token", "epoch")
    assert prepared is JobState.DONE
    assert store.jobs.get_job(job.id).started_at is None
    assert store.workers.get_flow_execution(flow.id).released_at is None


async def test_inflight_revision_is_acknowledged_then_verified_before_reopen(store, pending_revision, monkeypatch):
    flow, job, task, github, subject = pending_revision
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    github.sha = "revision"
    remote = GitHub(flow, head_sha="revision")
    async def observe_remote(pr):
        record_call(pr.repository, "fetch_pull_request", number=pr.number)
        return PullRequest(pr.repository, **remote.remote.model_dump(exclude_unset=True))

    with monkeypatch.context() as patch:
        patch.setattr(PullRequest, "refetch", observe_remote)
        await check(store, flow, remote)
    result = verified_result(task, commit_sha="revision", remote_sha="revision")
    assert accept(store, job, result) == "pending"
    assert await subject.results.apply(job.id) is JobState.DONE
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.CLOSED
    assert store.deliveries.get_delivery(flow.id).head_sha == "baseline"
    assert github.comments == [] and github.review_requests == []
    assert len(store.reviews.pending(flow.id)) == 1
    remote.remote = remote.remote.model_copy(update={"state": "open"})
    monkeypatch.setattr(PullRequest, "refetch", observe_remote)
    await check(store, flow, github)
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.OPEN
    assert store.deliveries.get_delivery(flow.id).head_sha == "revision"
    assert store.workers.get_flow_execution(flow.id).released_at is None
    queued = settle(store)[0]
    assert queued.job_id is None


async def test_close_after_comment_dispatch_recovers_only_the_existing_marker(store, monkeypatch, recording_resources):
    job, result = accepted_coding(store)
    flow = store.flows.get_flow(job.flow_id)
    github = GitHub(flow, state="open", head_sha=result.commit_sha, base_branch="trunk")
    github.pull_request = github.remote
    pending = store.reviews.pending(job.flow_id)
    assert pending
    original = Issue.comment

    async def close_and_lose_response(issue, body):
        await original(issue, body)
        with store.transaction() as tx:
            tx.lock_workflow(job.flow_id)
            pause_delivery(
                tx.deliveries.get_delivery(job.flow_id), deliveries=tx.deliveries, jobs=tx.jobs
            )
        raise GitHubError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(Issue, "comment", close_and_lose_response)
    subject = coordinator(store, github)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    future = store.now() + timedelta(seconds=300)
    monkeypatch.setattr(store._db, "_clock", lambda: future)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert len(github.comments) == 1
    assert "修订交付" in github.comments[0]
    assert store.reviews.pending(job.flow_id) == pending
    assert [j.id for j in store.jobs.list_jobs(job.flow_id) if j.kind is JobKind.REVISION] == [job.id]


async def test_initial_close_before_final_commit_keeps_verified_identity_and_can_reopen(store, recording_resources):
    flow = ControlService(
        database=store.database,
    ).on_trigger("owner/repo", 8, 42)
    store.workers.worker_connected("worker", PROTOCOL_VERSION, "epoch")
    job = store.workers.assign_job("worker", "token", "epoch")
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.state, delivery.head_sha, delivery.base_branch = DeliveryState.OPENING, "abc123", "trunk"
    delivery.title, delivery.body = "Title", "Body\n\nCloses #8"
    store.deliveries.update_delivery(delivery)
    prepared = PreparedDelivery(status="prepared", head_sha="abc123", delivery_version=delivery.version)
    store.deliveries.reserve_tool_call(call_id="initial", flow_id=flow.id, attempt_id=attempt.id,
                           job_id=job.id, name="flow.open_pr", arguments={"title": "Title", "body": "Body"})
    store.deliveries.complete_tool_call("initial", prepared)
    assert store.workers.start_assignment(job.id, attempt.id, "worker", "token", "epoch")
    result = InitialTaskResult(session_id="session-1", last_message="done", exit_code=0,
        vcs_backend=VcsKind.GIT, branch=delivery.branch, commit_sha="abc123", remote_sha="abc123",
        pushed=True, open_pr_call_id="initial", delivery_receipt=prepared)
    terminal = Succeeded(job_id=job.id, assignment_token="token", connection_epoch="epoch", result=result)
    assert store.workers.record_assignment_result(job.id, "worker", "token", "epoch",
        terminal.model_dump(mode="json"), terminal_digest(terminal)) == "pending"
    github = RecordingGitHub()
    github.pull_request = PullRequest(number=17, state="closed", head_branch=delivery.branch,
                                              head_sha="abc123", base_branch="trunk")
    subject = coordinator(store, github)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.CLOSED
    assert store.deliveries.get_delivery(flow.id).pr_number == 17
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert github.opened == github.labels == github.review_requests == []
    github.pull_request = github.pull_request.model_copy(update={"state": "open"})
    await check(store, flow, github)
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.OPEN
    assert store.flows.get_current_attempt(flow).codex_session_id == "session-1"


def test_close_and_queued_retirement_roll_back_together(store, lifecycle_flow, monkeypatch):
    flow, _ = lifecycle_flow
    queued = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)

    def fail(*args, **kwargs):
        raise RuntimeError("before commit")

    monkeypatch.setattr(JobStore, "cancel_jobs", fail)
    with pytest.raises(RuntimeError), store.transaction() as tx:
        tx.lock_workflow(flow.id)
        pause_delivery(tx.deliveries.get_delivery(flow.id), deliveries=tx.deliveries, jobs=tx.jobs)
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.OPEN
    assert store.jobs.get_job(queued.id).state is JobState.QUEUED


def test_query_hints_cannot_bypass_a_rate_limit_deadline(store, lifecycle_flow):
    flow, _ = lifecycle_flow
    store.reconcile.hint(flow.id)
    store.reconcile.failed(flow.id, 600, "rate_limited")
    deadline = store.reconcile.get(flow.id).next_at
    store.reconcile.hint(flow.id)
    assert store.reconcile.get(flow.id).next_at == deadline
