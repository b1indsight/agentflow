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


async def test_inventory_pages_skip_suspended_and_archived_without_losing_next_page(
    tmp_path, monkeypatch,
):
    github = GitHubApp(settings(tmp_path))
    installs = AsyncEndpoint(response([
        SimpleNamespace(id=i, suspended_at=None if i == 1 else "suspended")
        for i in range(1, 101)
    ]))
    repos = AsyncEndpoint(response(SimpleNamespace(repositories=[
        SimpleNamespace(full_name=f"owner/repo-{i}", archived=i != 1)
        for i in range(100)
    ])))

    def client(*, auto_retry):
        assert auto_retry is False
        return SimpleNamespace(rest=SimpleNamespace(apps=SimpleNamespace(
            async_list_installations=installs,
            async_list_repos_accessible_to_installation=repos,
        )))

    app_client(monkeypatch, client)
    install_client(monkeypatch, lambda _, **kwargs: client(**kwargs))
    page = await github.fetch_installations()
    assert [item.installation_id for item in page.items] == [1]
    assert page.next_cursor == "2"
    repositories = await page.items[0].fetch_repositories()
    assert [item.full_name for item in repositories.items] == ["owner/repo-1"]
    assert repositories.next_cursor == "2"
    installs.result = response([])
    repos.result = response(SimpleNamespace(repositories=[]))
    assert (await github.fetch_installations(page=2)).next_cursor is None
    assert (await page.items[0].fetch_repositories(page=2)).next_cursor is None
    assert installs.calls[-1] == ((), {"per_page": 100, "page": 2})


async def test_label_bootstrap_reads_all_pages_and_only_creates_missing_definitions(
    tmp_path, monkeypatch,
):
    github = GitHubApp(settings(tmp_path))
    existing = [SimpleNamespace(name=f"user-{i}") for i in range(100)]
    existing.append(SimpleNamespace(name="Flow", color="ffffff", description="user text"))
    calls = []

    async def listed(owner, repo, *, page, per_page):
        calls.append(page)
        return response(existing[(page - 1) * per_page:page * per_page])

    create = AsyncEndpoint(response(SimpleNamespace(name="custom:review")))

    def client(_, *, auto_retry):
        assert auto_retry is False
        return SimpleNamespace(rest=SimpleNamespace(issues=SimpleNamespace(
            async_list_labels_for_repo=listed, async_create_label=create,
        )))

    install_client(monkeypatch, client)
    definitions = (
        LabelDefinition(name="flow", color="5319e7", description="new text"),
        LabelDefinition(name="custom:review", color="fbca04", description="review"),
    )
    repo = github.installation(42).repository("owner/repo")
    assert await repo.ensure_labels(definitions) == ("custom:review",)
    assert calls == [1, 2]
    assert create.calls == [(("owner", "repo"), {
        "name": "custom:review", "color": "fbca04", "description": "review",
    })]
    assert existing[-1].description == "user text"
    existing.append(SimpleNamespace(name="custom:review"))
    assert await repo.ensure_labels(definitions) == ()
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


async def test_mixed_repositories_use_daily_cache_and_rescan_after_restart(
    tmp_path, monkeypatch,
):
    configured = settings(tmp_path)
    github = GitHubApp(configured)
    remote_inventory(monkeypatch, github, ["owner/pending", "owner/ready"])
    now = 0.0
    monkeypatch.setattr("app.controler.reconciliation.labels.monotonic", lambda: now)
    calls = []
    pending = True

    async def ensure(self, definitions):
        calls.append((self.full_name, tuple(item.name for item in definitions)))
        if self.name == "pending" and pending:
            raise GitHubError(GitHubErrorKind.PERMISSION, "create_label", status_code=403)
        return ()

    monkeypatch.setattr(Repository, "ensure_labels", ensure)
    sync = LabelSynchronizer(github, configured)
    assert await sync.sweep() == 300
    assert [name for name, _ in calls] == ["owner/pending", "owner/ready"]
    now = 300.0
    pending = False
    await sync.sweep()
    assert [name for name, _ in calls] == ["owner/pending", "owner/ready", "owner/pending"]
    now = 86100.0
    await sync.sweep()
    assert len(calls) == 3
    now = 86400.0
    await sync.sweep()
    assert calls[-1][0] == "owner/ready" and len(calls) == 4
    # A restart discards timing hints: read both repositories again instead of recovering state.
    await LabelSynchronizer(github, configured).sweep()
    assert len(calls) == 6
    changed = configured.model_copy(update={"done_label": "custom:done"})
    await LabelSynchronizer(github, changed).sweep()
    assert len(calls) == 8
    assert calls[-1][1][-1] == "custom:done"


async def test_daily_failure_returns_to_300_second_checks(
    tmp_path, monkeypatch,
):
    configured = settings(tmp_path)
    github = GitHubApp(configured)
    remote_inventory(monkeypatch, github, ["owner/repo"])
    now = 0.0
    monkeypatch.setattr("app.controler.reconciliation.labels.monotonic", lambda: now)
    ensure = AsyncEndpoint(())
    monkeypatch.setattr(Repository, "ensure_labels", ensure)
    sync = LabelSynchronizer(github, configured)
    await sync.sweep()
    now = 86400.0
    ensure.error = GitHubError(GitHubErrorKind.TRANSIENT, "list_labels", status_code=503)
    assert await sync.sweep() == 300
    assert len(ensure.calls) == 2
    ensure.error = None
    now += 300
    await sync.sweep()
    assert len(ensure.calls) == 3
    now += 300
    await sync.sweep()
    assert len(ensure.calls) == 3


@pytest.mark.parametrize("status", [403, 404, 503])
async def test_unreadable_label_list_never_creates_labels(tmp_path, monkeypatch, status):
    requests = []

    def transport(request):
        requests.append(request.method)
        return httpx.Response(status, json={"message": "unavailable"}, request=request)

    install_client(monkeypatch, lambda _, **kwargs: GitHub(
        "test-token", auto_retry=False, async_transport=httpx.MockTransport(transport),
    ))
    repo = GitHubApp(settings(tmp_path)).installation(42).repository("owner/repo")
    with pytest.raises(GitHubError) as caught:
        await repo.ensure_labels((LabelDefinition(name="flow", color="5319e7", description="start"),))
    assert caught.value.status_code == status
    assert requests == ["GET"]


async def test_lost_creation_response_is_recovered_by_listing_without_repeating_write(
    tmp_path, monkeypatch,
):
    # GitHub accepted the POST, but the response never reached the controller.
    labels, requests = [], []

    def transport(request):
        requests.append(request.method)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body == {"name": "flow", "color": "5319e7", "description": "start"}
            labels.append({"id": 1, "node_id": "label-1", "url": "https://example.com/label",
                           "default": False, **body})
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json=labels, request=request)

    install_client(monkeypatch, lambda _, **kwargs: GitHub(
        "test-token", auto_retry=False, async_transport=httpx.MockTransport(transport),
    ))
    repo = GitHubApp(settings(tmp_path)).installation(42).repository("owner/repo")
    definitions = (LabelDefinition(name="flow", color="5319e7", description="start"),)
    with pytest.raises(GitHubError) as caught:
        await repo.ensure_labels(definitions)
    assert caught.value.kind is GitHubErrorKind.TRANSIENT
    assert await repo.ensure_labels(definitions) == ()
    assert requests == ["GET", "POST", "GET"]


@pytest.mark.parametrize("limited_endpoint", ["inventory", "repositories", "labels"])
async def test_run_honors_retry_after_without_requesting_another_repository(
    tmp_path, monkeypatch, limited_endpoint,
):
    configured = settings(tmp_path)
    github = GitHubApp(configured)
    remote_inventory(monkeypatch, github, ["owner/limited", "owner/other"])
    calls = []

    async def installations(self, *, page=1):
        calls.append("inventory")
        if limited_endpoint == "inventory":
            raise GitHubError(GitHubErrorKind.RATE_LIMITED, "inventory", retry_after="900")
        return Batch(items=(self.installation(42),))

    async def repositories(self, *, page=1):
        calls.append("repositories")
        raise GitHubError(GitHubErrorKind.TRANSIENT, "repositories",
                          status_code=503, retry_after="900")

    async def ensure(self, definitions):
        calls.append(self.full_name)
        raise GitHubError(GitHubErrorKind.RATE_LIMITED, "labels", retry_after="900")

    async def sleep(delay):
        assert delay == 900
        raise asyncio.CancelledError

    monkeypatch.setattr(GitHubApp, "fetch_installations", installations)
    monkeypatch.setattr(Repository, "ensure_labels", ensure)
    if limited_endpoint == "repositories":
        monkeypatch.setattr(Installation, "fetch_repositories", repositories)
    monkeypatch.setattr("app.controler.reconciliation.labels.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await LabelSynchronizer(github, configured).run()
    assert "owner/other" not in calls


async def test_scan_paginates_current_grants_and_does_not_check_removed_repository(
    tmp_path, monkeypatch,
):
    configured = settings(tmp_path)
    github = GitHubApp(configured)
    granted = True
    visited, checked = [], []

    async def installations(self, *, page=1):
        visited.append(("installations", page))
        return Batch(items=(self.installation(40 + page),), next_cursor="2" if page == 1 else None)

    async def repositories(self, *, page=1):
        visited.append((self.installation_id, page))
        return Batch(items=(self.repository(f"owner/repo-{self.installation_id}-{page}"),)
                     if granted else (), next_cursor="2" if page == 1 else None)

    async def ensure(self, definitions):
        checked.append(self.full_name)
        return ()

    monkeypatch.setattr(GitHubApp, "fetch_installations", installations)
    monkeypatch.setattr(Installation, "fetch_repositories", repositories)
    monkeypatch.setattr(Repository, "ensure_labels", ensure)
    sync = LabelSynchronizer(github, configured)
    await sync.sweep()
    assert visited == [("installations", 1), (41, 1), (41, 2),
                       ("installations", 2), (42, 1), (42, 2)]
    assert checked == ["owner/repo-41-1", "owner/repo-41-2", "owner/repo-42-1", "owner/repo-42-2"]
    granted = False
    await sync.sweep()
    assert len(checked) == 4


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
