"""Actual adapter pagination distinguishes thread cursors from nested comment cursors."""


import pytest

from app.github import GitHubApp, GitHubError, GitHubErrorKind, PullRequest, ReviewThread
from app.github.threads import MAX_THREAD_BYTES
from tests.test_github_client import install_client, settings
from tests.test_github_feedback import adapter


def node(thread="T1", number=1, *, following=None):
    return {
        "id": thread, "isResolved": True, "isOutdated": False, "path": "app.py",
        "pullRequest": {"number": 17, "repository": {"nameWithOwner": "owner/repo"}},
        "comments": {
            "nodes": [{"fullDatabaseId": str(2**33 + number), "id": f"C{number}", "body": "Fix this",
                       "updatedAt": "2026-09-09T00:00:00Z", "author": {"login": "alice", "__typename": "User"},
                       "replyTo": None, "originalCommit": {"oid": "a" * 40}}],
            "pageInfo": {"hasNextPage": following is not None, "endCursor": following},
        },
    }


def outer(thread, *, following=None):
    return {"repository": {"pullRequest": {"reviewThreads": {
        "nodes": [thread] if thread is not None else [],
        "pageInfo": {"hasNextPage": following is not None, "endCursor": following},
    }}}}


async def test_all_thread_reads_preserve_the_same_comment_identity(tmp_path, monkeypatch):
    # Full snapshots and durable polling see the same large IDs, including reply targets.
    thread = node()
    raw = thread["comments"]["nodes"][0]
    raw["replyTo"] = {"fullDatabaseId": str(2**33)}
    raw["author"] = None
    app = GitHubApp(settings(tmp_path))
    client = Client(outer(thread), {"node": thread}, outer(thread))
    install_client(monkeypatch, lambda *args, **kwargs: client)

    pr = PullRequest(app.installation(42).repository("owner/repo"), 17)
    full = await pr.list_review_threads()
    single = await ReviewThread(pr, "T1").refetch()
    page = await pr.fetch_threads(limit=1)

    observations = (full.threads[0], single, page.items[0])
    assert all(type(item) is ReviewThread and item.pull_request is pr for item in observations)
    assert all(item.comments == single.comments for item in observations)
    comment = full.threads[0].comments.items[0]
    assert (comment.github_id, comment.reply_to_id) == (2**33 + 1, 2**33)
    assert comment.author == "deleted-user" and not comment.is_bot
    assert all("fullDatabaseId" in query and "databaseId" not in query for query, _ in client.calls)


class Client:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    async def async_graphql(self, query, variables):
        self.calls.append((query, variables))
        return next(self.responses)


async def test_real_sdk_missing_thread_is_reported_as_not_found(tmp_path, monkeypatch):
    import httpx
    from githubkit import GitHub

    client = GitHub("test-token", auto_retry=False, async_transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": {"node": None}, "errors": [
            {"type": "NOT_FOUND", "message": "not accessible", "path": ["node"]},
        ]}),
    ))
    app = adapter(tmp_path, monkeypatch, client)
    with pytest.raises(GitHubError) as caught:
        await ReviewThread(app, "T1").fetch_comments(cursor="c20")
    assert caught.value.kind is GitHubErrorKind.NOT_FOUND


@pytest.mark.parametrize("nested", [False, True])
async def test_other_graphql_errors_do_not_skip_unread_feedback(tmp_path, monkeypatch, nested):
    import httpx
    from githubkit import GitHub

    errors = [{"type": "NOT_FOUND", "message": "missing"}]
    if nested:
        errors.append({"type": "FORBIDDEN", "message": "permission"})
    client = GitHub("test-token", auto_retry=False, async_transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": {"node": None}, "errors": errors}),
    ))
    app = adapter(tmp_path, monkeypatch, client)
    with pytest.raises(GitHubError):
        if nested:
            await ReviewThread(app, "T1").fetch_comments(cursor="c20")
        else:
            await app.fetch_threads(limit=1)


async def test_thread_batches_and_comment_suffixes_have_independent_cursors(tmp_path, monkeypatch):
    # Each thread keeps its own comment continuation; the outer cursor skips neither thread.
    data = outer(node(following="comments-A"), following="threads-next")
    data["repository"]["pullRequest"]["reviewThreads"]["nodes"].append(
        node("T2", number=2, following="comments-B"),
    )
    client = Client(data, {"node": node(number=21)}, outer(node("T3", 22)))
    app = adapter(tmp_path, monkeypatch, client)
    first = await app.fetch_threads(cursor="threads-before", limit=2)
    assert len(client.calls) == 1
    assert [thread.comments.next_cursor for thread in first.items] == ["comments-A", "comments-B"]
    assert [thread.comments.items[0].github_id for thread in first.items] == [2**33 + 1, 2**33 + 2]
    assert all(thread.pull_request is app and not thread.complete for thread in first.items)
    thread = first.items[0]
    second = await thread.fetch_comments(cursor=thread.comments.next_cursor, limit=20)
    third = await app.fetch_threads(cursor=first.next_cursor, limit=1)
    assert thread.comments.items[0].github_id == 2**33 + 1
    assert thread.comments.next_cursor == "comments-A"
    assert second.items[0].github_id == 2**33 + 21 and second.next_cursor is None
    assert third.items[0].thread_id == "T3" and third.items[0].complete and third.next_cursor is None
    assert client.calls[0][1] == {"owner": "owner", "name": "repo", "pr": 17,
                                "cursor": "threads-before", "limit": 2}
    assert client.calls[1][1] == {"id": "T1", "cursor": "comments-A", "limit": 20}
    assert client.calls[2][1]["cursor"] == "threads-next"


@pytest.mark.parametrize("bad", ["repo", "pr", "duplicate", "cursor", "partial", "size", "null", "flag"])
async def test_invalid_pages_do_not_masquerade_as_complete(tmp_path, monkeypatch, bad):
    thread = node()
    data = outer(thread)
    if bad in {"repo", "pr"}:
        if bad == "repo":
            thread["pullRequest"]["repository"]["nameWithOwner"] = "foreign/repo"
        else:
            thread["pullRequest"]["number"] = 18
    elif bad == "duplicate":
        thread["comments"]["nodes"] *= 2
    elif bad == "cursor":
        thread["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": None}
    elif bad == "partial":
        data["errors"] = [{"message": "partial"}]
    elif bad == "size":
        thread["comments"]["nodes"][0]["body"] = "x" * MAX_THREAD_BYTES
    elif bad == "null":
        thread["comments"]["nodes"] = [None]
    else:
        thread["comments"]["pageInfo"]["hasNextPage"] = "false"
    app = adapter(tmp_path, monkeypatch, Client(data))
    with pytest.raises(GitHubError) as caught:
        await app.fetch_threads(limit=1)
    assert caught.value.kind is GitHubErrorKind.UNEXPECTED_RESPONSE
