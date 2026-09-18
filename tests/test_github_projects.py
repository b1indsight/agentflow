"""Projects contracts: IDs, complete page facts, and single-attempt writes."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from githubkit import GitHub
from pydantic import ValidationError
from typer.testing import CliRunner

from app import cli
from app.github_client import GitHubApp
from app.github_models import GitHubAdapterError, GitHubErrorKind
from app.project_models import ProjectConfig, ProjectStatus
from tests.test_github_client import AsyncEndpoint, response, settings


def config(**changes):
    return ProjectConfig(installation_id=42, project_id="P", status_field_id="F",
                         options={status: status.value for status in ProjectStatus}, **changes)


def target():
    return {"project": {"id": "P", "title": "Renamed board", "closed": False,
                        "viewerCanUpdate": True, "owner": {"__typename": "Organization", "login": "owner"}},
            "field": {"id": "F", "name": "Renamed status", "project": {"id": "P"},
                      "options": [{"id": status.value, "name": "Renamed"} for status in ProjectStatus]}}


def item(content="I", *, archived=False):
    return {"id": "ITEM", "project": {"id": "P"}, "isArchived": archived,
            "content": {"__typename": "Issue", "id": content} if content else None}


def page(nodes, following=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": following is not None, "endCursor": following}}


class Client:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    async def async_graphql(self, query, variables):
        self.calls.append((query, variables))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def adapter(tmp_path, monkeypatch, *responses, installation=42):
    app = GitHubApp(settings(tmp_path))
    client = Client(*responses)
    lookup = AsyncEndpoint(response(SimpleNamespace(
        id=installation, account=SimpleNamespace(type="Organization", login="owner"),
    )))

    def installed(number, *, auto_retry):
        assert number == 42 and auto_retry is False
        return client

    def app_client(*, auto_retry):
        assert auto_retry is False
        return SimpleNamespace(rest=SimpleNamespace(apps=SimpleNamespace(async_get_repo_installation=lookup)))

    monkeypatch.setattr(app, "_installation_client", installed)
    monkeypatch.setattr(app, "_app_client", app_client)
    return app.project_client(config()), client, lookup


def test_config_is_optional_and_strict(tmp_path, monkeypatch):
    assert settings(tmp_path).projects == {}
    monkeypatch.setenv("FLOWTOOL_PROJECTS", '{"Owner/Repo": ' + config().model_dump_json() + '}')
    assert settings(tmp_path).projects == {"owner/repo": config()}
    for change in ({"installation_id": 0}, {"project_id": " "}, {"options": {"todo": "x"}},
                   {"options": {status.value: "" for status in ProjectStatus}}, {"surprise": "value"}):
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate(config().model_dump() | change)
    with pytest.raises(ValidationError):
        type(settings(tmp_path))(**(settings(tmp_path).model_dump() | {"projects": {"bad": config()}}))


async def test_validate_uses_stable_ids_and_actual_repository_installation(tmp_path, monkeypatch):
    api, client, lookup = adapter(tmp_path, monkeypatch, target())
    checked = await api.validate("owner/repo")
    assert checked.title == "Renamed board" and checked.status_name == "Renamed status"
    assert lookup.calls == [(("owner", "repo"), {})]
    assert client.calls[0][1] == {"project": "P", "field": "F"}
    assert "mutation" not in client.calls[0][0]


@pytest.mark.parametrize("bad", ["installation", "owner", "personal", "permission", "closed", "field", "options"])
async def test_invalid_destination_is_explicit(tmp_path, monkeypatch, bad):
    data = target()
    if bad == "owner":
        data["project"]["owner"]["login"] = "other"
    elif bad == "personal":
        data["project"]["owner"]["__typename"] = "User"
    elif bad == "permission":
        data["project"]["viewerCanUpdate"] = False
    elif bad == "closed":
        data["project"]["closed"] = True
    elif bad == "field":
        data["field"]["project"]["id"] = "OTHER"
    elif bad == "options":
        data["field"]["options"].pop()
    api, client, _ = adapter(tmp_path, monkeypatch, data, installation=99 if bad == "installation" else 42)
    with pytest.raises(GitHubAdapterError):
        await api.validate("owner/repo")
    assert len(client.calls) == (0 if bad == "installation" else 1)


async def test_issue_identity_is_read_and_validated(tmp_path, monkeypatch):
    good = {"repository": {"nameWithOwner": "owner/repo", "issue": {"id": "I", "number": 7}}}
    api, client, _ = adapter(tmp_path, monkeypatch, good, {"repository": None})
    assert await api.issue_id("owner/repo", 7) == "I"
    assert client.calls[0][1] == {"owner": "owner", "name": "repo", "number": 7}
    with pytest.raises(GitHubAdapterError):
        await api.issue_id("owner/repo", 7)


async def test_item_lookup_exposes_second_page_and_invisible_content(tmp_path, monkeypatch):
    api, client, _ = adapter(tmp_path, monkeypatch,
        {"node": {"id": "P", "items": page([item(None)], "next")}},
        {"node": {"id": "P", "items": page([item()])}})
    first = await api.items_page(None)
    assert not first.complete and first.next_cursor == "next"
    assert first.items[0].content_id is None
    second = await api.items_page(first.next_cursor)
    assert second.complete and second.items[0].content_id == "I"
    assert client.calls[1][1] == {"project": "P", "after": "next"}
    assert all("mutation" not in query for query, _ in client.calls)


async def test_membership_pages_explicitly_include_archived_cards(tmp_path, monkeypatch):
    # GitHub defaults to NOT_ARCHIVED; a complete page must cover both states before proving absence.
    api, client, _ = adapter(tmp_path, monkeypatch,
        {"node": {"id": "P", "items": page([item("active")], "next")}},
        {"node": {"id": "P", "items": page([item("archived", archived=True)])}})
    first = await api.items_page(None)
    second = await api.items_page(first.next_cursor)
    assert all("archivedStates: [ARCHIVED, NOT_ARCHIVED]" in query for query, _ in client.calls)
    assert second.complete and second.next_cursor is None
    assert second.items[0].content_id == "archived" and second.items[0].archived


@pytest.mark.parametrize("bad", ["partial", "cursor", "boolean", "item", "project"])
async def test_malformed_pages_never_establish_absence(tmp_path, monkeypatch, bad):
    data = {"node": {"id": "P", "items": page([item()])}}
    if bad == "partial":
        data["errors"] = [{"message": "secret-token"}]
    elif bad == "cursor":
        data["node"]["items"]["pageInfo"] = {"hasNextPage": True, "endCursor": "old"}
    elif bad == "boolean":
        data["node"]["items"]["pageInfo"]["hasNextPage"] = "false"
    elif bad == "item":
        data["node"]["items"]["nodes"] = [None]
    else:
        data["node"]["id"] = "wrong"
    api, _, _ = adapter(tmp_path, monkeypatch, data)
    with pytest.raises(GitHubAdapterError) as caught:
        await api.items_page("old")
    assert "secret-token" not in str(caught.value)


async def test_read_status_by_field_id_across_pages_and_report_archived(tmp_path, monkeypatch):
    first = item() | {"fieldValues": page([], "fields")}
    second = item(archived=True) | {"fieldValues": page([
        {"field": {"id": "F"}, "optionId": "done"}])}
    api, client, _ = adapter(tmp_path, monkeypatch, {"node": first}, {"node": second}, {"node": None})
    result = await api.read_item("ITEM")
    assert result.option_id == "done" and result.archived
    assert client.calls[1][1]["after"] == "fields"
    assert await api.read_item("ITEM") is None


async def test_add_and_set_are_separate_and_check_mutation_receipts(tmp_path, monkeypatch):
    api, client, _ = adapter(tmp_path, monkeypatch,
        {"addProjectV2ItemById": {"item": item()}},
        {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM"}}},
        {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "OTHER"}}})
    assert (await api.add_issue("I")).id == "ITEM"
    assert len(client.calls) == 1 and "updateProject" not in client.calls[0][0]
    await api.set_status("ITEM", ProjectStatus.DONE)
    assert client.calls[1][1] == {"project": "P", "item": "ITEM", "field": "F", "option": "done"}
    assert "singleSelectOptionId" in client.calls[1][0]
    with pytest.raises(GitHubAdapterError):
        await api.set_status("ITEM", ProjectStatus.DONE)


@pytest.mark.parametrize("bad", ["identity", "archived", "partial"])
async def test_add_does_not_accept_unverified_receipt(tmp_path, monkeypatch, bad):
    data = {"addProjectV2ItemById": {"item": item("OTHER" if bad == "identity" else "I",
                                                   archived=bad == "archived")}}
    if bad == "partial":
        data["errors"] = [{"message": "permission"}]
    api, client, _ = adapter(tmp_path, monkeypatch, data)
    with pytest.raises(GitHubAdapterError):
        await api.add_issue("I")
    assert len(client.calls) == 1


async def test_status_absence_requires_complete_field_traversal(tmp_path, monkeypatch):
    pages = [{"node": item() | {"fieldValues": page([], str(i))}} for i in range(10)]
    api, client, _ = adapter(tmp_path, monkeypatch, *pages)
    with pytest.raises(GitHubAdapterError):
        await api.read_item("ITEM")
    assert len(client.calls) == 10
    api, _, _ = adapter(tmp_path, monkeypatch, {"node": item() | {"fieldValues": page([])}})
    assert (await api.read_item("ITEM")).option_id is None


@pytest.mark.parametrize("operation", ["add", "set"])
@pytest.mark.parametrize("failure", ["partial", "lost", "limited"])
async def test_real_sdk_never_retries_or_accepts_partial_mutation(tmp_path, monkeypatch, operation, failure):
    app = GitHubApp(settings(tmp_path))
    calls = []

    def transport(request):
        calls.append(request)
        if failure == "lost":
            raise httpx.ReadTimeout("secret-token", request=request)
        if failure == "limited":
            return httpx.Response(403, json={"message": "API rate limit exceeded"},
                                  headers={"Retry-After": "600", "X-RateLimit-Remaining": "0"})
        return httpx.Response(200, json={"data": {"addProjectV2ItemById": {"item": item()}},
            "errors": [{"type": "FORBIDDEN", "message": "secret-token"}]})

    def installed(_, *, auto_retry):
        assert auto_retry is False
        return GitHub("test-token", auto_retry=False, async_transport=httpx.MockTransport(transport))

    monkeypatch.setattr(app, "_installation_client", installed)
    api = app.project_client(config())
    call = api.add_issue("I") if operation == "add" else api.set_status("ITEM", ProjectStatus.DONE)
    with pytest.raises(GitHubAdapterError) as caught:
        await asyncio.wait_for(call, 1)
    assert len(calls) == 1 and "secret-token" not in str(caught.value)
    assert caught.value.request_rejected is (failure == "limited")
    assert caught.value.kind is {"partial": GitHubErrorKind.PERMISSION,
        "lost": GitHubErrorKind.TRANSIENT, "limited": GitHubErrorKind.RATE_LIMITED}[failure]


def test_check_disabled_does_not_construct_github(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "get_serve_settings", lambda: settings(tmp_path))
    monkeypatch.setattr("app.github_client.GitHubApp", lambda _: pytest.fail("disabled must not authenticate"))
    result = CliRunner().invoke(cli.app, ["project", "check", "owner/repo"])
    assert result.exit_code == 0 and "disabled" in result.stdout


def test_check_only_validates_and_prints_target(tmp_path, monkeypatch):
    settings_value = settings(tmp_path)
    settings_value.projects = {"owner/repo": config()}
    api, client, lookup = adapter(tmp_path, monkeypatch, target())
    monkeypatch.setattr(cli, "get_serve_settings", lambda: settings_value)
    monkeypatch.setattr("app.github_client.GitHubApp", lambda _: SimpleNamespace(project_client=lambda _: api))
    result = CliRunner().invoke(cli.app, ["project", "check", "OWNER/REPO"])
    assert result.exit_code == 0 and "Renamed board" in result.stdout
    assert len(client.calls) == len(lookup.calls) == 1
    assert "mutation" not in client.calls[0][0]
