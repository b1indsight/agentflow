"""Business-state contracts through the existing service and PostgreSQL transactions."""

from datetime import timedelta

import pytest

from app.controler.workflow.service import ControlService
from app.domain.models import DeliveryState, FlowState, ReviewItemKind
from app.domain.review_models import FeedbackUpdate


@pytest.mark.parametrize("branch,round_no,expected_branch", [
    ("feat/remove-login", 1, "feat/remove-login-round-2"),
    ("feat/remove-login-round-3", 3, "feat/remove-login-round-4"),
    ("flow/77", 1, "flow/77-round-2"),
    ("flow/custom-task", 1, "flow/custom-task-round-2"),
    ("flow/custom-task-round-3", 3, "flow/custom-task-round-4"),
    ("flow/not-this-round-3", 2, "flow/not-this-round-3-round-3"),
])
def test_resume_does_not_replay_prior_round_feedback_or_cursors(store, branch, round_no, expected_branch):
    service = ControlService(database=store.database)
    flow = service.create_flow("owner/repo", 7, 42)
    for previous_round in range(1, round_no):
        service.cancel(flow)
        flow = service.resume(flow, expected_round_no=previous_round)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    delivery = store.deliveries.get_or_create_delivery(flow.id, branch)
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
    resumed = service.resume(flow, expected_round_no=round_no)
    assert store.deliveries.get_delivery(flow.id).branch == expected_branch
    resumed.current_attempt_id = store.flows.create_attempt(flow.id).id
    store.flows.update_flow(resumed)
    assert store.reviews.pending(flow.id) == []
    assert store.reviews.claim_due() == []
    assert store.reconcile.feedback_get(flow.id, "reviews") is None
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM review_versions").fetchone()["n"] == 1


async def test_resume_before_first_delivery_still_requires_worker_naming(store, recording_resources):
    # A task abandoned before preparation has no branch for resume to reset.
    from app.contracts.protocol import InitialTask
    from tests.github import RecordingGitHub
    from tests.test_coordinator import coordinator, prepare_initial
    from tests.workspace_fixtures import register_worker

    service = ControlService(database=store.database)
    flow = service.create_flow("owner/repo", 8, 42)
    service.cancel(flow)
    resumed = service.resume(flow, expected_round_no=1)
    assert resumed.current_round_no == 2 and store.deliveries.get_delivery(flow.id) is None
    register_worker(store, "worker-a", repo=flow.repo, epoch="epoch")
    coding = store.workers.assign_job("worker-a", "token", "epoch")
    github = RecordingGitHub()
    github.refs = {}
    task = await prepare_initial(coordinator(store, github), coding)
    assert isinstance(task, InitialTask) and task.branch == "feat/implement-task-round-2"


async def test_failed_initial_round_stops_workspace_before_resume(
    store,
    tmp_path,
    recording_resources,
    monkeypatch,
):
    # A failed INITIAL retains dirty files. Stop proves writer exit, while the next
    # controller generation prepares the checkout and preserves both round refs.
    from app.contracts.protocol import (
        PROTOCOL_VERSION,
        AssignmentArguments,
        InitialTask,
        InitialTaskResult,
    )
    from app.domain.models import JobState
    from app.worker.execution.executor import WorkerExecutor
    from app.worker.execution.lifecycle import WorkerExecution
    from app.worker.execution.workspaces import RepositoryWorkspaces
    from app.worker.state.execution import ExecutionState
    from tests.github import RecordingGitHub
    from tests.test_coordinator import coordinator, prepare_initial
    from tests.test_vcs import _git
    from tests.test_worker_executor import RecordingAgent, assignment_secret
    from tests.test_worker_workspaces import managed_seed

    root, remote = managed_seed(tmp_path)
    seed = root / "repos/owner/repo"
    _git("branch", "trunk", cwd=seed)
    _git("push", "origin", "trunk", cwd=seed)
    service = ControlService(database=store.database)
    flow = service.create_flow("owner/repo", 8, 42)
    pool = RepositoryWorkspaces(root, "worker-a", limit=1)
    default = pool.initialize()
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "epoch",
                                   workspace=default.workspace, repo_workspace_limit=1)
    for item in await pool.provision(flow.repo):
        assert store.workers.report_workspace("worker-a", "epoch", item)
    job = store.workers.assign_job("worker-a", "first", "epoch")
    github = RecordingGitHub()
    github.refs = {}
    subject = coordinator(store, github)
    task = await prepare_initial(subject, job)
    assert isinstance(task, InitialTask)
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "first", "epoch")
    state = ExecutionState(root, flow.id)
    executor = WorkerExecutor(root, RecordingAgent(), worker_id="worker-a", state=state, workspaces=pool)
    state.record_received(
        AssignmentArguments(
            flow_id=flow.id,
            job_id=job.id,
            assignment_token="first",
            workspace=job.workspace,
            generation=job.generation,
            task=task,
        )
    )
    state.record_started()
    binding = await pool.claim(
        flow.repo,
        flow.id,
        job.workspace,
        job.generation,
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
    state.reconcile_acknowledgement()
    for item in pool.inventory():
        assert store.workers.report_workspace("worker-a", "epoch", item)
    assert store.workers.get_flow_execution(flow.id).released_at is None
    store.workers.worker_disconnected("worker-a", connection_epoch="epoch")
    later = store.now() + timedelta(days=1)
    monkeypatch.setattr(store._db, "_clock", lambda: later)
    store.workers.sweep_expired_assignments()
    assert store.workers.get_flow_execution(flow.id).released_at is None
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "epoch",
                                   workspace=default.workspace, repo_workspace_limit=1)
    for item in pool.inventory():
        assert store.workers.report_workspace("worker-a", "epoch", item)
    service.cancel(store.flows.get_flow(flow.id))
    assert store.flows.get_flow(flow.id).state is FlowState.FINALIZING
    with pytest.raises(ValueError, match="archived"):
        service.resume(flow, expected_round_no=1)
    stop = store.stops.pending("worker-a")
    owner = WorkerExecution(worker_id="worker-a", executor=executor, state=state)
    receipt = await owner.stop(stop.fence())
    assert store.stops.confirm("worker-a", receipt, "epoch")
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert (workspace / "README.md").read_text() == "unfinished old-round changes"
    assert (workspace / "unfinished.txt").exists()
    assert pool.lookup(flow.repo, flow.id) == binding
    assert next(w for w in store.workers.list_workspaces("worker-a")
                if w.workspace == job.workspace).generation == binding.generation + 1
    owner.on_stop_accepted(stop.fence())
    state.reconcile_acknowledgement()
    for item in pool.inventory():
        assert store.workers.report_workspace("worker-a", "epoch", item)
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    service.resume(flow, expected_round_no=1)
    next_job = store.workers.assign_job("worker-a", "second", "epoch")
    next_task = await subject.prepare(next_job)
    assert isinstance(next_task, InitialTask)
    assert next_task.branch == f"{task.branch}-round-2"
    assert store.flows.get_flow(flow.id).title_job_id == store.jobs.list_jobs(flow.id)[1].id
    assert store.deliveries.get_delivery(flow.id).branch == next_task.branch
    state.record_received(
        AssignmentArguments(
            flow_id=flow.id,
            job_id=next_job.id,
            assignment_token="second",
            workspace=next_job.workspace,
            generation=next_job.generation,
            task=next_task,
        )
    )
    assert state.load().workspace is None
    state.record_started()
    next_binding = await pool.claim(
        flow.repo,
        flow.id,
        next_job.workspace,
        next_job.generation,
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
