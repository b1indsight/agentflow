import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.control.api import create_app
from app.control.router import EventRouter
from app.control.service import ControlService
from app.control.webhooks import WebhookConsumer
from app.models import Flow, FlowState, JobKind
from app.observability import Metrics
from app.protocol import PROTOCOL_VERSION
from app.store import Store
from app.webhook_store import WebhookStore


class RecordingGateway:
    async def handle(self, socket) -> None:
        pass


class FakeContext:
    def __init__(self, router: EventRouter | None = None, settings=None) -> None:
        self.settings = settings or SimpleNamespace(github_webhook_secret="webhook-secret")
        self.settings.webhook_max_body_bytes = 25 * 1024 * 1024
        self.settings.metrics_token = None
        self.settings.host, self.settings.port = "test", 0
        self.metrics = Metrics()
        self.router = router
        self.gateway = RecordingGateway()
        self.tasks: list[asyncio.Task[None]] = []

    async def receive_webhook(self, event_type: str, delivery_id: str, payload: dict, digest: str) -> None:
        assert isinstance(self.router, EventRouter)
        store = self.router.store
        WebhookStore(store).receive(delivery_id, event_type, payload, digest)

        async def consume() -> None:
            WebhookConsumer(store, self.settings, ControlService(store)).sweep()

        self.tasks.append(asyncio.create_task(consume()))

    async def ready(self) -> bool:
        return True

    def startup(self) -> None:
        pass

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

    def on_merged(self, flow: Flow, delivery_id: str, payload: dict) -> None:
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
    store.worker_connected("worker-1", protocol_version=PROTOCOL_VERSION)
    assert store.assign_job("worker-1", "assignment-1") is not None


def signed_headers(body: bytes) -> dict[str, str]:
    return {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "receipt-1",
            "X-Hub-Signature-256": "sha256=" + hmac.new(
                b"webhook-secret", body, hashlib.sha256).hexdigest()}


@pytest.mark.asyncio
async def test_receipt_commit_failure_is_not_acknowledged(client, context, monkeypatch):
    from psycopg import OperationalError

    def unavailable(*args):
        raise OperationalError("secret database URL")

    monkeypatch.setattr(context, "receive_webhook", unavailable)
    response = await client.post("/webhook", content=b"{}", headers=signed_headers(b"{}"))
    assert response.status_code == 503
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_invalid_identity_json_and_conflict(client, context, monkeypatch):
    from app.webhook_store import DeliveryConflict

    for body in [b"[]", b"{", b"null", b'{"value":NaN}']:
        assert (await client.post("/webhook", content=body, headers=signed_headers(body))).status_code == 400
    headers = signed_headers(b"{}")
    del headers["X-GitHub-Delivery"]
    assert (await client.post("/webhook", content=b"{}", headers=headers)).status_code == 400

    def conflict(*args):
        raise DeliveryConflict()

    monkeypatch.setattr(context, "receive_webhook", conflict)
    assert (await client.post("/webhook", content=b"{}", headers=signed_headers(b"{}"))).status_code == 409


@pytest.mark.asyncio
async def test_streamed_request_body_limit(client, context):
    context.settings.webhook_max_body_bytes = 3

    async def chunks():
        yield b"{"
        yield b'"large":1}'

    assert (await client.post("/webhook", content=chunks())).status_code == 413
