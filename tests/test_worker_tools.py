import httpx
import pytest

from app.domain.delivery_models import OpenedDelivery
from app.domain.models import VcsExecutionContext, VcsKind, VcsOutcome
from app.worker.transport import mcp


async def test_worker_mcp_http_open_pr_and_lifecycle(tmp_path, monkeypatch):
    outcome = VcsOutcome(backend=VcsKind.GIT, branch="flow/test", commit_sha="abc", remote_sha="abc")

    async def verify(context):
        return outcome

    class Caller:
        async def call_tool(self, name, arguments, *, call_id, vcs_outcome):
            assert name == "flow.open_pr" and vcs_outcome == outcome and call_id
            return OpenedDelivery(pr_number=17, url="https://github.com/o/r/pull/17",
                                  head_sha="abc", delivery_version=1)

    monkeypatch.setattr(mcp, "verify_pushed_outcome", verify)
    context = VcsExecutionContext(tmp_path, "flow/test", "origin", None, None, None, None, None)
    async with mcp.WorkerMCP(Caller(), context) as server, httpx.AsyncClient(trust_env=False) as client:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert (await client.post(server.url, json=request)).status_code == 401
        headers = {"Authorization": f"Bearer {server.token.get_secret_value()}"}
        assert (await client.post(server.url, json=request,
                                  headers={**headers, "Origin": "https://other.example"})).status_code == 403
        initialized = await client.post(server.url, headers=headers, json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        assert initialized.json()["result"]["protocolVersion"] == "2025-06-18"
        notified = await client.post(server.url, headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        assert notified.status_code == 202 and not notified.content
        listed = (await client.post(server.url, headers=headers, json=request)).json()
        assert [item["name"] for item in listed["result"]["tools"]] == ["open_pr"]
        called = (await client.post(server.url, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "open_pr", "arguments": {"title": "Implement", "body": "Done"}},
        })).json()
        assert called["result"]["structuredContent"]["pr_number"] == 17
        assert server.last_result.pr_number == 17
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(server.url, json=request)


async def test_cancelled_invocation_closes_mcp_without_replacing_worker_signal_handler(tmp_path):
    import asyncio
    import signal
    from types import SimpleNamespace

    previous = signal.getsignal(signal.SIGTERM)
    context = VcsExecutionContext(tmp_path, "flow/test", "origin", None, None, None, None, None)
    server = mcp.WorkerMCP(SimpleNamespace(), context)
    started = asyncio.Event()

    async def invocation():
        async with server:
            assert signal.getsignal(signal.SIGTERM) == previous
            started.set()
            await asyncio.Future()

    running = asyncio.create_task(invocation())
    await asyncio.wait_for(started.wait(), 5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert signal.getsignal(signal.SIGTERM) == previous
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(server.url)
