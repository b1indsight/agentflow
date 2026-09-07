"""Thread identity, independent pagination and App-owned reply recovery at the SDK boundary."""

from types import SimpleNamespace

import pytest

from app.github_client import GitHubApp
from app.github_models import GitHubAdapterError
from tests.test_github_client import settings


def comment(number, *, author="alice", bot=False, body="Fix this"):
    return {"databaseId": number, "id": f"C{number}", "body": body,
            "updatedAt": "2026-09-07T10:00:00Z", "author": {
                "login": author, "__typename": "Bot" if bot else "User"},
            "replyTo": None, "originalCommit": {"oid": "baseline"}}


def connection(nodes, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None,
                                         "endCursor": cursor}}


def thread(number=1, *, comments=None, cursor=None):
    return {"id": f"T{number}", "isResolved": False, "isOutdated": False, "path": "app.py",
            "pullRequest": {"number": 17, "repository": {"nameWithOwner": "owner/repo"}},
            "comments": connection(comments or [comment(number)], cursor)}


async def test_threads_and_comments_have_independent_cursors(tmp_path, monkeypatch):
    calls = []

    async def graphql(query, variables):
        calls.append(variables)
        if "id" in variables:
            assert variables == {"id": "T1", "cursor": "comments-1"}
            return {"node": thread(comments=[comment(999)])}
        first = variables["cursor"] is None
        nodes = [thread(i) for i in range(1, 101)] if first else [thread(101)]
        if first:
            nodes[0] = thread(comments=[comment(i) for i in range(201, 221)],
                              cursor="comments-1")
        return {"repository": {"pullRequest": {
            "reviewThreads": connection(nodes, "threads-1" if first else None)}}}

    app = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(app, "_installation_client", lambda _: SimpleNamespace(async_graphql=graphql))
    result = await app.list_review_threads(1, "owner/repo", 17)
    assert result.complete and len(result.threads) == 101
    assert len(result.threads[0].comments) == 21
    assert result.threads[0].comments[-1].node_id == "C999"
    assert calls[-1]["cursor"] == "threads-1"


@pytest.mark.parametrize("fault", ["cursor", "partial", "wrong-pr", "missing-node"])
async def test_invalid_thread_reads_never_look_complete(tmp_path, monkeypatch, fault):
    async def graphql(query, variables):
        if fault == "partial":
            return {"errors": [{"type": "FORBIDDEN"}], "repository": {}}
        if fault == "missing-node":
            return {"node": None}
        node = thread(cursor="same" if fault == "cursor" else None)
        if fault == "wrong-pr":
            node["pullRequest"]["number"] = 18
        return {"node": node}

    app = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(app, "_installation_client", lambda _: SimpleNamespace(async_graphql=graphql))
    if fault == "partial":
        with pytest.raises(GitHubAdapterError):
            await app.get_review_thread(1, "owner/repo", 17, "T1")
    else:
        result = await app.get_review_thread(1, "owner/repo", 17, "T1")
        assert not result.complete


async def test_reply_uses_root_rest_endpoint_and_exact_app_marker(tmp_path, monkeypatch):
    writes = []

    async def reply(*args, **kwargs):
        writes.append((args, kwargs))
        return SimpleNamespace(parsed_data=SimpleNamespace(id=900))

    async def graphql(query, variables):
        return {"node": thread(comments=[comment(1), comment(2, body="MARK"),
            comment(3, author="flowtool[bot]", bot=True, body="prefix MARK"),
            comment(4, author="flowtool[bot]", bot=True, body="note\nMARK")])}

    app = GitHubApp(settings(tmp_path))
    app._bot_login = "flowtool[bot]"
    client = SimpleNamespace(async_graphql=graphql, rest=SimpleNamespace(pulls=SimpleNamespace(
        async_create_reply_for_review_comment=reply)))
    monkeypatch.setattr(app, "_installation_client", lambda _: client)
    assert await app.find_review_reply(1, "owner/repo", 17, "T1", "MARK") == 4
    assert await app.reply_review_thread(1, "owner/repo", 17, 1, "note") == 900
    assert writes == [(("owner", "repo", 17, 1), {"body": "note"})]


@pytest.mark.parametrize("limit", ["requests", "bytes"])
async def test_thread_snapshot_has_a_shared_finite_budget(tmp_path, monkeypatch, limit):
    from app import github_threads

    monkeypatch.setattr(github_threads, "MAX_THREAD_REQUESTS", 2)
    if limit == "bytes":
        monkeypatch.setattr(github_threads, "MAX_THREAD_BYTES", 10)
    calls = []

    async def graphql(query, variables):
        calls.append(variables)
        node = thread(comments=[comment(len(calls))], cursor=str(len(calls)))
        return {"node": node}

    app = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(app, "_installation_client", lambda _: SimpleNamespace(async_graphql=graphql))
    result = await app.get_review_thread(1, "owner/repo", 17, "T1")
    assert not result.complete
    assert len(calls) == (1 if limit == "bytes" else 2)


@pytest.mark.parametrize("kind,expected,status", [
    ("FORBIDDEN", "permission", 403), ("RATE_LIMITED", "rate_limited", 429),
    ("NOT_FOUND", "not_found", 404), ("INTERNAL", "transient", None),
])
async def test_sdk_graphql_errors_with_partial_data_are_classified(kind, expected, status):
    from githubkit.exception import GraphQLFailed
    from githubkit.graphql.models import GraphQLResponse

    async def partial():
        raise GraphQLFailed(GraphQLResponse.model_validate({
            "data": {"node": None}, "errors": [{"type": kind, "message": "private details"}],
        }))

    with pytest.raises(GitHubAdapterError) as error:
        await GitHubApp._execute("thread", partial)
    assert error.value.kind.value == expected and error.value.status_code == status
    assert "private details" not in str(error.value)
