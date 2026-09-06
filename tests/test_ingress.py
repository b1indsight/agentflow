import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Coroutine
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import WebSocket

from app.control.api import WebhookRouter, create_app
from app.control.router import EventRouter
from app.models import Flow, FlowState, JobKind
from app.store import Store


class RecordingRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.handled = asyncio.Event()

    async def handle(self, event_type: str, delivery_id: str, payload: dict) -> None:
        self.calls.append((event_type, delivery_id, payload))
        self.handled.set()


class RecordingGateway:
    async def handle(self, socket: WebSocket) -> None:
        pass


class FakeContext:
    def __init__(self, router: WebhookRouter | None = None, settings=None) -> None:
        self.settings = settings or SimpleNamespace(github_webhook_secret="webhook-secret")
        self.router = router or RecordingRouter()
        self.gateway = RecordingGateway()
        self.tasks: list[asyncio.Task[None]] = []

    def startup(self) -> None:
        pass

    def spawn_handler(self, coro: Coroutine[Any, Any, None]) -> None:
        self.tasks.append(asyncio.create_task(coro))

    async def shutdown(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks)


class RoutingService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def on_trigger(self, repo: str, issue_number: int, installation_id: int | None) -> Flow:
        flow = self.store.create_flow(repo, issue_number, installation_id)
        self.store.enqueue_job(flow.id, JobKind.INITIAL)
        return flow

    def cancel(self, flow: Flow) -> None:
        flow.state = FlowState.CANCELLED
        self.store.update_flow(flow)

    def on_merged(self, flow: Flow) -> None:
        flow.state = FlowState.SUCCEEDED
        self.store.update_flow(flow)

    def notify_jobs(self) -> None:
        pass


@pytest.fixture
def context() -> FakeContext:
    return FakeContext()


@pytest_asyncio.fixture
async def client(context: FakeContext) -> AsyncIterator[httpx.AsyncClient]:
    application = create_app(lambda: context)
    transport = httpx.ASGITransport(app=application)
    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(transport=transport, base_url="http://test") as instance,
    ):
        yield instance


@pytest.mark.asyncio
async def test_health_and_invalid_signature(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    response = await client.post(
        "/webhook", content=b"{}", headers={"X-Hub-Signature-256": "bad"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_valid_signature_dispatches_webhook(
    store: Store, monkeypatch
) -> None:
    settings = SimpleNamespace(
        allowlist=["alice"],
        bot_login="flowtool[bot]",
        github_webhook_secret="webhook-secret",
        host="test",
        port=0,
        trigger_label="flow",
        settle_seconds=75.0,
        submit_grace_seconds=15.0,
    )
    context = FakeContext(EventRouter(store, RoutingService(store), settings), settings)
    application = create_app(lambda: context)
    transport = httpx.ASGITransport(app=application)
    payload = {
        "action": "labeled",
        "label": {"name": "flow"},
        "repository": {"full_name": "owner/repo"},
        "issue": {"number": 7},
        "sender": {"login": "alice"},
        "installation": {"id": 42},
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(
        context.settings.github_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()

    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        response = await client.post(
            "/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "delivery-1",
                "X-Hub-Signature-256": signature,
            },
        )
        await asyncio.gather(*context.tasks)

    assert response.status_code == 202
    flow = store.get_active_flow("owner/repo", 7)
    assert flow is not None
    assert flow.state is FlowState.ACTIVE
    store.worker_connected("worker-1", protocol_version=1)
    assert store.assign_job("worker-1", "assignment-1") is not None
