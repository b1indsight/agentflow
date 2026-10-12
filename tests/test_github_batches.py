"""Native cursor queries return nested batches without performing hidden traversal."""

from types import SimpleNamespace

import pytest

from app.github import Batch, GitHubError
from tests.test_github_feedback import adapter
from tests.test_github_thread_pages import node, outer


async def test_threads_include_comments_with_independent_continuations(tmp_path, monkeypatch):
    # A second thread must not reuse the first thread's comment continuation.
    data = outer(node(following="comments-A"), following="threads-next")
    data["repository"]["pullRequest"]["reviewThreads"]["nodes"].append(
        node("T2", number=2, following="comments-B"),
    )
    calls = []

    async def graphql(query, variables):
        calls.append((query, variables))
        return data

    pr = adapter(tmp_path, monkeypatch, SimpleNamespace(async_graphql=graphql))
    batch = await pr.fetch_threads(cursor="threads-before", limit=2)
    assert isinstance(batch, Batch)
    assert batch.next_cursor == "threads-next"
    assert [thread.comments.next_cursor for thread in batch.items] == ["comments-A", "comments-B"]
    assert [thread.comments.items[0].github_id for thread in batch.items] == [2**33 + 1, 2**33 + 2]
    assert all(thread.pull_request is pr and not thread.complete for thread in batch.items)
    assert len(calls) == 1
    assert calls[0][1] == {"owner": "owner", "name": "repo", "pr": 17,
                           "cursor": "threads-before", "limit": 2}


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
async def test_invalid_limit_is_rejected_before_request(tmp_path, monkeypatch, limit):
    async def graphql(*args):
        pytest.fail("invalid query must not send a request")

    pr = adapter(tmp_path, monkeypatch, SimpleNamespace(async_graphql=graphql))
    with pytest.raises((ValueError, GitHubError)):
        await pr.fetch_threads(limit=limit)


async def test_comment_continuation_fetches_only_that_thread(tmp_path, monkeypatch):
    calls = []

    async def graphql(query, variables):
        calls.append((query, variables))
        if len(calls) == 1:
            return outer(node(following="comments-next"))
        return {"node": node(number=21)}

    pr = adapter(tmp_path, monkeypatch, SimpleNamespace(async_graphql=graphql))
    thread = (await pr.fetch_threads(limit=1)).items[0]
    comments = await thread.fetch_comments(cursor=thread.comments.next_cursor, limit=20)
    assert comments.items[0].github_id == 2**33 + 21 and comments.next_cursor is None
    assert calls[1][1] == {"id": "T1", "cursor": "comments-next", "limit": 20}
    assert thread.comments.items[0].github_id == 2**33 + 1
    assert thread.comments.next_cursor == "comments-next"  # Original observation remains immutable.
