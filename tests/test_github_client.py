from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from githubkit import GitHub
from githubkit.exception import RequestFailed
from githubkit.response import Response
from pydantic import SecretStr

from app.config import ServeSettings
from app.github_client import GitHubApp
from app.github_models import GitHubAdapterError, GitHubErrorKind, PushCapability


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


def response(data: Any) -> SimpleNamespace:
    return SimpleNamespace(parsed_data=data)


def settings(tmp_path: Path) -> ServeSettings:
    key = tmp_path / "app.pem"
    key.write_text("test-private-key")
    return ServeSettings(
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


@pytest.mark.asyncio
async def test_reads_issue_repository_and_remote_ref_as_internal_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = AsyncEndpoint(
        response(SimpleNamespace(number=7, title="Ship it", body=None, state="open"))
    )
    repository = AsyncEndpoint(
        response(SimpleNamespace(full_name="octo/repo", default_branch="trunk"))
    )
    get_ref = AsyncEndpoint(
        response(
            SimpleNamespace(
                ref="refs/heads/topic", object_=SimpleNamespace(sha="abc123")
            )
        )
    )
    client = SimpleNamespace(
        rest=SimpleNamespace(
            issues=SimpleNamespace(async_get=issue),
            repos=SimpleNamespace(async_get=repository),
            git=SimpleNamespace(async_get_ref=get_ref),
        )
    )
    github = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(github, "_installation_client", lambda installation_id: client)

    issue_snapshot = await github.get_issue(42, "octo/repo", 7)
    repository_snapshot = await github.get_repository(42, "octo/repo")
    ref_snapshot = await github.get_ref(42, "octo/repo", "topic")

    assert issue_snapshot.model_dump() == {
        "number": 7,
        "title": "Ship it",
        "body": "",
        "state": "open",
    }
    assert repository_snapshot.model_dump() == {
        "full_name": "octo/repo",
        "default_branch": "trunk",
    }
    assert ref_snapshot.model_dump() == {"branch": "topic", "sha": "abc123"}
    assert issue.calls == [(('octo', 'repo', 7), {})]
    assert repository.calls == [(('octo', 'repo'), {})]
    assert get_ref.calls == [(('octo', 'repo', 'heads/topic'), {})]


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
    monkeypatch.setattr(github, "_app_client", lambda: client)

    capability = await github.create_push_capability(42, "octo/repo")

    assert capability.repository == "octo/repo"
    assert capability.permissions == {"contents": "write"}
    assert capability.expires_at == datetime(2026, 9, 5, 11, 30, tzinfo=UTC)
    assert capability.token.get_secret_value() == "ghs_scoped"
    assert create_token.calls == [
        ((42,), {"repositories": ["repo"], "permissions": {"contents": "write"}})
    ]

    create_token.result = response(
        SimpleNamespace(
            token="ghs_overbroad",
            expires_at="2026-09-05T11:30:00Z",
            permissions=SimpleNamespace(contents="write", issues="write"),
            repositories=[SimpleNamespace(full_name="octo/repo")],
        )
    )
    with pytest.raises(GitHubAdapterError) as captured:
        await github.create_push_capability(42, "octo/repo")
    assert captured.value.kind is GitHubErrorKind.UNEXPECTED_RESPONSE


@pytest.mark.asyncio
async def test_pull_request_primitives_map_snapshots_and_skip_empty_reviewers(
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
    monkeypatch.setattr(github, "_installation_client", lambda installation_id: client)

    found = await github.find_pull_request_by_head(42, "octo/repo", "topic")
    opened = await github.open_pull_request(
        42, "octo/repo", "new-topic", "trunk", "Title", "Body"
    )
    await github.request_reviewers(42, "octo/repo", 10, [])

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
@pytest.mark.parametrize(
    ("status", "headers", "kind", "retryable"),
    [
        (401, {}, GitHubErrorKind.AUTHENTICATION, False),
        (403, {}, GitHubErrorKind.PERMISSION, False),
        (404, {}, GitHubErrorKind.NOT_FOUND, False),
        (409, {}, GitHubErrorKind.CONFLICT, False),
        (422, {}, GitHubErrorKind.VALIDATION, False),
        (429, {"Retry-After": "30"}, GitHubErrorKind.RATE_LIMITED, True),
        (503, {}, GitHubErrorKind.TRANSIENT, True),
        (418, {}, GitHubErrorKind.UNEXPECTED_RESPONSE, False),
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
    client = SimpleNamespace(
        rest=SimpleNamespace(issues=SimpleNamespace(async_get=get_issue))
    )
    github = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(github, "_installation_client", lambda installation_id: client)

    with pytest.raises(GitHubAdapterError) as captured:
        await github.get_issue(42, "octo/repo", 7)

    error = captured.value
    assert error.kind is kind
    assert error.retryable is retryable
    assert error.request_id == "request-123"
    assert "secret" not in str(error)


@pytest.mark.asyncio
async def test_rate_limit_403_is_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    get_issue = AsyncEndpoint(
        error=request_failed(403, **{"X-RateLimit-Remaining": "0", "Retry-After": "12"})
    )
    client = SimpleNamespace(
        rest=SimpleNamespace(issues=SimpleNamespace(async_get=get_issue))
    )
    github = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(github, "_installation_client", lambda installation_id: client)

    with pytest.raises(GitHubAdapterError) as captured:
        await github.get_issue(42, "octo/repo", 7)

    assert captured.value.kind is GitHubErrorKind.RATE_LIMITED
    assert captured.value.retry_after == "12"


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
    monkeypatch.setattr(github, "_installation_client", lambda installation_id: client)

    with pytest.raises(GitHubAdapterError) as captured:
        await github.get_issue(42, "octo/repo", 7)

    assert captured.value.kind is GitHubErrorKind.TRANSIENT
    assert captured.value.retryable is True
