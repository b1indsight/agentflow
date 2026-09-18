"""Polling adapters return whole pages and real edit timestamps without SDK sleep/retry."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.github_client import GitHubApp
from app.github_models import GitHubAdapterError, GitHubErrorKind
from tests.test_github_client import AsyncEndpoint, response, settings


def adapter(tmp_path, monkeypatch, client):
    app = GitHubApp(settings(tmp_path))

    def installation(installation_id, *, auto_retry):
        assert installation_id == 42 and auto_retry is False
        return client

    monkeypatch.setattr(app, "_installation_client", installation)
    return app


@pytest.mark.parametrize("source", ["issue_comments", "pr_comments"])
async def test_rest_preserves_full_body_and_explicit_page(tmp_path, monkeypatch, source):
    body = "long discussion " * 500
    rows = [SimpleNamespace(id=i + 1, body=body, updated_at=datetime.now(UTC),
                            user=SimpleNamespace(login="human", type="User")) for i in range(50)]
    endpoint = AsyncEndpoint(response(rows))
    app = adapter(tmp_path, monkeypatch, SimpleNamespace(rest=SimpleNamespace(
        issues=SimpleNamespace(async_list_comments=endpoint),
    )))
    page = await app.list_feedback_page(42, "owner/repo", 17, source, "2")
    assert page.next_cursor == "3" and len(page.items) == 50
    assert page.items[0].body == body
    assert endpoint.calls == [(("owner", "repo", 17), {"page": 2, "per_page": 50})]


def reviews(**changes):
    return {"repository": {"pullRequest": {"reviews": {
        "nodes": [{"fullDatabaseId": "8589934593", "body": "edited review", "state": "CHANGES_REQUESTED",
                   "updatedAt": "2026-09-09T12:00:00Z", "author": {"login": "alice", "__typename": "User"}}],
        "pageInfo": {"hasNextPage": True, "endCursor": "following"},
    }}}} | changes


async def test_graphql_review_uses_edit_time_and_64_bit_identity(tmp_path, monkeypatch):
    endpoint = AsyncEndpoint(reviews())
    app = adapter(tmp_path, monkeypatch, SimpleNamespace(async_graphql=endpoint))
    page = await app.list_feedback_page(42, "owner/repo", 17, "reviews", "previous")
    assert page.next_cursor == "following"
    assert page.items[0].github_id == 8589934593
    assert page.items[0].updated_at == datetime(2026, 9, 9, 12, tzinfo=UTC)
    assert page.items[0].state == "changes_requested"
    query, variables = endpoint.calls[0][0]
    assert "updatedAt" in query and "submittedAt" not in query
    assert variables["cursor"] == "previous"


@pytest.mark.parametrize("malformation", ["partial", "cursor", "completeness", "missing"])
async def test_incomplete_graphql_data_never_commits_a_page(tmp_path, monkeypatch, malformation):
    data = reviews()
    if malformation == "partial":
        data["errors"] = [{"message": "missing field"}]
    elif malformation == "missing":
        data["repository"] = None
    else:
        data["repository"]["pullRequest"]["reviews"]["pageInfo"] = {
            "hasNextPage": "true" if malformation == "completeness" else True,
            "endCursor": "previous",
        }
    app = adapter(tmp_path, monkeypatch, SimpleNamespace(async_graphql=AsyncEndpoint(data)))
    with pytest.raises(GitHubAdapterError) as caught:
        await app.list_feedback_page(42, "owner/repo", 17, "reviews", "previous")
    assert caught.value.kind is GitHubErrorKind.UNEXPECTED_RESPONSE
