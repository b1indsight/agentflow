import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from unittest.mock import Mock

import httpx
import pytest
import pytest_asyncio
from psycopg import OperationalError
from pydantic import SecretStr

from app.contracts.protocol import PROTOCOL_VERSION
from app.controler.api import create_app
from app.controler.config import ServerSettings
from app.controler.events.router import EventRouter
from app.controler.events.webhooks import WebhookConsumer
from app.controler.runtime import ServerRuntime
from app.controler.workers.conversation import ConversationService
from app.controler.workers.gateway import WorkerGateway
from app.controler.workflow.service import ControlService
from app.domain.models import Flow, FlowState, JobKind
from app.persistence.store import Store
from app.telemetry.metrics import Metrics


class WebhookRuntime(ServerRuntime):
    def __init__(self, router: EventRouter | None = None, settings: ServerSettings | None = None, store: Store | None = None) -> None:
        self.settings = settings or ServerSettings.model_construct(
            database_url="postgresql://localhost/test",
            worker_tokens={"worker-1": SecretStr("worker-secret")},
            github_webhook_secret="webhook-secret",
        )
        self.settings.webhook_max_body_bytes = 25 * 1024 * 1024
        self.settings.metrics_token = None
        self.settings.host, self.settings.port = "test", 0
        self.metrics = Metrics()
        self.router = router
        self.gateway = Mock(spec=WorkerGateway)
        self.conversations = Mock(spec=ConversationService)
        self.tasks: list[asyncio.Task[None]] = []
        self.webhooks = Mock(spec=WebhookConsumer)
        if store is not None:
            self.webhooks = WebhookConsumer(
                self.settings,
                ControlService(
                    database=store.database,
                    deliveries=store.deliveries,
                    flows=store.flows,
                    jobs=store.jobs,
                    result_store=store.results,
                    reviews=store.reviews,
                    stops=store.stops,
                    webhook_store=store.webhooks,
                    workers=store.workers,
                ),
                database=store.database,
                deliveries=store.deliveries,
                flows=store.flows,
                jobs=store.jobs,
                reconcile_store=store.reconcile,
                result_store=store.results,
                reviews=store.reviews,
                stops=store.stops,
                webhook_store=store.webhooks,
                workers=store.workers,
            )

    async def receive_webhook(self, event_type: str, delivery_id: str, payload: dict, digest: str) -> None:
        assert isinstance(self.router, EventRouter)
        self.webhooks.webhook_store.receive(delivery_id, event_type, payload, digest)

        async def consume() -> None:
            self.webhooks.sweep()

        self.tasks.append(asyncio.create_task(consume()))

    async def ready(self) -> bool:
        return True

    def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks)


class RoutingService(ControlService):
    def __init__(self, store: Store) -> None:
        self.flows = store.flows
        self.jobs = store.jobs

    def on_trigger(self, repo: str, issue_number: int, installation_id: int | None) -> Flow:
        flow = self.flows.create_flow(repo, issue_number, installation_id)
        self.jobs.enqueue_job(flow.id, JobKind.INITIAL)
        return flow

    def cancel(self, flow: Flow) -> None:
        flow.state = FlowState.CANCELLED
        self.flows.update_flow(flow)

    def on_merged(self, flow: Flow, delivery_id: str, payload: dict) -> None:
        flow.state = FlowState.SUCCEEDED
        self.flows.update_flow(flow)

    def notify_jobs(self) -> None:
        pass


@pytest.fixture
def runtime() -> WebhookRuntime:
    return WebhookRuntime()


@pytest_asyncio.fixture
async def client(runtime: WebhookRuntime) -> AsyncIterator[httpx.AsyncClient]:
    application = create_app(runtime)
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
    settings = ServerSettings.model_construct(
        database_url="postgresql://localhost/test",
        worker_tokens={"worker-1": SecretStr("worker-secret")},
        allowlist=["alice"],
        bot_login="flowtool[bot]",
        github_webhook_secret="webhook-secret",
        host="test",
        port=0,
        trigger_label="flow",
        settle_seconds=75.0,
        submit_grace_seconds=15.0,
    )
    runtime = WebhookRuntime(
        EventRouter(
            RoutingService(store),
            settings,
            database=store.database,
            flows=store.flows,
            reconcile_store=store.reconcile,
            reviews=store.reviews,
            webhook_store=store.webhooks,
        ),
        settings,
        store,
    )
    application = create_app(runtime)
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
        runtime.settings.github_webhook_secret.encode(), body, hashlib.sha256
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
        await asyncio.gather(*runtime.tasks)

    assert response.status_code == 202
    flow = store.flows.get_active_flow("owner/repo", 7)
    assert flow is not None
    assert flow.state is FlowState.ACTIVE
    store.workers.worker_connected("worker-1", protocol_version=PROTOCOL_VERSION)
    assert store.workers.assign_job("worker-1", "assignment-1") is not None


def signed_headers(body: bytes) -> dict[str, str]:
    return {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "receipt-1",
            "X-Hub-Signature-256": "sha256=" + hmac.new(
                b"webhook-secret", body, hashlib.sha256).hexdigest()}


@pytest.mark.asyncio
async def test_receipt_commit_failure_is_not_acknowledged(client, runtime, monkeypatch):
    def unavailable(*args):
        raise OperationalError("secret database URL")

    monkeypatch.setattr(runtime, "receive_webhook", unavailable)
    response = await client.post("/webhook", content=b"{}", headers=signed_headers(b"{}"))
    assert response.status_code == 503
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_missing_delivery_identity_is_rejected(client):
    headers = signed_headers(b"{}")
    del headers["X-GitHub-Delivery"]
    assert (await client.post("/webhook", content=b"{}", headers=headers)).status_code == 400



@pytest.mark.asyncio
async def test_streamed_request_body_limit(client, runtime):
    runtime.settings.webhook_max_body_bytes = 3

    async def chunks():
        yield b"{"
        yield b'"large":1}'

    assert (await client.post("/webhook", content=chunks())).status_code == 413
