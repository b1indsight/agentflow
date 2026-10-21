"""Repository label bootstrap preserves user definitions with disposable scan state."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from githubkit import GitHub

from app.controler.api import create_app
from app.controler.reconciliation.labels import LabelSynchronizer
from app.controler.runtime import ServerRuntime
from app.github import Batch, GitHubApp, GitHubError, GitHubErrorKind, Installation, Repository
from app.github.repositories import LabelDefinition
from tests.test_github_client import (
    AsyncEndpoint,
    app_client,
    install_client,
    request_failed,
    response,
    settings,
)


async def test_scan_paginates_filtered_inventory_and_only_checks_current_grants(tmp_path, monkeypatch):
    now, granted = 0.0, True
    monkeypatch.setattr("app.controler.reconciliation.labels.monotonic", lambda: now)

    async def installations(*, page, **kwargs):
        # Entirely filtered pages must not hide the active repository on the next page.
        return response([SimpleNamespace(id=i, suspended_at="paused") for i in range(1, 101)]
                        if page == 1 else [SimpleNamespace(id=101, suspended_at=None)])

    async def repositories(*, page, **kwargs):
        items = ([SimpleNamespace(full_name=f"owner/old-{i}", archived=True) for i in range(100)]
                 if page == 1 else [SimpleNamespace(full_name="owner/ready", archived=False)])
        return response(SimpleNamespace(repositories=items if granted else []))

    client = SimpleNamespace(rest=SimpleNamespace(apps=SimpleNamespace(
        async_list_installations=installations,
        async_list_repos_accessible_to_installation=repositories,
    )))
    app_client(monkeypatch, lambda **kwargs: client)
    install_client(monkeypatch, lambda _, **kwargs: client)
    checked = []

    async def ensure(self, definitions):
        checked.append(self.full_name)
        return ()

    monkeypatch.setattr(Repository, "ensure_labels", ensure)
    configured = settings(tmp_path)
    sync = LabelSynchronizer(GitHubApp(configured), configured)
    await sync.sweep()
    assert checked == ["owner/ready"]
    granted, now = False, 86400.0
    await sync.sweep()
    assert checked == ["owner/ready"]


async def test_label_bootstrap_reads_all_pages_and_only_creates_missing_definitions(
    tmp_path, monkeypatch,
):
    github = GitHubApp(settings(tmp_path))
    existing = [SimpleNamespace(name=f"user-{i}") for i in range(100)]
    existing.append(SimpleNamespace(name="Flow", color="ffffff", description="user text"))

    async def listed(owner, repo, *, page, per_page):
        return response(existing[(page - 1) * per_page:page * per_page])

    create = AsyncEndpoint(response(SimpleNamespace(name="custom:review")))
    client = SimpleNamespace(rest=SimpleNamespace(issues=SimpleNamespace(
        async_list_labels_for_repo=listed, async_create_label=create,
    )))
    install_client(monkeypatch, lambda _, **kwargs: client)
    definitions = (
        LabelDefinition(name="flow", color="5319e7", description="new text"),
        LabelDefinition(name="custom:review", color="fbca04", description="review"),
    )
    repo = github.installation(42).repository("owner/repo")
    assert await repo.ensure_labels(definitions) == ("custom:review",)
    existing.append(SimpleNamespace(name="custom:review"))
    assert await repo.ensure_labels(definitions) == ()
    assert len(create.calls) == 1
    assert create.calls[0][1]["name"] == "custom:review"
    # An inaccessible repository is not an empty label list and must never cause writes.
    client.rest.issues.async_list_labels_for_repo = AsyncEndpoint(error=request_failed(404))
    with pytest.raises(GitHubError):
        await repo.ensure_labels(definitions)
    assert len(create.calls) == 1


@pytest.mark.parametrize("found", [True, False])
async def test_duplicate_create_requires_positive_label_evidence(tmp_path, monkeypatch, found):
    # Another controller/user can create a definition between our list and POST.
    create = AsyncEndpoint(error=request_failed(422))
    get = AsyncEndpoint(response(SimpleNamespace(name="Flow")) if found else None,
                        error=None if found else request_failed(404))
    client = SimpleNamespace(rest=SimpleNamespace(issues=SimpleNamespace(
        async_list_labels_for_repo=AsyncEndpoint(response([])),
        async_create_label=create, async_get_label=get,
    )))
    install_client(monkeypatch, lambda _, **kwargs: client)
    repo = GitHubApp(settings(tmp_path)).installation(42).repository("owner/repo")
    definitions = (LabelDefinition(name="flow", color="5319e7", description="start"),)
    if found:
        assert await repo.ensure_labels(definitions) == ()
    else:
        with pytest.raises(GitHubError) as caught:
            await repo.ensure_labels(definitions)
        assert caught.value.status_code == 422
    assert len(create.calls) == len(get.calls) == 1


def remote_inventory(monkeypatch, github, names):
    installation = github.installation(42)

    async def installations(self, *, page=1):
        return Batch(items=(installation,))

    async def repositories(self, *, page=1):
        return Batch(items=tuple(self.repository(name) for name in names))

    monkeypatch.setattr(GitHubApp, "fetch_installations", installations)
    monkeypatch.setattr(Installation, "fetch_repositories", repositories)


async def test_scan_cadence_retries_failures_and_rescans_after_restart(tmp_path, monkeypatch):
    configured = settings(tmp_path).model_copy(update={"done_label": "custom:done"})
    github = GitHubApp(configured)
    remote_inventory(monkeypatch, github, ["owner/pending", "owner/ready"])
    now = 0.0
    monkeypatch.setattr("app.controler.reconciliation.labels.monotonic", lambda: now)
    calls, failed = [], {"owner/pending"}

    async def ensure(self, definitions):
        assert definitions[-1].name == "custom:done"
        calls.append(self.full_name)
        if self.full_name in failed:
            raise GitHubError(GitHubErrorKind.PERMISSION, "create_label", status_code=403)
        return ()

    monkeypatch.setattr(Repository, "ensure_labels", ensure)
    sync = LabelSynchronizer(github, configured)
    assert await sync.sweep() == 300
    assert calls == ["owner/pending", "owner/ready"]
    now = 300.0
    failed.clear()
    await sync.sweep()
    assert calls == ["owner/pending", "owner/ready", "owner/pending"]
    now = 86100.0
    await sync.sweep()
    assert len(calls) == 3
    now = 86400.0
    failed.add("owner/ready")
    assert await sync.sweep() == 300
    assert calls[-1] == "owner/ready" and len(calls) == 4
    failed.clear()
    now += 300
    await sync.sweep()
    assert calls[-2:] == ["owner/pending", "owner/ready"] and len(calls) == 6
    # A restart discards timing hints, so both repositories are read again.
    await LabelSynchronizer(github, configured).sweep()
    assert len(calls) == 8


async def test_lost_creation_response_is_recovered_by_listing_without_repeating_write(
    tmp_path, monkeypatch,
):
    # GitHub accepted the POST, but the response never reached the controller.
    labels, requests = [], []

    def transport(request):
        requests.append(request.method)
        if request.method == "POST":
            body = json.loads(request.content)
            labels.append({"id": 1, "node_id": "label-1", "url": "https://example.com/label",
                           "default": False, **body})
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json=labels, request=request)

    def client(_, *, auto_retry):
        assert auto_retry is False
        return GitHub("test-token", auto_retry=False, async_transport=httpx.MockTransport(transport))

    install_client(monkeypatch, client)
    repo = GitHubApp(settings(tmp_path)).installation(42).repository("owner/repo")
    definitions = (LabelDefinition(name="flow", color="5319e7", description="start"),)
    with pytest.raises(GitHubError) as caught:
        await repo.ensure_labels(definitions)
    assert caught.value.kind is GitHubErrorKind.TRANSIENT
    assert await repo.ensure_labels(definitions) == ()
    assert requests == ["GET", "POST", "GET"]


@pytest.mark.parametrize("target,method,kind,status", [
    (GitHubApp, "fetch_installations", GitHubErrorKind.RATE_LIMITED, 429),
    (Installation, "fetch_repositories", GitHubErrorKind.TRANSIENT, 503),
    (Repository, "ensure_labels", GitHubErrorKind.RATE_LIMITED, 429),
])
async def test_run_honors_retry_after_without_requesting_another_repository(
    tmp_path, monkeypatch, target, method, kind, status,
):
    configured = settings(tmp_path)
    github = GitHubApp(configured)
    remote_inventory(monkeypatch, github, ["owner/limited", "owner/other"])
    blocked = AsyncEndpoint(error=GitHubError(kind, method, status_code=status, retry_after="900"))
    monkeypatch.setattr(target, method, blocked)

    async def sleep(delay):
        assert delay == 900
        raise asyncio.CancelledError

    monkeypatch.setattr("app.controler.reconciliation.labels.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await LabelSynchronizer(github, configured).run()
    assert len(blocked.calls) == 1


async def test_background_label_discovery_does_not_block_readiness_and_stops_with_server(
    postgresql_url, tmp_path, monkeypatch,
):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def installations(self, *, page=1):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(GitHubApp, "fetch_installations", installations)
    configured = settings(tmp_path).model_copy(update={"database_url": postgresql_url})
    application = create_app(ServerRuntime(configured))
    async with application.router.lifespan_context(application):
        await asyncio.wait_for(entered.wait(), 3)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test",
        ) as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/readyz")).status_code == 200
    assert cancelled.is_set()
