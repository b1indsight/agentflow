"""Incremental label requests must never replace user labels or hide repository errors."""

from types import SimpleNamespace

import pytest

from app.domain.github_models import GitHubAdapterError
from app.github.client import GitHubApp
from tests.test_github_client import AsyncEndpoint, request_failed, response, settings


async def test_add_label_requires_existing_definition_and_uses_incremental_api(tmp_path, monkeypatch):
    get = AsyncEndpoint(response(SimpleNamespace(name="flow:done")))
    add = AsyncEndpoint(response([SimpleNamespace(name="flow:done")]))
    github = GitHubApp(settings(tmp_path))
    client = SimpleNamespace(rest=SimpleNamespace(issues=SimpleNamespace(
        async_get_label=get, async_add_labels=add,
    )))
    monkeypatch.setattr(github, "_installation_client", lambda _, **kwargs: client)
    await github.add_label(42, "octo/repo", 7, "flow:done")
    assert get.calls == [(("octo", "repo", "flow:done"), {})]
    assert add.calls == [(("octo", "repo", 7), {"labels": ["flow:done"]})]

    async def missing(*args, **kwargs):
        raise request_failed(404)

    client.rest.issues.async_get_label = missing
    with pytest.raises(GitHubAdapterError, match="label"):
        await github.add_label(42, "octo/repo", 7, "missing")
    assert len(add.calls) == 1


@pytest.mark.parametrize("remaining", [[], ["Flow:In-Review"], None])
async def test_remove_label_404_requires_positive_target_lookup(tmp_path, monkeypatch, remaining):
    async def missing(*args, **kwargs):
        raise request_failed(404)

    async def listed(*args, **kwargs):
        if remaining is None:
            raise request_failed(404)
        return response([SimpleNamespace(name=label) for label in remaining])

    github = GitHubApp(settings(tmp_path))
    client = SimpleNamespace(rest=SimpleNamespace(issues=SimpleNamespace(
        async_remove_label=missing, async_list_labels_on_issue=listed,
    )))
    monkeypatch.setattr(github, "_installation_client", lambda _, **kwargs: client)
    # A concurrent removal is success; a hidden repo or a still-present label is not.
    if remaining == []:
        await github.remove_label(42, "octo/repo", 7, "flow:in-review")
    else:
        with pytest.raises(GitHubAdapterError):
            await github.remove_label(42, "octo/repo", 7, "flow:in-review")
