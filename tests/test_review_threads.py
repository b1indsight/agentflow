"""Thread identity, independent pagination and App-owned reply recovery at the SDK boundary."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.github import (
    GitHubApp,
    GitHubError,
    GitHubErrorKind,
    PullRequest,
    ReviewThread,
    ReviewThreads,
)
from app.github.errors import translate_errors
from tests.test_github_client import install_client, settings


def test_identity_only_thread_never_proves_coverage():
    # Recovery can know a thread ID before reading its comments or resolution.
    from datetime import UTC, datetime

    identity = ReviewThread(thread_id="T1", repo="owner/repo", pr_number=17)
    scan = ReviewThreads(repo="owner/repo", pr_number=17, fetched_at=datetime.now(UTC),
                         threads=(identity,))
    assert not scan.complete
    restored = ReviewThreads.model_validate_json(scan.model_dump_json())
    assert type(restored.threads[0]) is ReviewThread and not restored.complete
    assert restored.threads[0].model_dump() == {
        "thread_id": "T1", "repo": "owner/repo", "pr_number": 17,
    }
    with pytest.raises(AttributeError):
        _ = restored.threads[0].resolved
    with pytest.raises(ValidationError):
        ReviewThread.model_validate(identity.model_dump() | {"pull_request": "unexpected"})


def comment(number, *, author="alice", bot=False, body="Fix this", reply_to=None):
    return {"fullDatabaseId": str(number), "id": f"C{number}", "body": body,
            "updatedAt": "2026-09-07T10:00:00Z", "author": {
                "login": author, "__typename": "Bot" if bot else "User"},
            "replyTo": {"fullDatabaseId": str(reply_to)} if reply_to is not None else None,
            "originalCommit": {"oid": "baseline"}}


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
            assert variables == {"id": "T1", "cursor": "comments-1", "limit": 20}
            return {"node": thread(comments=[comment(999)])}
        first = variables["cursor"] is None
        nodes = [thread(i) for i in range(1, 101)] if first else [thread(101)]
        if first:
            nodes[0] = thread(comments=[comment(i) for i in range(201, 221)],
                              cursor="comments-1")
        return {"repository": {"pullRequest": {
            "reviewThreads": connection(nodes, "threads-1" if first else None)}}}

    app = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, lambda _, **kwargs: SimpleNamespace(async_graphql=graphql))
    result = await PullRequest(app.installation(1).repository("owner/repo"), 17).list_review_threads()
    assert result.complete and len(result.threads) == 101
    assert len(result.threads[0].comments.items) == 21
    assert result.threads[0].comments.items[-1].node_id == "C999"
    assert calls[-1]["cursor"] == "threads-1"


@pytest.mark.parametrize("fault", [
    "cursor", "partial", "wrong-pr", "missing-node", "duplicate-comment", "missing-comment",
])
async def test_invalid_thread_reads_never_look_complete(tmp_path, monkeypatch, fault):
    async def graphql(query, variables):
        if fault == "partial":
            return {"errors": [{"type": "FORBIDDEN"}], "repository": {}}
        if fault == "missing-node":
            return {"node": None}
        node = thread(cursor="same" if fault == "cursor" else None)
        if fault == "wrong-pr":
            node["pullRequest"]["number"] = 18
        elif fault == "duplicate-comment":
            # A response can encode the same ID as either a number or a string.
            node["comments"]["nodes"].append(comment(1) | {"fullDatabaseId": 1})
        elif fault == "missing-comment":
            node["comments"]["nodes"].append(None)
        return {"node": node}

    app = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, lambda _, **kwargs: SimpleNamespace(async_graphql=graphql))
    if fault == "partial":
        with pytest.raises(GitHubError):
            await ReviewThread(PullRequest(app.installation(1).repository("owner/repo"), 17), "T1").refetch()
    else:
        result = await ReviewThread(PullRequest(app.installation(1).repository("owner/repo"), 17), "T1").refetch()
        assert not result.complete


async def test_reply_uses_root_rest_endpoint_and_exact_app_marker(tmp_path, monkeypatch):
    writes = []

    async def reply(*args, **kwargs):
        writes.append((args, kwargs))
        return SimpleNamespace(parsed_data=SimpleNamespace(id=900))

    async def graphql(query, variables):
        return {"node": thread(comments=[comment(1), comment(2, body="MARK", reply_to=1),
            comment(3, author="flowtool[bot]", bot=True, body="prefix MARK", reply_to=1),
            comment(4, author="flowtool[bot]", bot=True, body="note\nMARK", reply_to=1)])}

    app = GitHubApp(settings(tmp_path))
    client = SimpleNamespace(async_graphql=graphql, rest=SimpleNamespace(pulls=SimpleNamespace(
        async_create_reply_for_review_comment=reply)))
    install_client(monkeypatch, lambda _, **kwargs: client)
    resource = ReviewThread(PullRequest(app.installation(1).repository("owner/repo"), 17),
                            "T1", root_comment_id=1)
    assert await resource.find_reply("MARK") == 4
    assert await resource.reply("note") == 900
    assert writes == [(("owner", "repo", 17, 1), {"body": "note"})]


@pytest.mark.parametrize(("root", "later_root"), [(1, False), (2, False), (None, False), (1, True)])
async def test_observed_thread_checks_root_identity_without_inventing_it(
    tmp_path, monkeypatch, root, later_root,
):
    # Combining a node ID with another root's database ID could send a reply to the wrong thread.
    graphql_calls = []
    writes = []

    async def graphql(query, variables):
        graphql_calls.append(variables)
        if later_root:
            # Recovery knows a root ID before the root itself appears on a later page.
            if variables["cursor"] is None:
                return {"node": thread(comments=[comment(3, reply_to=2)], cursor="root-page")}
            assert variables["cursor"] == "root-page"
            return {"node": thread(comments=[comment(2)])}
        return {"node": thread()}

    async def reply(*args, **kwargs):
        writes.append((args, kwargs))
        return SimpleNamespace(parsed_data=SimpleNamespace(id=900))

    sdk = SimpleNamespace(async_graphql=graphql, rest=SimpleNamespace(pulls=SimpleNamespace(
        async_create_reply_for_review_comment=reply)))
    install_client(monkeypatch, lambda _, **kwargs: sdk)
    pr = PullRequest(GitHubApp(settings(tmp_path)).installation(42).repository("owner/repo"), 17)
    before = ReviewThread(pr, "T1", root_comment_id=root)
    if root == 2 or later_root:
        with pytest.raises(GitHubError) as caught:
            await before.refetch()
        assert caught.value.kind is GitHubErrorKind.UNEXPECTED_RESPONSE
        assert caught.value.endpoint == "get_review_thread"
    else:
        after = await before.refetch()
        assert after is not before
        assert after.root_comment_id == root
        if root is None:
            with pytest.raises(ValueError, match="known root"):
                await after.reply("note")
        else:
            with pytest.raises(ValueError, match="root comment ID"):
                ReviewThread(pr, "T1", root_comment_id=2, comments=after.comments)
            assert await after.reply("note") == 900
    assert len(graphql_calls) == (2 if later_root else 1)
    assert len(writes) == (1 if root == 1 and not later_root else 0)
    assert before.root_comment_id == root
    assert "comments" not in before.model_dump(exclude_unset=True)


async def test_resolve_returns_new_immutable_evidence_without_an_extra_read(tmp_path, monkeypatch):
    # A confirmed mutation changes only resolved; old comments and their observation stay fixed.
    calls = []

    async def graphql(query, variables):
        calls.append(variables)
        if "threadId" in variables:
            return {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}
        return {"node": thread()}

    app = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, lambda _, **kwargs: SimpleNamespace(async_graphql=graphql))
    bound = ReviewThread(PullRequest(app.installation(1).repository("owner/repo"), 17), "T1")
    before = await bound.refetch()
    after = await before.resolve()
    assert after is not before and after.resolved and not before.resolved
    assert after.fetched_at == before.fetched_at and after.complete == before.complete
    assert after.comments == before.comments
    assert len(calls) == 2 and calls[1] == {"threadId": "T1"}
    with pytest.raises(AttributeError):
        before.comments.items.append(before.comments.items[0])
    exported = before.model_dump(mode="json", exclude_unset=True)
    exported["comments"]["items"][0]["body"] = "local edit"
    assert before.comments.items[0].body == "Fix this"
    restored = ReviewThread.model_validate_json(before.model_dump_json(exclude_unset=True))
    assert type(restored) is type(before) and restored.comments == before.comments
    with pytest.raises(AttributeError):
        _ = restored.pull_request


# A cancelled resolve carries no confirmation; recovery must retain the observed state.
@pytest.mark.parametrize("failure", [
    pytest.param(GitHubError(GitHubErrorKind.TRANSIENT, "resolve_review_thread"), id="unknown-result"),
    pytest.param(asyncio.CancelledError(), id="cancelled"),
])
async def test_unknown_resolve_result_keeps_original_state(tmp_path, monkeypatch, failure):
    async def graphql(query, variables):
        if "threadId" in variables:
            raise failure
        return {"node": thread()}

    app = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, lambda _, **kwargs: SimpleNamespace(async_graphql=graphql))
    before = await ReviewThread(PullRequest(app.installation(1).repository("owner/repo"), 17), "T1").refetch()
    facts_before = before.model_dump()
    with pytest.raises(type(failure)) as caught:
        await before.resolve()
    assert caught.value is failure
    assert before.resolved is False
    assert before.model_dump() == facts_before


@pytest.mark.parametrize("limit", ["requests", "bytes"])
async def test_thread_snapshot_has_a_shared_finite_budget(tmp_path, monkeypatch, limit):
    from app.github import threads as github_threads

    monkeypatch.setattr(github_threads, "MAX_THREAD_REQUESTS", 2)
    if limit == "bytes":
        monkeypatch.setattr(github_threads, "MAX_THREAD_BYTES", 10)
    calls = []

    async def graphql(query, variables):
        calls.append(variables)
        node = thread(comments=[comment(len(calls))], cursor=str(len(calls)))
        return {"node": node}

    app = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, lambda _, **kwargs: SimpleNamespace(async_graphql=graphql))
    result = await ReviewThread(PullRequest(app.installation(1).repository("owner/repo"), 17), "T1").refetch()
    assert not result.complete
    assert len(calls) == (1 if limit == "bytes" else 2)


@pytest.mark.parametrize("kind,expected,status", [
    ("FORBIDDEN", "permission", 403), ("RATE_LIMITED", "rate_limited", 429),
    ("NOT_FOUND", "not_found", 404), ("INTERNAL", "transient", None),
])
def test_sdk_graphql_errors_with_partial_data_are_classified(kind, expected, status):
    from githubkit.exception import GraphQLFailed
    from githubkit.graphql.models import GraphQLResponse

    with pytest.raises(GitHubError) as error, translate_errors("thread"):
        raise GraphQLFailed(GraphQLResponse.model_validate({
            "data": {"node": None}, "errors": [{"type": kind, "message": "private details"}],
        }))
    assert error.value.kind.value == expected and error.value.status_code == status
    assert "private details" not in str(error.value)
