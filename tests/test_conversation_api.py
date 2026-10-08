import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

from app.contracts.conversation import ConversationFailure, ConversationPage
from app.control.api import create_app
from app.telemetry.metrics import Metrics


@pytest.fixture
def context():
    return SimpleNamespace(
        settings=SimpleNamespace(host="test", port=0, control_api_token=SecretStr("read-token")),
        metrics=Metrics(), startup=lambda: None, shutdown=AsyncMock(),
        conversations=SimpleNamespace(read=AsyncMock(return_value=ConversationPage(flow_id=42))),
    )


def test_conversation_authentication_parameters_and_response(context) -> None:
    with TestClient(create_app(lambda: context)) as client:
        path = "/api/flows/42/conversation"
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert context.conversations.read.await_count == 0
        headers = {"Authorization": "Bearer read-token"}
        response = client.get(path, params={"limit": 2}, headers=headers)
        assert response.status_code == 200
        assert response.json()["flow_id"] == 42
        context.conversations.read.assert_awaited_once_with(42, None, 2)
        assert response.headers["cache-control"] == "no-store"
        assert client.get(path, params={"limit": 501}, headers=headers).status_code == 422
        context.settings.control_api_token = None
        assert client.get(path, headers=headers).status_code == 503


@pytest.mark.parametrize(("code", "status"), [
    ("flow_not_found", 404), ("worker_unavailable", 503), ("query_timeout", 504),
    ("cursor_invalid", 409), ("history_unreadable", 502), ("query_busy", 503),
])
def test_conversation_expected_errors(context, code, status) -> None:
    context.conversations.read.side_effect = ConversationFailure(code)
    with TestClient(create_app(lambda: context)) as client:
        response = client.get("/api/flows/42/conversation", headers={"Authorization": "Bearer read-token"})
        assert response.status_code == status
        assert response.json() == {"detail": code}


@pytest.mark.asyncio
async def test_http_disconnect_cancels_pending_query(context) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def read(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    context.conversations.read = read
    app = create_app(lambda: context)
    app.state.ctx = context

    async def receive():
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    scope = {"type": "http", "method": "GET", "scheme": "http", "path": "/api/flows/42/conversation",
             "raw_path": b"/api/flows/42/conversation", "query_string": b"", "http_version": "1.1",
             "headers": [(b"authorization", b"Bearer read-token")], "server": ("test", 80)}
    await asyncio.wait_for(app(scope, receive, send), 2)
    assert cancelled.is_set()
