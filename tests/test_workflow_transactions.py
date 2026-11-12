"""PostgreSQL regressions for workflow commit boundaries; use the existing isolated fixture."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from threading import Barrier

import pytest

from app.contracts.protocol import RevisionTask, Succeeded
from app.controler.workflow.service import ControlService
from app.domain.models import (
    ArchiveReason,
    DeliveryState,
    FlowState,
    Job,
    JobKind,
    JobState,
    ReviewItemKind,
    TaskContent,
)
from app.domain.review_models import FeedbackUpdate
from app.persistence.jobs import JobStore
from app.persistence.store import Store
from tests.github import RecordingGitHub
from tests.review_memory import review_context
from tests.test_coordinator import coordinator
from tests.test_revision_delivery import verified_result
from tests.workspace_fixtures import register_worker, report_settled


def test_functional_store_cannot_escape_its_transaction(store):
    # A service retaining a transaction's child repository must not borrow a fresh connection later.
    with store.database.transaction() as tx:
        flows = store.flows.using(tx)
        flow = flows.create_flow("owner/repo", 707, 42)
    with pytest.raises(RuntimeError, match="outside its context"):
        flows.get_flow(flow.id)


def test_owned_repository_keeps_its_connection_scope_on_rollback(store):
    # Binding a transaction must not enroll a previously retained ordinary repository in it.
    flows = store.flows
    with pytest.raises(RuntimeError, match="abort transaction"), store.database.transaction() as tx:
        rolled_back = flows.using(tx).create_flow("owner/repo", 708, 42)
        committed = flows.create_flow("owner/repo", 709, 42)
        raise RuntimeError("abort transaction")
    assert flows.get_flow(rolled_back.id) is None
    assert flows.get_flow(committed.id) is not None


def test_trigger_rolls_back_flow_and_job_on_failure(store: Store, monkeypatch) -> None:
    # Reservation without its first job would permanently suppress delivery retries.
    def fail_enqueue(*args: object, **kwargs: object) -> Job:
        raise RuntimeError("enqueue interrupted")

    monkeypatch.setattr(JobStore, "enqueue_job", fail_enqueue)
    with pytest.raises(RuntimeError, match="enqueue interrupted"):
        ControlService(
            database=store.database,
        ).create_flow("owner/repo", 700, 42)
    assert store.flows.get_active_flow("owner/repo", 700) is None


def test_direct_tasks_keep_independent_inputs_through_retry_and_resume(store: Store):
    # A shared NULL Issue must neither deduplicate unrelated tasks nor lose their input on recovery.
    service = ControlService(database=store.database)
    content = TaskContent(title="直接任务 α", body="\n保留原始正文\n第二行 β\n")
    first = service.create_flow("owner/repo", None, 42, task_content=content)
    second = service.create_flow(
        "owner/repo", None, 42, task_content=TaskContent(title="另一任务", body="不同内容")
    )
    assert first is not None and second is not None and first.id != second.id
    assert first.issue_number is None and first.task_content == content
    first.state = FlowState.BLOCKED
    store.flows.update_flow(first)
    assert service.retry(first)
    fresh = store.flows.get_flow(first.id)
    assert fresh is not None
    service.cancel(fresh)
    archived = store.flows.get_flow(first.id)
    assert archived is not None and archived.state is FlowState.ARCHIVED
    resumed = service.resume(archived, expected_round_no=1)
    assert resumed.current_round_no == 2 and resumed.task_content == content
    first, second = store.flows.get_flow(first.id), store.flows.get_flow(second.id)
    assert first is not None and first.task_content == content
    assert second is not None and second.task_content is not None
    assert second.task_content.body == "不同内容"
    with store.connection() as connection:
        owners = connection.execute("SELECT count(*) AS n FROM issue_flow_owners").fetchone()
        assert owners is not None and owners["n"] == 0
    assert store.jobs.list_jobs(first.id)[-1].kind is JobKind.INITIAL


def test_direct_task_creation_rolls_back_input_and_initial_job(store: Store, monkeypatch):
    def fail_enqueue(*args, **kwargs):
        raise RuntimeError("enqueue interrupted")

    monkeypatch.setattr(JobStore, "enqueue_job", fail_enqueue)
    with pytest.raises(RuntimeError, match="enqueue interrupted"):
        ControlService(database=store.database).create_flow(
            "owner/repo", None, 42, task_content=TaskContent(title="原子任务", body="不能留下孤立输入")
        )
    assert store.flows.list_flows() == []


def test_admin_cli_accepts_stdin_content_without_an_issue(store, monkeypatch):
    from typer.testing import CliRunner

    from app import cli
    from app.github import Repository

    monkeypatch.setenv("FLOWTOOL_DATABASE_URL", store.database.database_url)
    monkeypatch.setenv("FLOWTOOL_WORKER_TOKENS", '{"worker":"test-token"}')
    monkeypatch.setattr(cli.github, "GitHubApp", lambda settings: RecordingGitHub())

    async def refetch(repo):
        return repo.model_copy(update={"default_branch": "main", "archived": False})

    monkeypatch.setattr(Repository, "refetch", refetch)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["flow", "create", "owner/repo", "--installation-id", "42",
                                    "--title", "命令行任务", "--body-file", "-"], input="正文 α\n完整保留 β\n")
    assert result.exit_code == 0, result.output
    flow, = store.flows.list_flows()
    assert flow.issue_number is None
    assert flow.task_content == TaskContent(title="命令行任务", body="正文 α\n完整保留 β\n")
    assert runner.invoke(cli.app, ["flows"]).exit_code == 0
    shown = runner.invoke(cli.app, ["flow", "show", str(flow.id)])
    assert shown.exit_code == 0 and "完整保留 β" in shown.output and "#None" not in shown.output


def test_assignment_skips_a_flow_locked_for_cancellation(store: Store):
    # The first assignment has no binding yet, so lifecycle serialization must include the Flow row.
    register_worker(store, "worker-a", repo="owner/repo", epoch="epoch-a")
    flow = ControlService(
        database=store.database,
    ).create_flow("owner/repo", 705, 42)
    assert flow is not None
    with store.transaction() as tx:
        assert tx.lock_workflow(flow.id) is not None
        assert store.workers.assign_job("worker-a", "token-a", "epoch-a") is None
        ControlService(
            database=tx.database,
        ).cancel(flow)
    assert store.workers.get_flow_execution(flow.id) is None


@pytest.mark.parametrize("command", ["retry", "resume"])
def test_retry_rolls_back_with_real_database(store: Store, monkeypatch, command) -> None:
    flow = store.flows.create_flow("owner/repo", 702, 42)
    flow.state = FlowState.BLOCKED
    store.flows.update_flow(flow)
    service = ControlService(database=store.database)
    if command == "resume":
        store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
        delivery = store.deliveries.get_or_create_delivery(flow.id, flow.initial_branch)
        delivery.state, delivery.pr_number = DeliveryState.OPEN, 17
        store.deliveries.update_delivery(delivery)
        store.reconcile.feedback_seed(10)
        store.reconcile.hint(flow.id)
        service.cancel(flow)

    def snapshot():
        with store.connection() as connection:
            return {
                table: connection.execute(
                    f"SELECT * FROM {table} ORDER BY to_jsonb({table})::text"
                ).fetchall()
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

    def fail_enqueue(self: JobStore, flow_id: int, kind: JobKind, **kwargs: object) -> Job:
        assert kind is JobKind.INITIAL
        raise RuntimeError("enqueue interrupted")

    # A failed enqueue must roll back both retry and every repository reset by resume.
    monkeypatch.setattr(JobStore, "enqueue_job", fail_enqueue)
    with pytest.raises(RuntimeError, match="enqueue interrupted"):
        if command == "resume":
            service.resume(flow, expected_round_no=1)
        else:
            service.retry(flow)
    assert snapshot() == before


def test_trigger_and_operator_resume_share_issue_reservation(store: Store, postgresql_url: str):
    # Explicit resume uses its own Store and can race a new webhook-triggered Flow.
    flow = ControlService(
        database=store.database,
    ).create_flow("owner/repo", 706, 42)
    assert flow is not None
    ControlService(
        database=store.database,
    ).cancel(flow)
    barrier = Barrier(2)

    with closing(Store(postgresql_url)) as operator, ThreadPoolExecutor(max_workers=2) as pool:
        def trigger():
            barrier.wait(timeout=5)
            return ControlService(
                database=store.database,
            ).create_flow(flow.repo, flow.issue_number, 42)

        def resume():
            barrier.wait(timeout=5)
            try:
                ControlService(
                    database=operator.database,
                ).resume(flow, expected_round_no=flow.current_round_no)
            except ValueError as error:
                assert "already active" in str(error)
                return False
            return True

        triggered = pool.submit(trigger)
        resumed = pool.submit(resume)
        created, resume_won = triggered.result(timeout=5), resumed.result(timeout=5)

    active = store.flows.get_active_flow(flow.repo, flow.issue_number)
    assert active is not None and active.id == (created.id if created else flow.id)
    assert resume_won is (created is None)
    assert len([job for job in store.jobs.list_jobs(active.id) if job.state is JobState.QUEUED]) == 1


def bound_review_flow(store: Store):
    """Bind a review-ready fixture without inventing evidence of remote execution."""
    register_worker(store, "worker-a", repo="owner/repo", epoch="epoch-a")
    flow = store.flows.create_flow("owner/repo", 703, 42)
    attempt = store.flows.create_attempt(flow.id)
    attempt.codex_session_id = "session"
    store.flows.update_attempt(attempt)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.state, delivery.base_branch, delivery.pr_number = DeliveryState.OPEN, "trunk", 17
    delivery.head_sha = "abc123"
    store.deliveries.update_delivery(delivery)
    initial = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("worker-a", "initial-token", "epoch-a") is not None
    # The fixture supplies the delivery directly; no INITIAL process was dispatched.
    # Starting this job would require a real terminal receipt before any later archive.
    assert store.workers.finish_assignment(
        initial.id, "worker-a", "initial-token", JobState.DONE, "epoch-a"
    )
    report_settled(store, initial.id, epoch="epoch-a")
    flow.state = FlowState.REVIEW
    store.flows.update_flow(flow)
    return flow


def accepted_coding(store: Store, *, receive: bool = True, started: bool = True):
    """A remote coding assignment for worker journal/stop recovery tests."""
    flow = bound_review_flow(store)
    store.reviews.ingest(flow.id, "coding-review", "pull_request_review", {}, FeedbackUpdate(
        kind=ReviewItemKind.REVIEW_BODY, github_id=101, author="alice", body="Fix the bound",
        updated_at=store.now(), signal="request_changes", submitted=True,
    ), settle_seconds=0, submit_grace_seconds=0)
    batch, = store.reviews.claim_due()
    job = store.workers.assign_job("worker-a", "triage-token", "epoch-a")
    assert job is not None and job.assignment_token is not None and job.payload is not None
    binding = store.workers.get_flow_execution(flow.id)
    assert flow.current_attempt_id is not None and binding is not None
    task = RevisionTask(
        batch_id=batch.id, attempt_id=flow.current_attempt_id, feedback=batch.feedback,
        flow_id=flow.id, repo=flow.repo, branch=f"flow/{flow.id}", repo_identity=binding.repo_identity,
        pr_number=17, base_branch="trunk", expected_head_sha="abc123", session_id="session",
        session_generation=0, instruction=job.payload["instruction"], task_content=TaskContent(title="Issue"),
        previous_summary="", open_points=batch.open_points,
    )
    with store.transaction() as tx:
        assert tx.lock_workflow(flow.id) is not None
        tx.reviews.freeze_revision(job, task)
        if started:
            assert tx.workers.start_assignment(job.id, task.attempt_id, "worker-a", "triage-token", "epoch-a")
            assert tx.reviews.started(job)
    result = verified_result(task)
    if receive:
        assert started
        message = Succeeded(job_id=job.id, assignment_token=job.assignment_token, result=result)
        assert (
            store.workers.record_assignment_result(
                job.id, "worker-a", job.assignment_token, "epoch-a", message
            )
            == "pending"
        )
    return store.jobs.get_job(job.id), result


def test_expired_cancelled_executor_does_not_overwrite_cancelled_flow(store: Store, monkeypatch):
    job, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(job.flow_id)
    assert flow is not None
    ControlService(
        database=store.database,
    ).cancel(flow)
    running = store.jobs.get_job(job.id)
    assert running is not None and running.state is JobState.RUNNING
    store.workers.worker_disconnected("worker-a", "offline", "epoch-a")
    later = store.now() + timedelta(minutes=10)
    monkeypatch.setattr(store._db, "_clock", lambda: later)
    assert store.workers.sweep_expired_assignments() == (0, 0)
    execution = store.workers.get_flow_execution(flow.id)
    assert execution is not None and execution.released_at is None
    cancelled = store.flows.get_flow(job.flow_id)
    assert cancelled is not None and cancelled.state is FlowState.BLOCKED
    assert cancelled.archive_reason is ArchiveReason.ABANDONED


async def test_accepted_result_survives_expired_worker_recovery(
    store: Store, monkeypatch, recording_resources,
) -> None:
    # Once control accepted the terminal result, an offline executor cannot invalidate its delivery.
    job, _ = accepted_coding(store)
    store.workers.worker_disconnected("worker-a", "connection lost", "epoch-a")
    later = store.now() + timedelta(minutes=10)
    monkeypatch.setattr(store._db, "_clock", lambda: later)
    assert store.workers.sweep_expired_assignments() == (0, 0)
    flow = store.flows.get_flow(job.flow_id)
    assert flow is not None and flow.state is FlowState.RUNNING
    github = RecordingGitHub()
    pull_request = review_context().pull_request
    assert pull_request is not None
    github.pull_request = pull_request.model_copy(update={
        "head_branch": f"flow/{flow.id}", "head_sha": "abc123", "base_branch": "trunk",
    })
    await coordinator(store, github).results.apply(job.id)
    finished = store.jobs.get_job(job.id)
    assert finished is not None and finished.state is JobState.DONE
    execution = store.workers.get_flow_execution(job.flow_id)
    assert execution is not None and execution.released_at is None
