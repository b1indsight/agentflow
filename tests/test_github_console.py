"""Console source contracts, with SDK HTTP and optional Actions lookup fixtures."""

from types import SimpleNamespace

import httpx
import pytest
from githubkit import GitHub
from pydantic import BaseModel

from app.github import GitHubApp, GitHubError, GitHubErrorKind
from app.github.console import read_source, webhook_observations
from tests.test_github_client import AsyncEndpoint, install_client, response, settings


def comment(**changes):
    return {
        "id": 9,
        "node_id": "IC_9",
        "body": "full " * 5000,
        "user": None,
        "url": "https://api.github.com/repos/owner/repo/issues/comments/9",
        "html_url": "https://github.com/owner/repo/issues/7#issuecomment-9",
        "issue_url": "https://api.github.com/repos/owner/repo/issues/7",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        **changes,
    }


def adapter(tmp_path, monkeypatch, handler):
    app = GitHubApp(settings(tmp_path))
    sdk = GitHub("test-token", auto_retry=None, async_transport=httpx.MockTransport(handler))
    install_client(monkeypatch, lambda _, **kwargs: sdk)
    return app.installation(42).repository("owner/repo")


async def test_sdk_comments_full_body_paging_and_webhook_identity(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/repos/owner/repo/issues/7/comments"
        return httpx.Response(200, json=[comment()] if request.url.params["page"] == "1" else [])

    repo = adapter(tmp_path, monkeypatch, handler)
    first = await read_source(repo, 7, 17, "issue.comments", limit=1)
    assert first.items[0].body == comment()["body"]
    assert first.child_sources == ["comment.reactions:9"]
    assert not (await read_source(repo, 7, 17, "issue.comments", first.next_cursor, 1)).items
    assert len(calls) == 2
    with pytest.raises(ValueError, match="cursor"):
        await read_source(repo, 7, 17, "pr.comments", first.next_cursor, 1)
    payload = {"repository": {"full_name": "owner/repo"}, "action": "edited", "comment": comment()}
    edit = webhook_observations("issue_comment", payload)[0]
    deleted = webhook_observations("issue_comment", payload | {"action": "deleted"})[0]
    assert edit.key == first.items[0].key == deleted.key
    assert edit.body == deleted.body == first.items[0].body
    assert not edit.deleted and deleted.deleted
    assert webhook_observations("issue_comment", payload | {"comment": {"id": 9}}) == []


def test_check_run_author_is_the_producing_app_name():
    # CI check runs identify their producing app, without a user/actor/author login.
    payload = {
        "repository": {"full_name": "owner/repo"},
        "action": "completed",
        "check_run": {
            "id": 9,
            "name": "check (3.10)",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:01:00Z",
            "app": {"name": "GitHub Actions", "slug": "github-actions"},
        },
    }
    item = webhook_observations("check_run", payload)[0]
    assert item.author == "GitHub Actions"
    assert item.metadata["workflow_name"] is None
    assert item.metadata["workflow_name_reason"] == "not_loaded"


class CheckNode(BaseModel):
    id: int
    name: str = "check (3.10)"
    started_at: str = "2026-01-01T00:00:00Z"
    status: str = "completed"
    conclusion: str | None = "success"
    app: dict[str, str] = {"name": "GitHub Actions", "slug": "github-actions"}
    check_suite: dict[str, int]


@pytest.mark.parametrize("suites", [(11, 11), (11, 12)])
async def test_check_workflows_use_suite_identity_and_deduplicate_reads(tmp_path, monkeypatch, suites):
    # A workflow can have many matrix jobs; another workflow on the same head has its own suite.
    checks = [CheckNode(id=index, check_suite={"id": suite}) for index, suite in enumerate(suites, 1)]
    listing = AsyncEndpoint(response(SimpleNamespace(check_runs=checks)))
    calls = []

    async def workflows(owner, name, **kwargs):
        calls.append(kwargs)
        suite = kwargs["check_suite_id"]
        return response(SimpleNamespace(workflow_runs=[SimpleNamespace(
            check_suite_id=suite, name="CI" if suite == 11 else "Release",
        )]))

    client = SimpleNamespace(rest=SimpleNamespace(
        checks=SimpleNamespace(async_list_for_ref=listing),
        actions=SimpleNamespace(async_list_workflow_runs_for_repo=workflows),
    ))
    install_client(monkeypatch, lambda _, **kwargs: client)
    repo = GitHubApp(settings(tmp_path)).installation(42).repository("owner/repo")
    page = await read_source(repo, 7, 17, "commit.check_runs:" + "a" * 40)
    assert [item.metadata["workflow_name"] for item in page.items] == [
        "CI" if suite == 11 else "Release" for suite in suites
    ]
    assert all(item.title == "check (3.10)" and item.author == "GitHub Actions" for item in page.items)
    assert all(item.metadata["conclusion"] == "success" for item in page.items)
    assert [call["check_suite_id"] for call in calls] == sorted(set(suites))
    assert all(call["per_page"] == 1 for call in calls)


@pytest.mark.parametrize("kind", [
    GitHubErrorKind.PERMISSION, GitHubErrorKind.NOT_FOUND, GitHubErrorKind.RATE_LIMITED,
    GitHubErrorKind.TRANSIENT,
])
async def test_workflow_lookup_failure_keeps_check_rows_and_their_status(tmp_path, monkeypatch, kind):
    # Optional Actions metadata must not hide checks when the App lacks Actions read.
    listing = AsyncEndpoint(response(SimpleNamespace(check_runs=[
        CheckNode(id=1, check_suite={"id": 11}, status="in_progress", conclusion=None),
        CheckNode(id=2, check_suite={"id": 12}, status="in_progress", conclusion=None),
    ])))
    workflows = AsyncEndpoint(error=GitHubError(kind, "console_check_workflow"))
    client = SimpleNamespace(rest=SimpleNamespace(
        checks=SimpleNamespace(async_list_for_ref=listing),
        actions=SimpleNamespace(async_list_workflow_runs_for_repo=workflows),
    ))
    install_client(monkeypatch, lambda _, **kwargs: client)
    repo = GitHubApp(settings(tmp_path)).installation(42).repository("owner/repo")
    page = await read_source(repo, 7, 17, "commit.check_runs:" + "a" * 40)
    assert len(page.items) == 2
    for item in page.items:
        assert item.metadata["workflow_name"] is None
        assert item.metadata["workflow_name_reason"] == kind.value
        assert item.metadata["status"] == "in_progress" and item.metadata["conclusion"] is None
    assert len(workflows.calls) == (2 if kind == GitHubErrorKind.NOT_FOUND else 1)
    assert page.limitations == ["expired_remote_logs_unrecoverable"]


async def test_cross_referenced_timeline_events_without_ids_have_stable_distinct_keys(tmp_path, monkeypatch):
    # GitHub's cross-reference event has no id/node_id, unlike other timeline events.
    issue = comment(body="referencing PR") | {
        "number": 17, "state": "closed", "title": "PR", "labels": [], "user": None,
        "milestone": None, "locked": False, "comments": 0, "closed_at": None,
        "repository_url": "https://api.github.com/repos/owner/repo",
        "labels_url": "https://api.github.com/repos/owner/repo/issues/17/labels",
        "comments_url": "https://api.github.com/repos/owner/repo/issues/17/comments",
        "events_url": "https://api.github.com/repos/owner/repo/issues/17/events",
    }
    events = [
        {"event": "cross-referenced", "actor": None, "source": {"type": "issue", "issue": issue},
         "created_at": f"2026-01-02T00:00:0{index}Z", "updated_at": "2026-01-02T00:00:02Z"}
        for index in range(2)
    ]
    repo = adapter(tmp_path, monkeypatch, lambda request: httpx.Response(200, json=events))
    first = await read_source(repo, 7, 17, "issue.timeline")
    assert len(first.items) == 2 and len({item.key for item in first.items}) == 2
    assert all(item.metadata["event"] == "cross-referenced" for item in first.items)
    issue["title"] = "edited source PR"
    events[0]["updated_at"] = "2026-01-03T00:00:00Z"
    again = await read_source(repo, 7, 17, "issue.timeline")
    assert [item.key for item in first.items] == [item.key for item in again.items]


async def test_threads_use_separate_bounded_reply_connection(tmp_path, monkeypatch):
    def handler(request):
        import json

        payload = json.loads(request.content)
        pull = {"number": 17, "repository": {"nameWithOwner": "owner/repo"}}
        node = {
            "id": "IC_9",
            "fullDatabaseId": 9,
            "body": "reply body",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-02T00:00:00Z",
            "url": "https://github.com/owner/repo/pull/17#discussion_r9",
            "author": None,
            "commit": {"oid": "a" * 40},
            "path": "a.py",
            "line": 5,
            "diffHunk": "+x",
        }
        if "reviewThreads" in payload["query"]:
            thread = {
                "id": "T_1",
                "isResolved": False,
                "isOutdated": False,
                "path": "a.py",
                "line": 5,
                "pullRequest": pull,
                "comments": {"nodes": [node]},
            }
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [thread],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                }
                            }
                        }
                    }
                },
            )
        assert payload["variables"]["id"] == "T_1"
        return httpx.Response(
            200,
            json={
                "data": {
                    "node": {
                        "pullRequest": pull,
                        "comments": {
                            "nodes": [node],
                            "pageInfo": {"hasNextPage": True, "endCursor": "after"},
                        },
                    }
                }
            },
        )

    repo = adapter(tmp_path, monkeypatch, handler)
    threads = await read_source(repo, 7, 17, "pr.review_threads")
    assert threads.child_sources == ["pr.review_thread_replies:T_1"]
    replies = await read_source(repo, 7, 17, threads.child_sources[0])
    assert replies.items[0].parent_key == threads.items[0].key
    assert replies.items[0].body == "reply body" and replies.next_cursor
    assert replies.child_sources == ["inline.reactions:9"]


@pytest.mark.parametrize(
    "status,headers,kind",
    [
        (404, {}, GitHubErrorKind.NOT_FOUND),
        (403, {}, GitHubErrorKind.PERMISSION),
        (429, {"Retry-After": "600"}, GitHubErrorKind.RATE_LIMITED),
    ],
)
async def test_source_errors_are_not_remote_deletions(tmp_path, monkeypatch, status, headers, kind):
    repo = adapter(
        tmp_path,
        monkeypatch,
        lambda request: httpx.Response(status, headers=headers, json={"message": "unavailable"}),
    )
    with pytest.raises(GitHubError) as error:
        await read_source(repo, 7, None, "issue.description")
    assert error.value.kind == kind
    if status == 429:
        assert error.value.retry_after == "600"
