"""Business-state contracts through the existing service and PostgreSQL transactions."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.controler.events.router import EventRouter
from app.controler.workflow.service import ControlService
from app.domain.models import ArchiveReason, DeliveryState, FlowState, JobKind, ReviewItemKind
from app.domain.review_models import FeedbackUpdate
from app.persistence.activity import ActivityStore
from app.persistence.console import ConsoleStore


def test_investigation_archive_and_explicit_new_round(store):
    service = ControlService(database=store.database)
    flow = service.on_trigger("owner/repo", 7, 42)
    assert flow.state is FlowState.QUEUED
    with pytest.raises(ValueError, match="conclusion"):
        service.finish(flow, reason=ArchiveReason.SUCCEEDED, summary="", expected_round_no=1)
    service.finish(
        flow,
        reason=ArchiveReason.SUCCEEDED,
        summary="Investigation complete: no change needed.",
        expected_round_no=1,
    )
    archived = store.flows.get_flow(flow.id)
    assert archived.state is FlowState.ARCHIVED
    assert archived.archive_reason is ArchiveReason.SUCCEEDED
    assert store.deliveries.get_delivery(flow.id) is None
    assert ConsoleStore(store.database).detail(flow.id).phase == "archived"
    resumed = service.resume(archived, expected_round_no=1)
    assert resumed.id == flow.id and resumed.current_round_no == 2
    assert resumed.state is FlowState.QUEUED and resumed.archived_at is None
    archive = ActivityStore(store.database).archived(flow.id, 1)
    assert archive is not None
    assert archive.metadata["archive_summary"] == archived.archive_summary
    assert archive.occurred_at == archived.archived_at
    with pytest.raises(ValueError):
        service.resume(archived, expected_round_no=1)


def test_blocked_still_owns_issue_and_archive_releases_it(store):
    service = ControlService(database=store.database)
    flow = service.on_trigger("owner/repo", 7, 42)
    flow.state = FlowState.BLOCKED
    store.flows.update_flow(flow)
    assert service.on_trigger("owner/repo", 7, 42) is None
    service.cancel(flow)
    assert store.flows.get_flow(flow.id).archive_reason is ArchiveReason.ABANDONED
    newer = service.on_trigger("owner/repo", 7, 42)
    assert newer is not None
    with pytest.raises(ValueError, match="already active"):
        service.resume(flow, expected_round_no=1)


def test_old_pr_merge_records_evidence_without_affecting_resumed_round(store):
    # A delayed merge webhook still names the previous PR after a new delivery starts.
    service = ControlService(database=store.database)
    flow = service.on_trigger("owner/repo", 7, 42)
    delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.state, delivery.pr_number = DeliveryState.OPEN, 17
    store.deliveries.update_delivery(delivery)
    service.cancel(flow)
    activity = ActivityStore(store.database)
    archived = activity.archived(flow.id, 1)
    assert archived is not None and archived.metadata["archive_reason"] == "abandoned"
    resumed = service.resume(flow, expected_round_no=1)
    router = EventRouter(service, SimpleNamespace(), database=store.database)
    payload = {
        "action": "closed",
        "repository": {"full_name": flow.repo},
        "installation": {"id": 99},
        "pull_request": {
            "number": 17,
            "merged": True,
            "merged_at": (archived.occurred_at - timedelta(seconds=5)).isoformat(),
            "merge_commit_sha": "a" * 40,
        },
    }
    router.dispatch("pull_request", "late-merge", payload)
    assert not store.webhooks.event_seen("late-merge")
    payload["installation"]["id"] = 42
    router.dispatch("pull_request", "late-merge", payload)
    router.dispatch("pull_request", "late-merge", payload)
    assert store.flows.get_flow(flow.id) == resumed
    assert activity.archived(flow.id, 1) == archived
    assert activity.archived_pr(flow.repo, 17) == (flow.id, 1)
    history = activity.history(flow.id, 1)
    merges = [item for item in history if item.kind == "merge"]
    assert len(merges) == 1
    assert merges[0].metadata["merge_commit_sha"] == "a" * 40
    assert len([item for item in history if item.kind == "archive_correction"]) == 1
    assert activity.archived(flow.id, 2) is None
    assert [job.kind for job in store.jobs.list_jobs(flow.id)] == [JobKind.INITIAL, JobKind.INITIAL]
    assert store.deliveries.get_delivery(flow.id).pr_number is None


def test_resume_does_not_replay_prior_round_feedback_or_cursors(store):
    service = ControlService(database=store.database)
    flow = service.on_trigger("owner/repo", 7, 42)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.state, delivery.pr_number = DeliveryState.OPEN, 17
    store.deliveries.update_delivery(delivery)
    store.reviews.ingest(
        flow.id,
        "old-feedback",
        "pull_request_review",
        {},
        FeedbackUpdate(
            kind=ReviewItemKind.REVIEW_BODY,
            github_id=12,
            author="reviewer",
            body="Old delivery only",
            signal="request_changes",
            submitted=True,
            updated_at=store.now(),
        ),
        settle_seconds=0,
        submit_grace_seconds=0,
    )
    store.reconcile.feedback_seed(10)
    assert store.reviews.pending(flow.id)
    assert store.reconcile.feedback_get(flow.id, "reviews") is not None
    service.cancel(flow)
    resumed = service.resume(flow, expected_round_no=1)
    resumed.current_attempt_id = store.flows.create_attempt(flow.id).id
    store.flows.update_flow(resumed)
    assert store.reviews.pending(flow.id) == []
    assert store.reviews.claim_due() == []
    assert store.reconcile.feedback_get(flow.id, "reviews") is None
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM review_versions").fetchone()["n"] == 1


async def test_failed_initial_round_stops_workspace_before_resume(
    store,
    tmp_path,
    recording_resources,
    monkeypatch,
):
    # A failed INITIAL has an acknowledged journal but can still own a dirty checkout.
    # Exercise the real stop/recycle path, then create and push both retained round refs.
    from app.contracts.protocol import (
        PROTOCOL_VERSION,
        AssignmentArguments,
        InitialTask,
        InitialTaskResult,
    )
    from app.domain.models import JobState
    from app.worker.execution.executor import WorkerExecutor
    from app.worker.execution.lifecycle import WorkerExecution
    from app.worker.state.execution import ExecutionState
    from tests.github import RecordingGitHub
    from tests.test_coordinator import coordinator
    from tests.test_vcs import _git
    from tests.test_worker_executor import RecordingAgent, assignment_secret
    from tests.test_worker_workspaces import managed_seed

    root, remote = managed_seed(tmp_path)
    seed = root / "repos/owner/repo"
    _git("branch", "trunk", cwd=seed)
    _git("push", "origin", "trunk", cwd=seed)
    service = ControlService(database=store.database)
    flow = service.on_trigger("owner/repo", 8, 42)
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "epoch")
    job = store.workers.assign_job("worker-a", "first", "epoch")
    subject = coordinator(store, RecordingGitHub())
    task = await subject.prepare(job)
    assert isinstance(task, InitialTask)
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "first", "epoch")
    state = ExecutionState(root)
    executor = WorkerExecutor(root, RecordingAgent(), worker_id="worker-a", state=state)
    state.record_received(
        AssignmentArguments(
            flow_id=flow.id,
            job_id=job.id,
            assignment_token="first",
            task=task,
        )
    )
    state.record_started()
    binding = await executor.workspaces.acquire(
        flow.repo,
        flow.id,
        base_branch=task.base_branch,
        remote="origin",
        push_token=assignment_secret().push.token,
    )
    state.bind_workspace(binding)
    workspace = executor.workspaces.path(binding)
    _git("checkout", "-b", task.branch, cwd=workspace)
    _git("push", "origin", task.branch, cwd=workspace)
    (workspace / "README.md").write_text("unfinished old-round changes")
    (workspace / "unfinished.txt").write_text("untracked old-round changes")
    terminal = state.complete(
        InitialTaskResult(session_id=None, last_message="failed", exit_code=1)
    )
    assert (
        store.workers.record_assignment_result(job.id, "worker-a", "first", "epoch", terminal)
        == "pending"
    )
    assert await subject.results.apply(job.id) is JobState.FAILED
    state.acknowledge(job.id, JobState.FAILED)
    assert store.workers.get_flow_execution(flow.id).released_at is None
    store.workers.worker_disconnected("worker-a", connection_epoch="epoch")
    later = store.now() + timedelta(days=1)
    monkeypatch.setattr(store._db, "_clock", lambda: later)
    store.workers.sweep_expired_assignments()
    assert store.workers.get_flow_execution(flow.id).released_at is None
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "epoch")
    service.cancel(store.flows.get_flow(flow.id))
    assert store.flows.get_flow(flow.id).state is FlowState.FINALIZING
    with pytest.raises(ValueError, match="archived"):
        service.resume(flow, expected_round_no=1)
    stop = store.stops.pending("worker-a")
    owner = WorkerExecution(worker_id="worker-a", executor=executor, state=state)
    receipt = await owner.stop(stop.fence())
    assert store.stops.confirm("worker-a", receipt, "epoch")
    owner.on_stop_accepted(stop.fence())
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    service.resume(flow, expected_round_no=1)
    next_job = store.workers.assign_job("worker-a", "second", "epoch")
    next_task = await subject.prepare(next_job)
    assert isinstance(next_task, InitialTask)
    assert store.deliveries.get_delivery(flow.id).branch == next_task.branch
    state.record_received(
        AssignmentArguments(
            flow_id=flow.id,
            job_id=next_job.id,
            assignment_token="second",
            task=next_task,
        )
    )
    assert state.load().workspace is None
    state.record_started()
    next_binding = await executor.workspaces.acquire(
        flow.repo,
        flow.id,
        base_branch=next_task.base_branch,
        remote="origin",
        push_token=assignment_secret().push.token,
    )
    state.bind_workspace(next_binding)
    assert next_binding.generation > binding.generation
    assert (workspace / "README.md").read_text() == "base\n"
    assert not (workspace / "unfinished.txt").exists()
    _git("checkout", "-b", next_task.branch, cwd=workspace)
    _git("push", "origin", next_task.branch, cwd=workspace)
    _git("show-ref", "--verify", f"refs/heads/{task.branch}", cwd=remote)
    _git("show-ref", "--verify", f"refs/heads/{next_task.branch}", cwd=remote)


def test_resume_rolls_back_all_owned_resets_if_initial_enqueue_fails(store, monkeypatch):
    from app.persistence.jobs import JobStore

    # Splitting persistence ownership must retain the all-or-nothing resume boundary.
    service = ControlService(database=store.database)
    flow = service.on_trigger("owner/repo", 7, 42)
    delivery = store.deliveries.get_or_create_delivery(flow.id, flow.initial_branch)
    delivery.state, delivery.pr_number = DeliveryState.OPEN, 17
    store.deliveries.update_delivery(delivery)
    store.reconcile.feedback_seed(10)
    store.reconcile.hint(flow.id)
    service.cancel(flow)

    def snapshot():
        with store.connection() as connection:
            return {
                table: connection.execute(f"SELECT * FROM {table}").fetchall()
                for table in (
                    "flows",
                    "flow_deliveries",
                    "console_activity",
                    "feedback_reconcile",
                    "flow_reconcile",
                    "console_sync",
                    "console_flows",
                    "issue_flow_owners",
                    "jobs",
                )
            }

    before = snapshot()

    def fail_enqueue(*args, **kwargs):
        raise RuntimeError("enqueue interrupted")

    monkeypatch.setattr(JobStore, "enqueue_job", fail_enqueue)
    with pytest.raises(RuntimeError, match="enqueue interrupted"):
        service.resume(flow, expected_round_no=1)
    assert snapshot() == before
