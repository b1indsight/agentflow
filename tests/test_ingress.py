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

from app.ingress import create_app


class RecordingRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.handled = asyncio.Event()

    async def handle(self, event_type: str, delivery_id: str, payload: dict) -> None:
        self.calls.append((event_type, delivery_id, payload))
        self.handled.set()


class FakeContext:
    def __init__(self, secret: str = "webhook-secret") -> None:
        self.settings = SimpleNamespace(github_webhook_secret=secret)
        self.router = RecordingRouter()
        self.tasks: list[asyncio.Task[None]] = []

    def startup(self) -> None:
        pass

    def spawn_handler(self, coro: Coroutine[Any, Any, None]) -> None:
        self.tasks.append(asyncio.create_task(coro))

    async def shutdown(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks)


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
    client: httpx.AsyncClient, context: FakeContext
) -> None:
    payload = {"action": "labeled", "issue": {"number": 7}}
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(
        context.settings.github_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()

    response = await client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-1",
            "X-Hub-Signature-256": signature,
        },
    )

    assert response.status_code == 202
    await asyncio.wait_for(context.router.handled.wait(), timeout=1)
    assert context.router.calls == [("issues", "delivery-1", payload)]
