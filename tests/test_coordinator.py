from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from app.contracts.protocol import (
    ExecutionResult,
    Failed,
    InitialTask,
    InitialTaskResult,
    Succeeded,
    terminal_digest,
)
from app.controler.config import ServerSettings
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.effects import ResultEffects
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.tools import FlowToolService
from app.domain.models import DeliveryState, FlowState, Job, JobKind, JobState
from app.github import GitHubApp
from app.persistence.store import Store
from app.plugins import get_plugin_manager
from tests.github import RecordingGitHub

pytestmark = pytest.mark.usefixtures("recording_resources")


class CoordinatorFixture(JobCoordinator):
    tools: FlowToolService


def accept_result(subject: JobCoordinator, job: Job, result: ExecutionResult) -> str:
    """Accept through the same fenced inbox used by the live worker connection."""
    assert job.assigned_worker_id is not None and job.assignment_token is not None
    worker = next(w for w in subject.workers.list_workers() if w.worker_id == job.assigned_worker_id)
    assert worker.connection_epoch is not None
    terminal = Succeeded(job_id=job.id, assignment_token=job.assignment_token, result=result)
    accepted = subject.workers.record_assignment_result(
        job.id, job.assigned_worker_id, job.assignment_token, worker.connection_epoch, terminal,
    )
    assert accepted in {"pending", "applied"}
    return accepted


async def apply_result(subject: JobCoordinator, job: Job, result: ExecutionResult) -> JobState:
    accept_result(subject, job, result)
    return await subject.results.apply(job.id)


def coordinator(store: Store, github: GitHubApp) -> CoordinatorFixture:
    settings = ServerSettings(
        database_url=store.database.database_url,
        worker_tokens={"worker-1": SecretStr("worker-secret")},
        allowlist=["alice"],
        reviewers=["carol"],
        in_review_label="flow:in-review",
        done_label="flow:done",
        confidence_threshold=0.6,
        max_revision_cycles=20,
        prepare_max_failures=5,
        prepare_retry_window_seconds=900,
    )
    plugins = get_plugin_manager()
    effects = ResultEffects(github, database=store.database, plugins=plugins)
    results = ResultApplication(github, settings, plugins, effects=effects, database=store.database)
    subject = CoordinatorFixture(github, settings, plugins, database=store.database, results=results)
    subject.tools = FlowToolService(github, database=store.database)
    return subject


async def test_initial_job_crosses_worker_boundary_and_opens_pr(store, prepared_initial):
    flow, subject, github, request, result = prepared_initial
    job = store.jobs.get_job(request.arguments.job_id)
    task = await subject.prepare(job)
    assert isinstance(task, InitialTask)
    assert task.flow_id == flow.id
    assert task.issue_title == "Real issue" and task.issue_body == "Real body"
    assert task.base_branch == "trunk" and task.branch == flow.initial_branch
    # A tool receipt reserves delivery intent; only an accepted terminal may open a PR.
    assert github.opened == []
    assert await apply_result(subject, job, result) is JobState.DONE
    assert await apply_result(subject, job, result) is JobState.DONE
    assert store.flows.get_flow(flow.id).state is FlowState.REVIEW
    assert store.flows.get_current_attempt(store.flows.get_flow(flow.id)).codex_session_id == result.session_id
    delivery = store.deliveries.get_delivery(flow.id)
    assert delivery.state is DeliveryState.OPEN and delivery.pr_number == 17
    assert github.opened == [task.branch]
    assert github.review_requests == [["carol"]]
    assert github.labels == [["flow:in-review"]]
    assert [j.kind for j in store.jobs.list_jobs(flow.id)] == [JobKind.INITIAL]
    assert store.workers.get_flow_execution(flow.id).released_at is None


async def test_initial_failure_after_open_pr_request_has_no_github_pr_side_effect(store, prepared_initial):
    # An agent can call the tool and then fail; its prepared receipt must not open a PR.
    flow, subject, github, request, _ = prepared_initial
    job = store.jobs.get_job(request.arguments.job_id)
    result = InitialTaskResult(session_id="session-1", last_message="failed", exit_code=1)
    assert await apply_result(subject, job, result) is JobState.FAILED
    assert github.opened == []
    assert store.deliveries.get_delivery(flow.id).state is DeliveryState.OPENING


@pytest.mark.parametrize("stage,detail,reason", [
    ("workspace", "unregistered workspace unexpectedly exists",
     "（exit=65，阶段=workspace）：unregistered workspace unexpectedly exists"),
    ("push", "remote branch is missing", "（exit=65，阶段=push）：remote branch is missing"),
    ("execution", "Amp: no new stdout/stderr for 1200s; execution timed out",
     "（exit=65，阶段=execution）：Amp: no new stdout/stderr for 1200s; execution timed out"),
    (None, "safe diagnostic [REDACTED]", "（exit=65）：safe diagnostic [REDACTED]"),
    (None, None, "（exit=65）"),
])
@pytest.mark.parametrize("pr_number", [None, 17])
async def test_initial_failure_keeps_diagnostics_local(store, prepared_initial, stage, detail, reason, pr_number):
    flow, subject, github, request, _ = prepared_initial
    job = store.jobs.get_job(request.arguments.job_id)
    if pr_number is not None:
        delivery = store.deliveries.get_delivery(flow.id)
        delivery.pr_number, delivery.state = pr_number, DeliveryState.OPEN
        store.deliveries.update_delivery(delivery)
    result = InitialTaskResult(
        session_id=None if stage == "workspace" else "session-1",
        last_message="ignored raw message", exit_code=65, failure_stage=stage, failure_detail=detail,
    )
    assert await apply_result(subject, job, result) is JobState.FAILED
    assert github.comments == github.opened == []
    assert store.results.list_effects(job.id) == []
    assert store.flows.get_flow(flow.id).state is FlowState.BLOCKED
    assert store.jobs.get_job(job.id).last_error == f"编码失败{reason}。未开 PR。"


@pytest.mark.parametrize("version", [1, 14, 15])
async def test_recovery_applies_unassigned_control_failure_without_rewriting_inbox(store, version):
    # Preparation can fail before assignment; only a control failure may use the sentinel token.
    flow = store.flows.create_flow("owner/control", 1, 42)
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    terminal = Failed(job_id=job.id, assignment_token="control", error="preparation failed")
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.lock_job(job.id)
        with tx.connection() as connection:
            connection.execute("UPDATE jobs SET state='running' WHERE id=%s", (job.id,))
        tx.jobs.record_control_result(job.id, terminal, terminal.error)
        if version != 1:
            payload = {
                "protocol_version": version,
                "connection_epoch": "control",
                **terminal.model_dump(mode="json"),
            }
            with tx.connection() as connection:
                connection.execute("UPDATE jobs SET result_payload=%s WHERE id=%s", (Jsonb(payload), job.id))
    accepted = store.jobs.get_job(job.id)
    github = RecordingGitHub()
    subject = coordinator(store, github)
    await subject.results.recover_pending_results()
    persisted = store.jobs.get_job(job.id)
    assert persisted.state is JobState.FAILED and persisted.result_applied_at is not None
    assert persisted.result_payload == accepted.result_payload
    assert persisted.result_digest == accepted.result_digest == terminal_digest(terminal)
    assert persisted.last_error == "preparation failed"
    assert github.comments == []
    assert store.results.list_effects(job.id) == []
    assert store.flows.get_flow(flow.id).state is FlowState.BLOCKED
    assert store.jobs.lifecycle_job(flow.id) is None
