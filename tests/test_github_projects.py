"""Projects contracts: IDs, complete page facts, and single-attempt writes."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from githubkit import GitHub
from githubkit_schemas.latest.models import SimpleUser
from pydantic import ValidationError
from typer.testing import CliRunner

from app import cli
from app.control import config as control_config
from app.domain.project_models import ProjectStatus
from app.github import (
    GitHubApp,
    GitHubError,
    GitHubErrorKind,
    Issue,
    ProjectConfig,
    ProjectItem,
    ProjectItemsPage,
)
from tests.test_github_client import AsyncEndpoint, app_client, install_client, response, settings


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
        id=installation, account=SimpleUser.model_construct(type="Organization", login="owner"),
    )))

    def installed(number, *, auto_retry):
        assert number == 42 and auto_retry is False
        return client

    def app_sdk(*, auto_retry):
        assert auto_retry is False
        return SimpleNamespace(rest=SimpleNamespace(apps=SimpleNamespace(async_get_repo_installation=lookup)))

    install_client(monkeypatch, installed)
    app_client(monkeypatch, app_sdk)
    return app.installation(42).project(config()), client, lookup


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
    checked = await api.validate(api.installation.repository("owner/repo"))
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
    with pytest.raises(GitHubError):
        await api.validate(api.installation.repository("owner/repo"))
    assert len(client.calls) == (0 if bad == "installation" else 1)


async def test_issue_identity_is_read_and_validated(tmp_path, monkeypatch):
    good = {"repository": {"nameWithOwner": "owner/repo", "issue": {"id": "I", "number": 7}}}
    api, client, _ = adapter(tmp_path, monkeypatch, good, {"repository": None})
    assert await Issue(api.installation.repository("owner/repo"), 7).fetch_content_id() == "I"
    assert client.calls[0][1] == {"owner": "owner", "name": "repo", "number": 7}
    with pytest.raises(GitHubError):
        await Issue(api.installation.repository("owner/repo"), 7).fetch_content_id()


async def test_membership_pages_cover_invisible_and_archived_items(tmp_path, monkeypatch):
    # Proving absence requires every page, including hidden content and archived cards.
    api, client, _ = adapter(tmp_path, monkeypatch,
        {"node": {"id": "P", "items": page([item("active"), item(None)], "next")}},
        {"node": {"id": "P", "items": page([item("archived", archived=True)])}})
    first = await api.list_items_page(None)
    assert not first.complete and first.next_cursor == "next"
    assert first.items[1].content_id is None
    second = await api.list_items_page(first.next_cursor)
    assert second.complete and second.next_cursor is None
    assert second.items[0].content_id == "archived" and second.items[0].archived
    assert client.calls[1][1] == {"project": "P", "after": "next"}
    assert all("archivedStates: [ARCHIVED, NOT_ARCHIVED]" in query and "mutation" not in query
               for query, _ in client.calls)


async def test_membership_item_can_write_and_round_trip_without_an_extra_read(tmp_path, monkeypatch):
    # A listed card is the resource itself; its unqueried status must stay unknown.
    api, client, _ = adapter(tmp_path, monkeypatch,
        {"node": {"id": "P", "items": page([item()])}},
        {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM"}}})
    membership = await api.list_items_page(None)
    listed = membership.items[0]
    assert listed.project is api
    assert listed.model_dump() == {"id": "ITEM", "content_id": "I", "archived": False}
    with pytest.raises(AttributeError):
        _ = listed.option_id
    with pytest.raises(ValidationError):
        listed.archived = True
    updated = await listed.set_status(ProjectStatus.DONE)
    assert updated.option_id == "done" and updated is not listed
    assert len(client.calls) == 2 and "updateProjectV2ItemFieldValue" in client.calls[1][0]

    # Standard nested JSON restores the same model without serializing its parent.
    restored = ProjectItemsPage.model_validate_json(membership.model_dump_json())
    assert restored.model_dump() == membership.model_dump()
    assert isinstance(restored.items, tuple)
    with pytest.raises(AttributeError):
        _ = restored.items[0].option_id
    with pytest.raises(ValueError, match="not bound"):
        await restored.items[0].set_status(ProjectStatus.DONE)
    assert len(client.calls) == 2


def test_restored_item_keeps_known_null_separate_from_unqueried_facts():
    # Restoring a saved observation must not turn known absence into an unknown value.
    observed = ProjectItem(id="ITEM", content_id=None, archived=False, option_id=None)
    restored = ProjectItem.model_validate_json(observed.model_dump_json())
    assert restored.model_dump() == {"id": "ITEM", "content_id": None, "archived": False, "option_id": None}
    assert restored.content_id is None and restored.option_id is None
    with pytest.raises(ValueError, match="not bound"):
        _ = restored.project


@pytest.mark.parametrize("bad", ["cursor", "item"])
async def test_malformed_pages_never_establish_absence(tmp_path, monkeypatch, bad):
    data = {"node": {"id": "P", "items": page([item()])}}
    if bad == "cursor":
        data["node"]["items"]["pageInfo"] = {"hasNextPage": True, "endCursor": "old"}
    else:
        data["node"]["items"]["nodes"] = [None]
    api, _, _ = adapter(tmp_path, monkeypatch, data)
    with pytest.raises(GitHubError) as caught:
        await api.list_items_page("old")
    assert "secret-token" not in str(caught.value)


async def test_read_status_by_field_id_across_pages_and_report_archived(tmp_path, monkeypatch):
    first = item() | {"fieldValues": page([], "fields")}
    second = item(archived=True) | {"fieldValues": page([
        {"field": {"id": "F"}, "optionId": "done"}])}
    api, client, _ = adapter(tmp_path, monkeypatch, {"node": first}, {"node": second}, {"node": None})
    result = await api.fetch_item("ITEM")
    assert result.option_id == "done" and result.archived
    assert client.calls[1][1]["after"] == "fields"
    assert await api.fetch_item("ITEM") is None


async def test_add_and_set_are_separate_and_check_mutation_receipts(tmp_path, monkeypatch):
    api, client, _ = adapter(tmp_path, monkeypatch,
        {"addProjectV2ItemById": {"item": item()}},
        {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM"}}})
    added = await api.add_issue("I")
    assert added.id == "ITEM"
    assert "option_id" not in added.model_dump(exclude_unset=True)
    assert len(client.calls) == 1 and "updateProject" not in client.calls[0][0]
    before = ProjectItem(api, "ITEM", content_id="I", archived=False, option_id="todo")
    after = await before.set_status(ProjectStatus.DONE)
    assert before.option_id == "todo" and after.option_id == "done"
    assert after is not before
    assert after.model_dump(mode="json", exclude_unset=True) == {
        "id": "ITEM", "content_id": "I", "archived": False, "option_id": "done",
    }
    assert client.calls[1][1] == {"project": "P", "item": "ITEM", "field": "F", "option": "done"}
    assert "singleSelectOptionId" in client.calls[1][0]


async def test_add_does_not_accept_unverified_receipt(tmp_path, monkeypatch):
    data = {"addProjectV2ItemById": {"item": item("I", archived=True)}}
    api, client, _ = adapter(tmp_path, monkeypatch, data)
    with pytest.raises(GitHubError):
        await api.add_issue("I")
    assert len(client.calls) == 1


async def test_status_absence_requires_complete_field_traversal(tmp_path, monkeypatch):
    pages = [{"node": item() | {"fieldValues": page([], str(i))}} for i in range(10)]
    api, client, _ = adapter(tmp_path, monkeypatch, *pages)
    with pytest.raises(GitHubError):
        await api.fetch_item("ITEM")
    assert len(client.calls) == 10
    api, _, _ = adapter(tmp_path, monkeypatch, {"node": item() | {"fieldValues": page([])}})
    assert (await api.fetch_item("ITEM")).option_id is None


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

    install_client(monkeypatch, installed)
    api = app.installation(42).project(config())
    call = api.add_issue("I") if operation == "add" else ProjectItem(api, "ITEM").set_status(ProjectStatus.DONE)
    with pytest.raises(GitHubError) as caught:
        await asyncio.wait_for(call, 1)
    assert len(calls) == 1 and "secret-token" not in str(caught.value)
    # A mutation can return data and GraphQL errors together; its diagnostic 403
    # must not authorize a retry like an actual HTTP rejection does.
    assert caught.value.definitely_rejected is (failure == "limited")
    assert caught.value.kind is {"partial": GitHubErrorKind.PERMISSION,
        "lost": GitHubErrorKind.TRANSIENT, "limited": GitHubErrorKind.RATE_LIMITED}[failure]


@pytest.fixture
def serve_config(tmp_path, monkeypatch):
    monkeypatch.setitem(control_config.__dict__, "settings", settings(tmp_path))
    return control_config


def test_check_disabled_does_not_construct_github(serve_config, monkeypatch):
    monkeypatch.setattr("app.github.GitHubApp", lambda _: pytest.fail("disabled must not authenticate"))
    result = CliRunner().invoke(cli.app, ["project", "check", "owner/repo"])
    assert result.exit_code == 0 and "disabled" in result.stdout


def test_check_only_validates_and_prints_target(tmp_path, monkeypatch, serve_config):
    settings_value = serve_config.settings
    settings_value.projects = {"owner/repo": config()}
    api, client, lookup = adapter(tmp_path, monkeypatch, target())
    monkeypatch.setattr("app.github.GitHubApp", lambda _: api.installation.app)
    result = CliRunner().invoke(cli.app, ["project", "check", "OWNER/REPO"])
    assert result.exit_code == 0 and "Renamed board" in result.stdout
    assert len(client.calls) == len(lookup.calls) == 1
    assert "mutation" not in client.calls[0][0]
