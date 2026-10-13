import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from githubkit import GitHub
from githubkit.exception import RequestFailed
from githubkit.response import Response
from pydantic import BaseModel, SecretStr, ValidationError

from app.controler.config import ServerSettings
from app.github import (
    GitHubApp,
    GitHubError,
    GitHubErrorKind,
    Installation,
    Issue,
    PullRequest,
    PushCapability,
    Repository,
)


class AsyncEndpoint:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


def install_client(monkeypatch, factory):
    def client(installation, *, auto_retry=True):
        return factory(installation.installation_id, auto_retry=auto_retry)

    monkeypatch.setattr(Installation, "_client", client)


def app_client(monkeypatch, factory):
    monkeypatch.setattr(GitHubApp, "_client", lambda self, **kwargs: factory(**kwargs))


@pytest.mark.parametrize("method,target", [("fetch_issue", 7), ("fetch_ref", "flow/7")])
async def test_preparation_rate_limit_is_returned_without_sdk_sleep(tmp_path, monkeypatch, method, target):
    requests = []

    def limited(request):
        requests.append(request)
        return httpx.Response(403, json={"message": "API rate limit exceeded"},
                              headers={"X-RateLimit-Remaining": "0", "Retry-After": "600"}, request=request)

    app = GitHubApp(settings(tmp_path))

    def installation(installation_id, *, auto_retry):
        assert auto_retry is False
        return GitHub("test-token", auto_retry=auto_retry, async_transport=httpx.MockTransport(limited))

    install_client(monkeypatch, installation)
    with pytest.raises(GitHubError) as caught:
        await asyncio.wait_for(getattr(app.installation(42).repository("owner/repo"), method)(target), 1)
    assert caught.value.kind is GitHubErrorKind.RATE_LIMITED
    assert caught.value.retry_after == "600" and len(requests) == 1


@pytest.mark.parametrize("status,ancestor,behind,expected", [
    ("ahead", "base", 0, True), ("behind", "head", 2, False),
])
async def test_revision_lineage_requires_github_fast_forward(
    tmp_path, monkeypatch, status, ancestor, behind, expected,
):
    endpoint = AsyncEndpoint(SimpleNamespace(parsed_data=SimpleNamespace(
        status=status, merge_base_commit=SimpleNamespace(sha=ancestor),
        base_commit=SimpleNamespace(sha="base"), behind_by=behind,
    )))
    client = SimpleNamespace(rest=SimpleNamespace(repos=SimpleNamespace(async_compare_commits=endpoint)))
    app = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, lambda _, **kwargs: client)
    assert await app.installation(1).repository("owner/repo").compare_commits("base", "head") is expected
    assert endpoint.calls == [(("owner", "repo", "base...head"), {"per_page": 1})]


async def test_review_files_preserve_facts_and_page_requests(tmp_path, monkeypatch):
    calls = []

    async def files(*args, **kwargs):
        calls.append((args, kwargs))
        count = 20 if kwargs["page"] == 1 else 1
        return response([SimpleNamespace(filename=f"{kwargs['page']}-{i}.py", status="modified",
            patch=None if i == 0 else "+line", previous_filename=None) for i in range(count)])

    client = SimpleNamespace(rest=SimpleNamespace(pulls=SimpleNamespace(async_list_files=files)))

    def installed(installation_id, *, auto_retry):
        assert installation_id == 1 and auto_retry is False
        return client

    app = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, installed)
    pr = PullRequest(app.installation(1).repository("owner/repo"), 17)
    result = [item async for item in pr.list_files()]
    assert calls == [
        (("owner", "repo", 17), {"per_page": 20, "page": 1}),
        (("owner", "repo", 17), {"per_page": 20, "page": 2}),
    ]
    assert len(result) == 21
    assert result[0].patch is None
    assert result[20].filename == "2-0.py"


async def test_inline_comments_preserve_bodies_and_page_requests(tmp_path, monkeypatch):
    calls = []

    def discussion(kind):
        async def endpoint(*args, **kwargs):
            calls.append((kind, args, kwargs))
            count = 10 if kwargs["page"] == 1 else 1
            return response([SimpleNamespace(id=i + kwargs["page"] * 10, body="x" * 5000,
                user=SimpleNamespace(login="reviewer"), updated_at=datetime.now(UTC),
                submitted_at=datetime.now(UTC), path="app.py", original_commit_id="old",
                diff_hunk="+x") for i in range(count)])

        return endpoint

    client = SimpleNamespace(rest=SimpleNamespace(
        pulls=SimpleNamespace(async_list_review_comments=discussion("inline")),
    ))

    def installed(installation_id, *, auto_retry):
        assert installation_id == 1 and auto_retry is False
        return client

    app = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, installed)
    pr = PullRequest(app.installation(1).repository("owner/repo"), 17)
    result = [item async for item in pr.list_review_comments()]
    assert calls == [
        ("inline", ("owner", "repo", 17), {"per_page": 10, "page": 1}),
        ("inline", ("owner", "repo", 17), {"per_page": 10, "page": 2}),
    ]
    assert len(result) == 11
    assert all(len(item.body) == 5000 for item in result)


def response(data: Any) -> SimpleNamespace:
    return SimpleNamespace(parsed_data=data)


def settings(tmp_path: Path) -> ServerSettings:
    key = tmp_path / "app.pem"
    key.write_text("test-private-key")
    return ServerSettings(
        database_url="postgresql://localhost/flowtool",
        worker_tokens={"worker-1": SecretStr("secret")},
        github_app_id=123,
        github_private_key_path=key,
    )


def test_push_capability_never_represents_or_serializes_secret() -> None:
    token = "ghs_this-must-stay-secret"
    capability = PushCapability(
        token=SecretStr(token),
        repository="octo/repo",
        permissions={"contents": "write"},
        expires_at=datetime(2026, 9, 5, 10, tzinfo=UTC),
    )

    assert capability.token.get_secret_value() == token
    assert token not in repr(capability)
    assert token not in capability.model_dump_json()


def test_github_app_requires_positive_app_id_and_readable_nonempty_key(tmp_path: Path) -> None:
    configured = settings(tmp_path)

    with pytest.raises(ValueError, match="positive integer"):
        GitHubApp(configured.model_copy(update={"github_app_id": 0}))
    with pytest.raises(FileNotFoundError):
        GitHubApp(
            configured.model_copy(update={"github_private_key_path": tmp_path / "missing.pem"})
        )
    empty_key = tmp_path / "empty.pem"
    empty_key.write_text("")
    with pytest.raises(ValueError, match="empty file"):
        GitHubApp(configured.model_copy(update={"github_private_key_path": empty_key}))


@pytest.mark.parametrize("configured_timeout", [None, "12.5"])
def test_both_github_auth_clients_use_finite_timeouts(
    configured_timeout: str | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # A client left at githubkit's timeout=None can hold result recovery indefinitely.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLOWTOOL_GITHUB_REQUEST_TIMEOUT_SECONDS", raising=False)
    if configured_timeout is not None:
        monkeypatch.setenv("FLOWTOOL_GITHUB_REQUEST_TIMEOUT_SECONDS", configured_timeout)
    github = GitHubApp(settings(tmp_path))
    expected = float(configured_timeout) if configured_timeout is not None else 30.0
    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    github._sdk.config = replace(github._sdk.config, transport=transport, async_transport=transport)

    for binding in (github, github.installation(42)):
        original = binding._sdk.config
        original_retry = original.auto_retry
        read = binding._client()
        write = binding._client(auto_retry=False)
        later_read = binding._client()
        for client in (read, write, later_read):
            assert client.auth is binding._sdk.auth
            timeout = client.config.timeout
            assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (expected,) * 4
            assert client.config.transport is transport and client.config.async_transport is transport
        assert write.config.auto_retry is None
        assert read.config.auto_retry is not None and later_read.config.auto_retry is not None
        assert binding._sdk.config is original and binding._sdk.config.auto_retry is original_retry


@pytest.mark.asyncio
async def test_fetches_issue_repository_and_remote_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = AsyncEndpoint(
        response(SimpleNamespace(number=7, title="Ship it", body=None, state="open"))
    )
    repository = AsyncEndpoint(
        response(SimpleNamespace(full_name="octo/repo", default_branch="trunk"))
    )
    get_ref = AsyncEndpoint(
        response(SimpleNamespace(ref="refs/heads/topic", object_=SimpleNamespace(sha="abc123")))
    )
    client = SimpleNamespace(
        rest=SimpleNamespace(
            issues=SimpleNamespace(async_get=issue),
            repos=SimpleNamespace(async_get=repository),
            git=SimpleNamespace(async_get_ref=get_ref),
        )
    )
    github = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, lambda installation_id, **kwargs: client)

    observed_issue = await github.installation(42).repository("octo/repo").fetch_issue(7)
    observed_repository = await github.installation(42).repository("octo/repo").refetch()
    remote_ref = await github.installation(42).repository("octo/repo").fetch_ref("topic")

    assert observed_issue.model_dump() == {
        "number": 7,
        "title": "Ship it",
        "body": "",
        "state": "open",
    }
    assert observed_repository.model_dump() == {
        "full_name": "octo/repo",
        "default_branch": "trunk",
    }
    assert remote_ref.model_dump() == {"branch": "topic", "sha": "abc123"}
    assert issue.calls == [(("octo", "repo", 7), {})]
    assert repository.calls == [(("octo", "repo"), {})]
    assert get_ref.calls == [(("octo", "repo", "heads/topic"), {})]


@pytest.mark.parametrize("full_name", ["other/repo", "OCTO/REPO"])
async def test_repository_refetch_keeps_identity_and_observed_casing(tmp_path, monkeypatch, full_name):
    # A redirected response must not retarget a resource; casing remains an observed fact.
    endpoint = AsyncEndpoint(response(SimpleNamespace(full_name=full_name, default_branch="trunk")))
    sdk = SimpleNamespace(rest=SimpleNamespace(repos=SimpleNamespace(async_get=endpoint)))
    install_client(monkeypatch, lambda _, **kwargs: sdk)
    installation = GitHubApp(settings(tmp_path)).installation(42)
    before = Repository(installation, "octo/repo", default_branch="main")
    if full_name == "other/repo":
        with pytest.raises(GitHubError) as caught:
            await before.refetch()
        assert caught.value.kind is GitHubErrorKind.UNEXPECTED_RESPONSE
        assert caught.value.endpoint == "get_repository"
    else:
        after = await before.refetch()
        assert after.full_name == full_name and after.default_branch == "trunk"
    assert before.full_name == "octo/repo" and before.default_branch == "main"
    assert endpoint.calls == [(("octo", "repo"), {})]


def test_partial_pull_request_distinguishes_unobserved_nullable_facts_from_null():
    # Old short receipts omit repo and merge details; absence cannot become a known null.
    short = {"number": 17, "state": "open", "head_branch": "topic",
             "head_sha": "abc", "base_branch": "main"}
    pr = PullRequest.model_validate(short)
    assert pr.model_dump() == short
    for name in ("repo", "head_repository", "merged_at", "merge_commit_sha"):
        with pytest.raises(AttributeError):
            getattr(pr, name)
    explicit = PullRequest.model_validate(short | {
        "head_repository": None, "merged_at": None, "merge_commit_sha": None,
    })
    assert explicit.head_repository is explicit.merged_at is explicit.merge_commit_sha is None
    assert explicit.model_dump() == short | {
        "head_repository": None, "merged_at": None, "merge_commit_sha": None,
    }
    with pytest.raises(ValidationError):
        PullRequest.model_validate(short | {"merged": True})


def test_pull_request_preserves_persisted_json_field_order():
    # Frozen context digests use this JSON, including the historical order of its fields.
    original = (
        '{"number":17,"state":"open","head_branch":"topic","head_sha":"abc",'
        '"base_branch":"main","repo":"octo/repo","title":"PR","body":"",'
        '"base_sha":"def","head_repository":null,"merged":false,"merged_at":null,'
        '"merge_commit_sha":null}'
    )
    assert PullRequest.model_validate_json(original).model_dump_json() == original


def test_bound_resources_serialize_and_restore_inside_standard_container(tmp_path):
    class Document(BaseModel):
        repository: Repository
        issue: Issue
        pull_request: PullRequest

    # Default nested serialization must omit unknown sentinels and all authentication references.
    repository = GitHubApp(settings(tmp_path)).installation(42).repository("octo/repo")
    document = Document(
        repository=repository, issue=Issue(repository, 7), pull_request=PullRequest(repository, 17),
    )
    expected = {"repository": {"full_name": "octo/repo"}, "issue": {"number": 7},
                "pull_request": {"number": 17, "repo": "octo/repo"}}
    assert document.model_dump() == expected
    with pytest.raises(AttributeError):
        _ = document.issue.state
    restored = Document.model_validate_json(document.model_dump_json())
    assert restored.model_dump() == expected
    assert type(restored.repository) is Repository and type(restored.issue) is Issue
    assert type(restored.pull_request) is PullRequest
    for resource, parent in ((restored.repository, "installation"),
                             (restored.issue, "repository"), (restored.pull_request, "repository")):
        with pytest.raises(AttributeError, match="not bound"):
            getattr(resource, parent)


@pytest.mark.asyncio
async def test_creates_repository_scoped_contents_push_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_token = AsyncEndpoint(
        response(
            SimpleNamespace(
                token="ghs_scoped",
                expires_at="2026-09-05T11:30:00Z",
                permissions=SimpleNamespace(contents="write"),
                repositories=[SimpleNamespace(full_name="octo/repo")],
            )
        )
    )
    client = SimpleNamespace(
        rest=SimpleNamespace(
            apps=SimpleNamespace(async_create_installation_access_token=create_token)
        )
    )
    github = GitHubApp(settings(tmp_path))
    app_client(monkeypatch, lambda **kwargs: client)

    capability = await github.installation(42).repository("octo/repo").create_push_capability()

    assert capability.repository == "octo/repo"
    assert capability.permissions == {"contents": "write"}
    assert capability.expires_at == datetime(2026, 9, 5, 11, 30, tzinfo=UTC)
    assert capability.token.get_secret_value() == "ghs_scoped"
    assert create_token.calls == [
        ((42,), {"repositories": ["repo"], "permissions": {"contents": "write"}})
    ]


@pytest.mark.asyncio
async def test_finds_and_opens_pull_requests_and_skips_empty_reviewers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = SimpleNamespace(
        number=9,
        state="open",
        head=SimpleNamespace(ref="topic", sha="abc123"),
        base=SimpleNamespace(ref="trunk"),
    )
    created = SimpleNamespace(
        number=10,
        state="open",
        head=SimpleNamespace(ref="new-topic", sha="def456"),
        base=SimpleNamespace(ref="trunk"),
    )
    list_pulls = AsyncEndpoint(response([existing]))
    create_pull = AsyncEndpoint(response(created))
    reviewers = AsyncEndpoint(response(None))
    client = SimpleNamespace(
        rest=SimpleNamespace(
            pulls=SimpleNamespace(
                async_list=list_pulls,
                async_create=create_pull,
                async_request_reviewers=reviewers,
            )
        )
    )
    github = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, lambda installation_id, **kwargs: client)

    found = await github.installation(42).repository("octo/repo").find_pull_request_by_head("topic")
    opened = await github.installation(42).repository("octo/repo").open_pull_request("new-topic", "trunk", "Title", "Body")
    await PullRequest(github.installation(42).repository("octo/repo"), 10).request_reviewers([])

    assert found is not None and found.number == 9
    assert found.head_branch == "topic"
    assert opened.number == 10
    assert opened.head_sha == "def456"
    assert list_pulls.calls == [
        (("octo", "repo"), {"state": "all", "head": "octo:topic", "per_page": 2})
    ]
    assert create_pull.calls == [
        (
            ("octo", "repo"),
            {"title": "Title", "head": "new-topic", "base": "trunk", "body": "Body"},
        )
    ]
    assert reviewers.calls == []


def request_failed(status: int, **headers: str) -> RequestFailed:
    request = httpx.Request("GET", "https://api.github.com/repos/octo/repo")
    raw = httpx.Response(status, request=request, headers=headers, json={"message": "secret"})
    return RequestFailed(Response(raw, dict))


@pytest.mark.asyncio
async def test_comment_recovery_checks_author_and_paginates(tmp_path, monkeypatch) -> None:
    # A copied marker in a user's comment must not acknowledge an unperformed bot effect.
    marker = "<!-- flowtool:effect:stable -->"
    pages = []

    async def list_comments(*args, page, **kwargs):
        pages.append(page)
        if page == 1:
            return response(
                [
                    SimpleNamespace(
                        id=n,
                        body=marker,
                        user=SimpleNamespace(login="alice", type="User"),
                    )
                    for n in range(100)
                ]
            )
        return response(
            [
                SimpleNamespace(
                    id=700,
                    body=f"Done.\n\n{marker}",
                    user=SimpleNamespace(login="flowtool[bot]", type="Bot"),
                )
            ]
        )

    client = SimpleNamespace(
        rest=SimpleNamespace(
            issues=SimpleNamespace(
                async_list_comments=list_comments,
                async_create_comment=AsyncEndpoint(response(SimpleNamespace(id=701))),
            )
        )
    )
    github = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, lambda _, **kwargs: client)
    assert await PullRequest(github.installation(42).repository("octo/repo"), 10).find_comment(marker) == 700
    assert pages == [1, 2]
    assert await PullRequest(github.installation(42).repository("octo/repo"), 10).comment("Another comment") == 701


@pytest.mark.asyncio
async def test_reviewer_reconciliation_includes_completed_reviews_across_pages(
    tmp_path, monkeypatch
):
    # A reviewer may finish before an interrupted request is reconciled; do not request it twice.
    since = datetime(2026, 9, 6, tzinfo=UTC)
    pages = []

    def review(login, submitted_at, state="APPROVED"):
        return SimpleNamespace(
            user=SimpleNamespace(login=login),
            submitted_at=submitted_at,
            state=state,
        )

    async def list_reviews(*args, page, **kwargs):
        pages.append(page)
        if page == 1:
            return response([review("old", since - timedelta(days=1)) for _ in range(100)])
        return response([review("finished", since), review("draft", since, "PENDING")])

    client = SimpleNamespace(
        rest=SimpleNamespace(
            pulls=SimpleNamespace(
                async_get=AsyncEndpoint(
                    response(
                        SimpleNamespace(
                            requested_reviewers=[SimpleNamespace(login="requested")],
                        )
                    )
                ),
                async_list_reviews=list_reviews,
            )
        )
    )
    github = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, lambda _, **kwargs: client)
    assert await PullRequest(github.installation(42).repository("octo/repo"), 10).fetch_reviewers(since=since) == {"requested", "finished"}
    assert pages == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "kind", "retryable"),
    [
        (401, {}, GitHubErrorKind.AUTHENTICATION, False),
        (403, {}, GitHubErrorKind.PERMISSION, False),
        (404, {}, GitHubErrorKind.NOT_FOUND, False),
        (408, {}, GitHubErrorKind.UNEXPECTED_RESPONSE, False),
        (409, {}, GitHubErrorKind.CONFLICT, False),
        (422, {}, GitHubErrorKind.VALIDATION, False),
        (429, {"Retry-After": "30"}, GitHubErrorKind.RATE_LIMITED, True),
        (503, {}, GitHubErrorKind.TRANSIENT, True),
    ],
)
async def test_maps_github_failures_without_response_bodies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, str],
    kind: GitHubErrorKind,
    retryable: bool,
) -> None:
    headers["X-GitHub-Request-Id"] = "request-123"
    get_issue = AsyncEndpoint(error=request_failed(status, **headers))
    client = SimpleNamespace(rest=SimpleNamespace(issues=SimpleNamespace(async_get=get_issue)))
    github = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, lambda installation_id, **kwargs: client)

    with pytest.raises(GitHubError) as captured:
        await github.installation(42).repository("octo/repo").fetch_issue(7)

    error = captured.value
    assert error.kind is kind
    assert error.retryable is retryable
    assert error.definitely_rejected is (400 <= status < 500 and status != 408)
    assert error.request_id == "request-123"
    assert "secret" not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_error", [httpx.ConnectError, httpx.ReadTimeout])
async def test_maps_real_githubkit_transport_wrappers_as_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport_error: type[httpx.RequestError],
) -> None:
    # githubkit wraps failures raised by httpx.send; production outages therefore do not expose
    # the raw HTTPX types used by simpler endpoint fakes.
    def fail(request: httpx.Request) -> httpx.Response:
        raise transport_error("GitHub transport unavailable", request=request)

    client = GitHub("test-token", async_transport=httpx.MockTransport(fail))
    github = GitHubApp(settings(tmp_path))
    install_client(monkeypatch, lambda installation_id, **kwargs: client)

    with pytest.raises(GitHubError) as captured:
        await github.installation(42).repository("octo/repo").fetch_issue(7)

    assert captured.value.kind is GitHubErrorKind.TRANSIENT
    assert captured.value.retryable is True
