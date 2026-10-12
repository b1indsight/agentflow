import os
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.persistence.store import Store


@pytest.fixture
def recording_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.github import Issue, PullRequest, Repository, ReviewThread
    from tests.github import (
        RecordingIssue,
        RecordingPullRequest,
        RecordingRepository,
        RecordingReviewThread,
    )

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
