"""Transient ref failures cannot depend on a model deciding to repeat flow.open_pr."""

from typing import cast

import httpx
import pytest
from githubkit import GitHub as SDK

from app.control.tools import FlowToolService
from app.github_client import GitHubApp
from app.ports import GitHubClient
from app.store import Store
from tests.test_flow_tools import ToolGitHub, ToolStore, request
from tests.test_github_client import settings


@pytest.mark.parametrize("status", [503, 429])
async def test_open_pr_ref_recovers_within_the_same_tool_call(tmp_path, monkeypatch, status):
    calls, sleeps = [], []

    def transport(incoming):
        calls.append(incoming)
        if len(calls) == 1:
            return httpx.Response(status, json={"message": "temporarily unavailable"},
                                  headers={"Retry-After": "2"})
        return httpx.Response(200, json={
            "ref": "refs/heads/flow/3", "node_id": "REF", "url": "https://api.github.com/ref",
            "object": {"type": "commit", "sha": "a" * 40, "url": "https://api.github.com/commit"},
        })

    async def sleep(seconds):
        sleeps.append(seconds)

    app = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(app, "_installation_client", lambda _, **options: SDK(
        "test-token", async_transport=httpx.MockTransport(transport), **options,
    ))
    monkeypatch.setattr("githubkit.core.anyio.sleep", sleep)
    memory, github = ToolStore(), ToolGitHub()
    monkeypatch.setattr(github, "get_ref", app.get_ref)
    service = FlowToolService(cast(Store, memory), cast(GitHubClient, github))
    invocation = request().model_copy(update={"commit_sha": "a" * 40, "remote_sha": "a" * 40})
    response = await service.execute(invocation, worker_id="worker-a")
    assert response.error_kind is None and response.result is not None
    assert len(calls) == 2 and len(sleeps) == 1
    # Retrying the tool delivery after recovery uses its existing receipt, with no new network read.
    assert await service.execute(invocation, worker_id="worker-a") == response
    assert len(calls) == 2
