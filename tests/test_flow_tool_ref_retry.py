"""Transient ref failures cannot depend on a model deciding to repeat flow.open_pr."""

from uuid import uuid4

import httpx
import pytest
from githubkit import GitHub as SDK

from app.controler.workflow.tools import FlowToolService
from app.github import GitHubApp, Installation, Repository
from tests.github import NATIVE_REPOSITORY_FETCH_REF
from tests.test_github_client import settings


@pytest.mark.parametrize("status", [503, 429])
async def test_open_pr_ref_recovers_within_the_same_tool_call(
    store, prepared_initial, tmp_path, monkeypatch, status,
):
    flow, _, _, request, _ = prepared_initial
    calls, sleeps = [], []

    def transport(incoming):
        assert incoming.method == "GET"
        assert incoming.url.path == f"/repos/{flow.repo}/git/ref/heads/{request.arguments.branch}"
        calls.append(incoming)
        if len(calls) == 1:
            return httpx.Response(status, json={"message": "temporarily unavailable"},
                                  headers={"Retry-After": "2"})
        return httpx.Response(200, json={
            "ref": f"refs/heads/{request.arguments.branch}",
            "node_id": "REF", "url": "https://api.github.com/ref",
            "object": {"type": "commit", "sha": "a" * 40, "url": "https://api.github.com/commit"},
        })

    async def sleep(seconds):
        sleeps.append(seconds)

    app = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(Installation, "_client", lambda self, **options: SDK(
        "test-token", async_transport=httpx.MockTransport(transport), **options,
    ))
    monkeypatch.setattr("githubkit.core.anyio.sleep", sleep)
    monkeypatch.setattr(Repository, "fetch_ref", NATIVE_REPOSITORY_FETCH_REF)
    service = FlowToolService(app, database=store.database)
    invocation = request.model_copy(
        update={
            "arguments": request.arguments.model_copy(
                update={"call_id": uuid4().hex, "commit_sha": "a" * 40, "remote_sha": "a" * 40},
            )
        }
    )
    response = await service.execute(invocation, worker_id="worker-a", connection_epoch="epoch")
    assert response.error_kind is None and response.result is not None
    assert len(calls) == 2 and len(sleeps) == 1
    assert store.deliveries.get_delivery(flow.id).head_sha == "a" * 40
    assert store.deliveries.get_tool_call(invocation.arguments.call_id).response == response.result
    # Retrying the tool delivery after recovery uses its existing receipt, with no new network read.
    replay = await service.execute(invocation, worker_id="worker-a", connection_epoch="epoch")
    assert replay.result == response.result
    assert replay.reply_to == response.reply_to == invocation.message_id
    assert replay.message_id != response.message_id
    assert len(calls) == 2
