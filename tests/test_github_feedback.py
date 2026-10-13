"""Polling adapters return whole pages and real edit timestamps without SDK sleep/retry."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.controler.reconciliation.feedback import FeedbackReconciler
from app.github import GitHubApp, GitHubError, GitHubErrorKind, Issue, PullRequest
from tests.test_github_client import AsyncEndpoint, install_client, settings


def adapter(tmp_path, monkeypatch, client):
    app = GitHubApp(settings(tmp_path))

    def installation(installation_id, *, auto_retry):
        assert installation_id == 42 and auto_retry is False
        return client

    install_client(monkeypatch, installation)
    return PullRequest(app.installation(42).repository("owner/repo"), 17)


@pytest.mark.parametrize("source", ["issue_comments", "pr_comments"])
async def test_graphql_comments_preserve_full_body_and_native_cursor(tmp_path, monkeypatch, source):
    body = "long discussion " * 500
    rows = [{"fullDatabaseId": str(2**33 + i), "body": body,
             "updatedAt": datetime.now(UTC).isoformat(),
             "author": {"login": "human", "__typename": "User"}} for i in range(2)]
    endpoint = AsyncEndpoint({"repository": {"issueOrPullRequest": {"comments": {
        "nodes": rows, "pageInfo": {"hasNextPage": False, "endCursor": "last"},
    }}}})
    app = adapter(tmp_path, monkeypatch, SimpleNamespace(async_graphql=endpoint))
    if source == "issue_comments":
        app = Issue(app.repository, app.number)
    page = await app.fetch_comments(cursor="previous", limit=2)
    assert page.next_cursor is None and len(page.items) == 2
    assert page.items[0].body == body
    assert page.items[0].github_id == 2**33
    query, variables = endpoint.calls[0][0]
    assert "comments(first:$limit,after:$cursor)" in query
    assert variables == {"owner": "owner", "name": "repo", "number": 17, "cursor": "previous", "limit": 2}


def reviews(**changes):
    return {"repository": {"pullRequest": {"reviews": {
        "nodes": [{"fullDatabaseId": "8589934593", "body": "edited review", "state": "CHANGES_REQUESTED",
                   "updatedAt": "2026-09-09T12:00:00Z", "author": {"login": "alice", "__typename": "User"}}],
        "pageInfo": {"hasNextPage": True, "endCursor": "following"},
    }}}} | changes


async def test_graphql_review_uses_edit_time_and_64_bit_identity(tmp_path, monkeypatch):
    endpoint = AsyncEndpoint(reviews())
    app = adapter(tmp_path, monkeypatch, SimpleNamespace(async_graphql=endpoint))
    page = await app.fetch_reviews(cursor="previous", limit=1)
    assert page.next_cursor == "following"
    assert page.items[0].github_id == 8589934593
    assert page.items[0].updated_at == datetime(2026, 9, 9, 12, tzinfo=UTC)
    assert page.items[0].state == "changes_requested"
    query, variables = endpoint.calls[0][0]
    assert "updatedAt" in query and "submittedAt" not in query
    assert variables["cursor"] == "previous"


async def test_deleted_author_stays_in_batch_but_does_not_become_reviewer_input(tmp_path, monkeypatch):
    # Deleted authors still consume the context quota; durable scanning must keep
    # its previous rule of ignoring feedback without a known reviewer identity.
    data = reviews()
    data["repository"]["pullRequest"]["reviews"]["nodes"][0]["author"] = None
    pr = adapter(tmp_path, monkeypatch, SimpleNamespace(async_graphql=AsyncEndpoint(data)))
    page = await pr.fetch_reviews(limit=1)
    assert len(page.items) == 1 and page.items[0].author is None
    assert page.items[0].body == "edited review" and not page.items[0].is_bot
    scanner = FeedbackReconciler(
        pr.repository.installation.app,
        settle_seconds=0,
        database=Mock(),
        deliveries=Mock(),
        reconcile_store=Mock(),
        reviews=Mock(),
    )
    assert scanner._discussion_updates(page, "reviews") == ([], None)


@pytest.mark.parametrize("malformation", ["cursor", "missing"])
async def test_incomplete_graphql_data_never_commits_a_page(tmp_path, monkeypatch, malformation):
    data = reviews()
    if malformation == "missing":
        data["repository"] = None
    else:
        data["repository"]["pullRequest"]["reviews"]["pageInfo"] = {
            "hasNextPage": True,
            "endCursor": "previous",
        }
    app = adapter(tmp_path, monkeypatch, SimpleNamespace(async_graphql=AsyncEndpoint(data)))
    with pytest.raises(GitHubError) as caught:
        await app.fetch_reviews(cursor="previous", limit=1)
    assert caught.value.kind is GitHubErrorKind.UNEXPECTED_RESPONSE
