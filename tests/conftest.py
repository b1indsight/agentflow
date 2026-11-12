import os
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.controler.config import ServerSettings
from app.controler.runtime import ServerRuntime
from app.github import Issue, PullRequest, Repository, ReviewThread
from app.persistence.store import Store
from tests.github import (
    RecordingGitHub,
    RecordingIssue,
    RecordingPullRequest,
    RecordingRepository,
    RecordingReviewThread,
)


@pytest.fixture
def recording_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Repository, "refetch", RecordingRepository.refetch)
    monkeypatch.setattr(Repository, "fetch_ref", RecordingRepository.fetch_ref)
    monkeypatch.setattr(Repository, "compare_commits", RecordingRepository.compare_commits)
    monkeypatch.setattr(Repository, "create_push_capability", RecordingRepository.create_push_capability)
    monkeypatch.setattr(Repository, "find_pull_request_by_head", RecordingRepository.find_pull_request_by_head)
    monkeypatch.setattr(Repository, "open_pull_request", RecordingRepository.open_pull_request)
    monkeypatch.setattr(Issue, "refetch", RecordingIssue.refetch)
    monkeypatch.setattr(Issue, "comment", RecordingIssue.comment)
    monkeypatch.setattr(Issue, "find_comment", RecordingIssue.find_comment)
    monkeypatch.setattr(Issue, "fetch_labels", RecordingIssue.fetch_labels)
    monkeypatch.setattr(Issue, "add_label", RecordingIssue.add_label)
    monkeypatch.setattr(Issue, "remove_label", RecordingIssue.remove_label)
    monkeypatch.setattr(Issue, "fetch_comments", RecordingIssue.fetch_comments)
    monkeypatch.setattr(PullRequest, "refetch", RecordingPullRequest.refetch)
    monkeypatch.setattr(PullRequest, "comment", RecordingIssue.comment)
    monkeypatch.setattr(PullRequest, "find_comment", RecordingIssue.find_comment)
    monkeypatch.setattr(PullRequest, "fetch_labels", RecordingIssue.fetch_labels)
    monkeypatch.setattr(PullRequest, "add_label", RecordingIssue.add_label)
    monkeypatch.setattr(PullRequest, "remove_label", RecordingIssue.remove_label)
    monkeypatch.setattr(PullRequest, "fetch_reviewers", RecordingPullRequest.fetch_reviewers)
    monkeypatch.setattr(PullRequest, "request_reviewers", RecordingPullRequest.request_reviewers)
    monkeypatch.setattr(PullRequest, "fetch_comments", RecordingPullRequest.fetch_comments)
    monkeypatch.setattr(PullRequest, "fetch_reviews", RecordingPullRequest.fetch_reviews)
    monkeypatch.setattr(PullRequest, "list_review_threads", RecordingPullRequest.list_review_threads)
    monkeypatch.setattr(PullRequest, "fetch_threads", RecordingPullRequest.fetch_threads)
    monkeypatch.setattr(PullRequest, "list_files", RecordingPullRequest.list_files)
    monkeypatch.setattr(PullRequest, "list_review_comments", RecordingPullRequest.list_review_comments)
    monkeypatch.setattr(ReviewThread, "fetch_comments", RecordingReviewThread.fetch_comments)
    monkeypatch.setattr(ReviewThread, "refetch", RecordingReviewThread.refetch)
    monkeypatch.setattr(ReviewThread, "find_reply", RecordingReviewThread.find_reply)
    monkeypatch.setattr(ReviewThread, "reply", RecordingReviewThread.reply)
    monkeypatch.setattr(ReviewThread, "fetch_resolution", RecordingReviewThread.fetch_resolution)
    monkeypatch.setattr(ReviewThread, "resolve", RecordingReviewThread.resolve)


@pytest.fixture
def postgresql_url() -> Iterator[str]:
    base_url = os.getenv("FLOWTOOL_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("FLOWTOOL_TEST_DATABASE_URL is required for PostgreSQL integration tests")

    schema = f"test_{uuid4().hex}"
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    schema_url = make_conninfo(base_url, options=f"-c search_path={schema}")
    try:
        yield schema_url
    finally:
        with psycopg.connect(base_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.fixture
def store(postgresql_url: str) -> Iterator[Store]:
    instance = Store(postgresql_url)
    instance.init_db()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
async def server_runtime(store: Store, monkeypatch: pytest.MonkeyPatch):
    """Construct owned control planes on the test schema, replacing only GitHub I/O."""
    runtimes: list[ServerRuntime] = []

    def create(
        *,
        github: RecordingGitHub | None = None,
        settings: dict[str, Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> ServerRuntime:
        values = {
            "_env_file": None,
            "database_url": store.database.database_url,
            "worker_tokens": {"worker-1": "worker-secret"},
            "worker_poll_interval": 0.01,
            **(settings or {}),
        }
        remote = github if github is not None else RecordingGitHub()
        with monkeypatch.context() as patch:
            patch.setattr("app.controler.runtime.GitHubApp", lambda _: remote)
            runtime = ServerRuntime(ServerSettings(**values))
        runtimes.append(runtime)
        if clock is not None:
            monkeypatch.setattr(runtime.database, "_clock", clock)
        return runtime

    yield create
    for runtime in reversed(runtimes):
        await runtime.shutdown()


@pytest.fixture
async def prepared_initial(store: Store, recording_resources: None):
    """Reuse the INITIAL workflow up to its prepared tool receipt, before terminal acceptance."""
    from app.contracts.protocol import (
        FlowToolArguments,
        FlowToolRequest,
        InitialTask,
        InitialTaskResult,
        call_header,
    )
    from app.controler.workflow.service import ControlService
    from app.domain.models import VcsKind
    from tests.github import RecordingGitHub
    from tests.test_coordinator import coordinator
    from tests.workspace_fixtures import register_worker

    flow = ControlService(database=store.database).on_trigger("owner/repo", 8, 42)
    assert flow is not None
    register_worker(store, "worker-a", repo=flow.repo, epoch="epoch")
    job = store.workers.assign_job("worker-a", "token", "epoch")
    assert job is not None
    github = RecordingGitHub()
    subject = coordinator(store, github)
    task = await subject.prepare(job)
    assert isinstance(task, InitialTask)
    assert store.workers.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    request = FlowToolRequest(
        **call_header(), method="flow.open_pr",
        arguments=FlowToolArguments(
            job_id=job.id, flow_id=flow.id, attempt_id=task.attempt_id, assignment_token="token",
            call_id="initial-open", arguments={"title": "Title", "body": "Body"},
            vcs_backend=VcsKind.GIT, branch=task.branch, commit_sha="abc123", remote_sha="abc123",
        ),
    )
    tool = await subject.tools.execute(request, worker_id="worker-a", connection_epoch="epoch")
    assert tool.result is not None
    result = InitialTaskResult(
        session_id="initial-session", last_message="done", exit_code=0,
        vcs_backend=VcsKind.GIT, branch=task.branch, commit_sha="abc123", remote_sha="abc123",
        pushed=True, open_pr_call_id="initial-open", delivery_receipt=tool.result,
    )
    current = store.flows.get_flow(flow.id)
    assert current is not None
    return current, subject, github, request, result


@pytest.fixture
async def completed_open_initial(store: Store, prepared_initial):
    """Continue the same assignment through its persisted terminal and inventory report."""
    from app.domain.models import JobState
    from tests.test_coordinator import apply_result
    from tests.workspace_fixtures import report_settled

    flow, subject, github, request, result = prepared_initial
    job = store.jobs.get_job(request.arguments.job_id)
    assert job is not None
    assert await apply_result(subject, job, result) is JobState.DONE
    report_settled(store, job.id, epoch="epoch")
    completed = store.flows.get_flow(flow.id)
    assert completed is not None
    return completed, subject, github, request, result
