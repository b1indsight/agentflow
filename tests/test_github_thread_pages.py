"""Actual adapter pagination distinguishes thread cursors from nested comment cursors."""

import json
from types import SimpleNamespace

import pytest

from app.control.resolution import covered_thread
from app.github import GitHubApp, GitHubError, GitHubErrorKind, PullRequest, ReviewThread
from app.github.thread_pages import ThreadCursor
from app.github.threads import MAX_THREAD_BYTES
from tests.test_github_client import AsyncEndpoint, install_client, settings
from tests.test_github_feedback import adapter
from tests.test_review_resolution import feedback


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
    page = await pr.list_review_thread_page(None)

    observations = (full.threads[0], single, page.snapshot.threads[0])
    assert all(type(item) is ReviewThread and item.pull_request is pr for item in observations)
    assert all(item.comments == single.comments for item in observations)
    comment = full.threads[0].comments[0]
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


async def test_real_sdk_missing_nested_node_keeps_outer_continuation(tmp_path, monkeypatch):
    import httpx
    from githubkit import GitHub

    responses = iter([
        {"data": {"node": None}, "errors": [{"type": "NOT_FOUND", "message": "not accessible", "path": ["node"]}]},
        {"data": outer(node("T2"))},
    ])
    client = GitHub("test-token", auto_retry=False, async_transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=next(responses)),
    ))
    app = adapter(tmp_path, monkeypatch, client)
    cursor = ThreadCursor(thread_id="T1", comments="c20", following="t1").model_dump_json()
    missing = await app.list_review_thread_page(cursor)
    assert missing.snapshot.limitations == ("thread:missing-node",)
    assert missing.next_cursor is not None
    later = await app.list_review_thread_page(missing.next_cursor)
    assert later.snapshot.threads[0].thread_id == "T2"


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
    cursor = ThreadCursor(thread_id="T1", comments="c20", following="t1").model_dump_json() if nested else None
    with pytest.raises(GitHubError):
        await app.list_review_thread_page(cursor)


async def test_nested_and_outer_pages_resume_with_distinct_cursors(tmp_path, monkeypatch):
    client = Client(outer(node(following="comments-20"), following="threads-1"),
                    {"node": node(number=21)}, outer(node("T2", 22)))
    app = adapter(tmp_path, monkeypatch, client)
    first = await app.list_review_thread_page(None)
    second = await app.list_review_thread_page(first.next_cursor)
    third = await app.list_review_thread_page(second.next_cursor)
    assert first.snapshot.threads[0].comments[0].github_id == 2**33 + 1
    assert not first.snapshot.complete and not second.snapshot.complete
    assert third.snapshot.complete and third.next_cursor is None
    assert client.calls[0][1]["after"] is None
    assert client.calls[1][1] == {"id": "T1", "comments": "comments-20"}
    assert client.calls[2][1]["after"] == "threads-1"
    for query, _ in client.calls:
        assert "fullDatabaseId" in query and "mutation" not in query
        assert "isResolved" in query
    # Reading a complete traversal does not make the final fragment complete evidence.
    thread = second.snapshot.threads[0]
    assert not covered_thread(thread, [feedback(comment) for comment in thread.comments])


async def test_removed_nested_thread_does_not_trap_later_threads(tmp_path, monkeypatch):
    client = Client({"node": None}, outer(node("T2")))
    app = adapter(tmp_path, monkeypatch, client)
    cursor = ThreadCursor(thread_id="T1", comments="c20", following="t1").model_dump_json()
    missing = await app.list_review_thread_page(cursor)
    assert missing.snapshot.limitations == ("thread:missing-node",)
    assert missing.next_cursor is not None
    later = await app.list_review_thread_page(missing.next_cursor)
    assert later.snapshot.threads[0].thread_id == "T2"


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
        await app.list_review_thread_page(None)
    assert caught.value.kind is GitHubErrorKind.UNEXPECTED_RESPONSE


async def test_cursor_is_validated_before_network_io(tmp_path, monkeypatch):
    endpoint = AsyncEndpoint()
    app = adapter(tmp_path, monkeypatch, SimpleNamespace(async_graphql=endpoint))
    with pytest.raises(GitHubError):
        await app.list_review_thread_page(json.dumps({"thread_id": "T1"}))
    assert endpoint.calls == []
