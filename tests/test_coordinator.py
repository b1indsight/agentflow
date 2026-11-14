from __future__ import annotations

import asyncio
import json
import re
import subprocess
from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from app.contracts.protocol import (
    ExecutionResult,
    Failed,
    InferenceResult,
    InitialTask,
    InitialTaskResult,
    Succeeded,
    result_header,
    terminal_digest,
)
from app.controler.config import ServerSettings
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.effects import ResultEffects
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.service import ControlService
from app.controler.workflow.tools import FlowToolService
from app.domain.inference_models import InferenceCapability, InferenceFailure, InferenceOutput
from app.domain.models import DeliveryState, FlowState, Job, JobKind, JobState
from app.github import GitHubApp
from app.persistence.store import Store
from app.plugins import get_plugin_manager
from tests.github import RecordingGitHub
from tests.workspace_fixtures import register_worker

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


async def prepare_initial(subject, coding):
    """Complete the real title/branch inference before a scripted coding assignment."""
    preparing = asyncio.create_task(subject.prepare(coding))
    try:
        async with asyncio.timeout(3):
            while not preparing.done():
                flow = subject.flows.get_flow(coding.flow_id)
                naming = subject.jobs.get_job(flow.title_job_id) if flow.title_job_id is not None else None
                if naming is not None and naming.state in {JobState.QUEUED, JobState.RUNNING}:
                    break
                await asyncio.sleep(.01)
            if preparing.done():
                return preparing.result()
            worker_id = coding.assigned_worker_id
            worker = next(w for w in subject.workers.list_workers() if w.worker_id == worker_id)
            epoch = worker.connection_epoch
            subject.workers.worker_ready(worker_id, epoch, InferenceCapability(adapter_version="codex/v1"))
            naming = subject.workers.assign_job(worker_id, "naming", epoch, slot="infer")
            assert naming is not None and naming.id == flow.title_job_id
            call = naming.inference.current_task
            response = InferenceResult(
                **result_header(call.request_id), method="worker.infer", job_id=naming.id,
                assignment_token="naming", output=InferenceOutput(
                    request_id=call.request_id, request_digest=call.digest, adapter_version="codex/v1",
                    text=json.dumps({"title": "Task title", "branch_slug": "feat/implement-task"}),
                ),
            )
            assert subject.workers.record_assignment_result(naming.id, worker_id, "naming", epoch, response) == "pending"
            await subject.results.apply(naming.id)
            return await preparing
    finally:
        preparing.cancel()
        await asyncio.gather(preparing, return_exceptions=True)


@pytest.mark.parametrize("task_content,branch_slug", [
    ("feat: remove login", "feat/remove-login"),
    ("feat(auth): remove login", "feat/remove-login"),
    ("feat!: remove login", "feat/remove-login"),
    ("feat(auth)!: remove login", "feat/remove-login"),
])
async def test_initial_waits_for_title_and_branch_from_one_inference(store, task_content, branch_slug):
    # An async title used to arrive after coding had already started on an ID branch.
    flow = ControlService(database=store.database).create_flow(
        "owner/repo", None, 42, task_content=task_content,
    )
    register_worker(store, "worker-a", repo=flow.repo, epoch="epoch")
    github = RecordingGitHub()
    github.refs = {}
    subject = coordinator(store, github)
    coding = store.workers.assign_job("worker-a", "token", "epoch")
    preparing = asyncio.create_task(subject.prepare(coding))
    try:
        async with asyncio.timeout(2):
            while store.flows.get_flow(flow.id).title_job_id is None:
                await asyncio.sleep(.01)
        assert not preparing.done()
        assert store.deliveries.get_delivery(flow.id) is None
        store.workers.worker_ready("worker-a", "epoch", InferenceCapability(adapter_version="codex/v1"))
        naming = store.workers.assign_job("worker-a", "naming", "epoch", slot="infer")
        assert naming.id == store.flows.get_flow(flow.id).title_job_id
        call = naming.inference.current_task
        # The naming inference, not the later coding prompt, must choose a typed branch slug.
        naming_prompt = call.messages[0].content
        assert "Conventional Commits" in naming_prompt
        assert "<type>[optional scope][!]: <description>" in naming_prompt
        assert "<type>/<description>" in naming_prompt
        assert "feat(auth)!: remove login -> feat/remove-login" in naming_prompt
        assert "fix/pagination-cursor" in naming_prompt
        assert "flow/ and heads/ prefixes are reserved" in naming_prompt
        assert call.messages[1].content == task_content
        assert re.search(call.output_schema["properties"]["branch_slug"]["pattern"], branch_slug)
        result = InferenceResult(
            **result_header(call.request_id), method="worker.infer", job_id=naming.id,
            assignment_token="naming", output=InferenceOutput(
                request_id=call.request_id, request_digest=call.digest, adapter_version="codex/v1",
                text=json.dumps({"title": "移除登录功能", "branch_slug": branch_slug}),
            ),
        )
        assert store.workers.record_assignment_result(naming.id, "worker-a", "naming", "epoch", result) == "pending"
        await subject.results.apply(naming.id)
        task = await asyncio.wait_for(preparing, 2)
        assert isinstance(task, InitialTask) and task.branch == branch_slug
        await asyncio.to_thread(subprocess.run, ["git", "check-ref-format", "--branch", task.branch],
                                check=True, capture_output=True)
        assert store.flows.get_flow(flow.id).title == "移除登录功能"
        assert (await subject.prepare(coding)).branch == task.branch
        assert [job.kind for job in store.jobs.list_jobs(flow.id)] == [JobKind.INITIAL, JobKind.INFERENCE]
    finally:
        preparing.cancel()
        await asyncio.gather(preparing, return_exceptions=True)


@pytest.mark.parametrize("prepared_initial", [None, "\n直接任务 α\r\n完整需求 β\n"], indirect=True)
async def test_initial_job_crosses_worker_boundary_and_opens_pr(store, prepared_initial):
    flow, subject, github, request, result = prepared_initial
    job = store.jobs.get_job(request.arguments.job_id)
    task = await subject.prepare(job)
    assert isinstance(task, InitialTask)
    assert task.flow_id == flow.id
    assert task.task_content == (flow.task_content or "Real issue\n\nReal body")
    title_job = store.jobs.get_job(store.flows.get_flow(flow.id).title_job_id)
    assert title_job.inference.task.messages[-1].content == task.task_content
    if flow.issue_number is None:
        assert not any(call["operation"] == "fetch_issue" for call in github.resource_calls)
    assert task.base_branch == "trunk" and task.branch == request.arguments.branch
    # A tool receipt reserves delivery intent; only an accepted terminal may open a PR.
    assert github.opened == []
    assert await apply_result(subject, job, result) is JobState.DONE
    assert await apply_result(subject, job, result) is JobState.DONE
    assert store.flows.get_flow(flow.id).state is FlowState.REVIEW
    assert store.flows.get_current_attempt(store.flows.get_flow(flow.id)).codex_session_id == result.session_id
    delivery = store.deliveries.get_delivery(flow.id)
    assert delivery.state is DeliveryState.OPEN and delivery.pr_number == 17
    if flow.issue_number is None:
        assert delivery.body == "Body"
    assert github.opened == [task.branch]
    assert github.review_requests == [["carol"]]
    assert github.labels == [["flow:in-review"]]
    assert [j.kind for j in store.jobs.list_jobs(flow.id) if j.kind is not JobKind.INFERENCE] == [JobKind.INITIAL]
    assert store.workers.get_flow_execution(flow.id).released_at is None


async def test_issue_edits_change_prepared_text_without_resubmitting_title(store, prepared_initial, monkeypatch):
    # Edits are observed on preparation, not a new session trigger or persisted direct input.
    from app.github import Issue, Repository

    flow, subject, _, request, _ = prepared_initial
    coding = store.jobs.get_job(request.arguments.job_id)
    title_job = store.jobs.get_job(flow.title_job_id)
    for body in ("\n更新正文 β\r\n", ""):
        async def fetch_issue(repo, number, body=body):
            return Issue(repo, number, title="改名 α", body=body, state="open")

        monkeypatch.setattr(Repository, "fetch_issue", fetch_issue)
        task = await subject.prepare(coding)
        assert task.task_content == ("改名 α\n\n\n更新正文 β\r\n" if body else "改名 α")
        assert store.flows.get_flow(flow.id).task_content is None
        assert store.flows.get_flow(flow.id).title_job_id == title_job.id
        assert store.jobs.get_job(title_job.id).inference.task == title_job.inference.task
    assert [job.id for job in store.jobs.list_jobs(flow.id)] == [coding.id, title_job.id]


@pytest.fixture
async def pending_naming(store):
    flow = ControlService(database=store.database).create_flow("owner/repo", 8, 42)
    register_worker(store, "worker-a", repo=flow.repo, epoch="epoch")
    github = RecordingGitHub()
    github.refs = {}
    subject = coordinator(store, github)
    coding = store.workers.assign_job("worker-a", "token", "epoch")
    preparing = asyncio.create_task(subject.prepare(coding))
    try:
        async with asyncio.timeout(2):
            while (flow := store.flows.get_flow(flow.id)).title_job_id is None:
                await asyncio.sleep(.01)
        yield flow, subject, coding, preparing, store.jobs.get_job(flow.title_job_id)
    finally:
        preparing.cancel()
        await asyncio.gather(preparing, return_exceptions=True)


@pytest.mark.parametrize("reply,cancelled,expected", [
    ({"title": "修复分页游标 😀", "branch_slug": "fix/cursor"}, False, "修复分页游标 😀"),
    ({"title": "Title", "branch_slug": "custom/" + "x" * 41}, False, "Title"),
    ({"title": "Title", "branch_slug": "heads2/remove-login"}, False, "Title"),
    ({"title": "Title", "branch_slug": "feat/heads-remove-login"}, False, "Title"),
    ({"title": "   ", "branch_slug": "fix/cursor"}, False, None),
    ({"title": 123, "branch_slug": "fix/cursor"}, False, None),
    ({"title": "第一行\n第二行", "branch_slug": "fix/cursor"}, False, None),
    ({"title": "x" * 257, "branch_slug": "fix/cursor"}, False, None),
    ({"title": "Title", "branch_slug": 123}, False, None),
    ({"title": "Title", "branch_slug": "ab"}, False, None),
    ({"title": "Title", "branch_slug": "custom/" + "x" * 42}, False, None),
    ({"title": "Title", "branch_slug": "../unsafe"}, False, None),
    ({"title": "Title", "branch_slug": "123"}, False, None),
    ({"title": "Title", "branch_slug": "feat"}, False, None),
    ({"title": "Title", "branch_slug": "feat/"}, False, None),
    ({"title": "Title", "branch_slug": "/remove-login"}, False, None),
    ({"title": "Title", "branch_slug": "flow/remove-login"}, False, None),
    ({"title": "Title", "branch_slug": "heads/remove-login"}, False, None),
    ({"title": "Title", "branch_slug": "flow/feat/remove-login"}, False, None),
    ({"title": "Title", "branch_slug": "feat/auth/remove-login"}, False, None),
    ({"title": "Title", "branch_slug": "feat-remove-login"}, False, None),
    ({"title": "Title", "branch_slug": "feat(auth)/remove-login"}, False, None),
    ({"title": "Title", "branch_slug": "feat!/remove-login"}, False, None),
    ({"title": "Title", "branch_slug": "feat(auth)!-remove-login"}, False, None),
    ({"title": "Title", "branch_slug": "feat(auth)!: remove login"}, False, None),
    ({"title": "Title", "branch_slug": "feat/remove_login"}, False, None),
    ({"title": "Title", "branch_slug": "feat/Remove-login"}, False, None),
    ({"title": "Title", "branch_slug": "feat/remove--login"}, False, None),
    ({"title": "Title", "branch_slug": "feat/remove-login\n"}, False, None),
    ({"title": "Title", "branch_slug": "feat/remove-login-round"}, False, None),
    ({"title": "Title", "branch_slug": "feat/remove-login-round-2"}, False, None),
    ({"title": "Title"}, False, None),
    ({"title": "Title", "branch_slug": "fix/cursor", "extra": True}, False, None),
    ("[]", False, None),
    ('{"title":"One","title":"Two","branch_slug":"fix/cursor"}', False, None),
    (InferenceFailure(code="configuration"), False, None),
    ({"title": "不应使用的标题", "branch_slug": "fix/cursor"}, True, None),
    ({"title": "不应使用的标题", "branch_slug": "fix/cursor"}, "stop", None),
])
async def test_title_is_inferred_once_and_replayed_atomically(store, pending_naming, monkeypatch, reply, cancelled, expected):
    # Recover the branch from the same inference after title application, before delivery exists.
    flow, subject, _coding, preparing, title_job = pending_naming
    assert title_job is not None and title_job.inference.source_attempt_id == flow.current_attempt_id
    assert title_job.inference.task.messages[-1].content == "Real issue\n\nReal body"
    assert store.flows.get_flow(flow.id).title_job_id == title_job.id
    assert len([j for j in store.jobs.list_jobs(flow.id) if j.kind is JobKind.INFERENCE]) == 1
    store.workers.worker_ready("worker-a", "epoch", InferenceCapability(adapter_version="codex/v1"))
    assigned = store.workers.assign_job("worker-a", "title-token", "epoch", slot="infer")
    assert assigned is not None and assigned.id == title_job.id
    call = assigned.inference.current_task
    if (isinstance(reply, dict) and set(reply) == {"title", "branch_slug"}
            and reply["title"] == "Title" and isinstance(reply["branch_slug"], str)):
        # The schema given to the model and replay-time validation must accept the same slugs.
        schema = call.output_schema["properties"]["branch_slug"]
        slug = reply["branch_slug"]
        schema_accepts = (schema["minLength"] <= len(slug) <= schema["maxLength"]
                          and re.search(schema["pattern"], slug) is not None)
        assert schema_accepts is (expected is not None)
    response = InferenceResult(
        **result_header(call.request_id), method="worker.infer", job_id=assigned.id,
        assignment_token=assigned.assignment_token,
        output=InferenceOutput(request_id=call.request_id, request_digest=call.digest,
            text=json.dumps(reply) if isinstance(reply, dict) else reply,
            adapter_version="codex/v1") if not isinstance(reply, InferenceFailure) else None,
        failure=reply if isinstance(reply, InferenceFailure) else None,
    )
    assert store.workers.record_assignment_result(assigned.id, "worker-a", "title-token", "epoch", response) == "pending"
    if cancelled == "stop":
        ControlService(database=store.database).cancel(flow)
    elif cancelled:
        flow.current_attempt_id = store.flows.create_attempt(flow.id).id
        store.flows.update_flow(flow)
    recovered = coordinator(store, subject.github)
    if expected is not None:
        # Fail the title write after settlement: both facts must roll back together.
        from psycopg import Connection
        execute = Connection.execute

        def interrupt(connection, query, *args, **kwargs):
            if isinstance(query, str) and query.startswith("UPDATE flows SET title="):
                raise RuntimeError("title write interrupted")
            return execute(connection, query, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Connection, "execute", interrupt)
            with pytest.raises(RuntimeError, match="title write interrupted"):
                await recovered.results.apply(assigned.id)
        assert store.jobs.get_job(assigned.id).result_applied_at is None
        assert store.deliveries.get_delivery(flow.id) is None
        assert store.flows.get_flow(flow.id).title is None
    await recovered.results.apply(assigned.id)
    await coordinator(store, subject.github).results.apply(assigned.id)
    assert store.flows.get_flow(flow.id).title == expected
    if cancelled != "stop":
        store.flows.update_flow(flow)
    assert store.flows.get_flow(flow.id).title == expected
    if expected is not None:
        preparing.cancel()
        await asyncio.gather(preparing, return_exceptions=True)
        assert store.deliveries.get_delivery(flow.id) is None
        task = await asyncio.wait_for(coordinator(store, subject.github).prepare(_coding), 2)
        assert isinstance(task, InitialTask) and task.branch == reply["branch_slug"]
        assert store.deliveries.get_delivery(flow.id).branch == task.branch
        assert len([j for j in store.jobs.list_jobs(flow.id) if j.kind is JobKind.INFERENCE]) == 1
    else:
        task = await asyncio.wait_for(preparing, 2)
        assert task is (JobState.DONE if cancelled else JobState.FAILED)
        assert store.deliveries.get_delivery(flow.id) is None
    assert store.jobs.get_job(assigned.id).result_applied_at is not None


async def test_title_enqueue_failure_blocks_coding_without_an_id_fallback(store, monkeypatch):
    # A descriptive branch is required before any coding assignment can be sent.
    from app.persistence.jobs import JobStore

    def unavailable(*args, **kwargs):
        raise ValueError("title inference unavailable")

    monkeypatch.setattr(JobStore, "enqueue_inference", unavailable)
    flow = ControlService(database=store.database).create_flow("owner/repo", None, 42, task_content="\n原文 α\r\n")
    register_worker(store, "worker-a", repo=flow.repo, epoch="epoch")
    job = store.workers.assign_job("worker-a", "token", "epoch")
    task = await coordinator(store, RecordingGitHub()).prepare(job)
    assert task is JobState.FAILED
    assert store.deliveries.get_delivery(flow.id) is None
    assert store.flows.get_flow(flow.id).title_job_id is None


async def test_naming_deadline_without_an_inference_worker_blocks_initial(store, pending_naming, monkeypatch):
    # A coding-only worker cannot leave preparation waiting forever for inference.
    flow, subject, coding, preparing, naming = pending_naming
    deadline = naming.inference.deadline_at + timedelta(seconds=1)
    monkeypatch.setattr(store.database, "_clock", lambda: deadline)
    assert await asyncio.wait_for(preparing, 2) is JobState.FAILED
    assert store.flows.get_flow(flow.id).state is FlowState.BLOCKED
    assert store.deliveries.get_delivery(flow.id) is None
    assert store.jobs.get_job(coding.id).started_at is None
    assert "execution_timeout" in store.jobs.get_job(coding.id).last_error

    # Explicit retry retains the attempt but starts one new logical naming call.
    from tests.workspace_fixtures import report_settled

    report_settled(store, coding.id, epoch="epoch")
    assert ControlService(database=store.database).retry(store.flows.get_flow(flow.id))
    retry = store.workers.assign_job("worker-a", "retry", "epoch")
    task = await prepare_initial(subject, retry)
    assert isinstance(task, InitialTask) and task.branch == "feat/implement-task"
    named = store.flows.get_flow(flow.id)
    assert named.current_attempt_id == flow.current_attempt_id and named.title_job_id != naming.id


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
