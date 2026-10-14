import os
from collections.abc import Iterator
from functools import partial
from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.github import Issue, PullRequest, Repository, ReviewThread
from app.persistence.store import Store
from tests.github import (
    RecordingIssue,
    RecordingPullRequest,
    RecordingRepository,
    RecordingReviewThread,
)


@pytest.fixture(autouse=True)
def memory_repository_construction() -> Iterator[None]:
    """Replace constructor calls only for databases wrapping an in-memory simulation.

    PostgreSQL scenarios still construct real repositories. This keeps test doubles at
    the repository creation boundary without adding injection parameters to services.
    """
    from app.controler.workers import conversation, gateway
    from app.controler.workflow import coordinator, effects, results, service, tools
    from app.persistence.deliveries import DeliveryStore
    from app.persistence.flows import FlowStore
    from app.persistence.jobs import JobStore
    from app.persistence.results import ResultStore
    from app.persistence.reviews import ReviewStore
    from app.persistence.stops import StopStore
    from app.persistence.webhooks import WebhookStore
    from app.persistence.workers import WorkerStore
    from tests.persistence_fakes import RepositoryViews

    def construct(repository: type, database):
        if isinstance(database, Mock) and isinstance(database._mock_wraps, RepositoryViews):
            return database._mock_wraps.repository(repository)
        return repository(database)

    repositories = (DeliveryStore, FlowStore, JobStore, ResultStore,
                    ReviewStore, StopStore, WebhookStore, WorkerStore)
    # Keep these constructor patches independent of each test's monkeypatch fixture;
    # SQL observers must be restored before the PostgreSQL fixture drops its schema.
    with pytest.MonkeyPatch.context() as patch:
        for module in (coordinator, effects, results, service, tools, conversation, gateway):
            for repository in repositories:
                if getattr(module, repository.__name__, None) is repository:
                    patch.setattr(module, repository.__name__, partial(construct, repository))
        yield


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
