"""Incremental label requests must never replace user labels or hide repository errors."""

from types import SimpleNamespace

import pytest

from app.github import GitHubApp, GitHubError, PullRequest
from tests.test_github_client import (
    AsyncEndpoint,
    install_client,
    request_failed,
    response,
    settings,
)


async def test_add_label_requires_existing_definition_and_uses_incremental_api(tmp_path, monkeypatch):
    get = AsyncEndpoint(response(SimpleNamespace(name="flow:done")))
    add = AsyncEndpoint(response([SimpleNamespace(name="flow:done")]))
    github = GitHubApp(settings(tmp_path))
    client = SimpleNamespace(rest=SimpleNamespace(issues=SimpleNamespace(
        async_get_label=get, async_add_labels=add,
    )))
    install_client(monkeypatch, lambda _, **kwargs: client)
    await PullRequest(github.installation(42).repository("octo/repo"), 7).add_label("flow:done")
    assert get.calls == [(("octo", "repo", "flow:done"), {})]
    assert add.calls == [(("octo", "repo", 7), {"labels": ["flow:done"]})]

    async def missing(*args, **kwargs):
        raise request_failed(404)

    client.rest.issues.async_get_label = missing
    with pytest.raises(GitHubError, match="label"):
        await PullRequest(github.installation(42).repository("octo/repo"), 7).add_label("missing")
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
    install_client(monkeypatch, lambda _, **kwargs: client)
    # A concurrent removal is success; a hidden repo or a still-present label is not.
    if remaining == []:
        await PullRequest(github.installation(42).repository("octo/repo"), 7).remove_label("flow:in-review")
    else:
        with pytest.raises(GitHubError):
            await PullRequest(github.installation(42).repository("octo/repo"), 7).remove_label("flow:in-review")
