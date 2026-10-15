import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from starlette.testclient import TestClient

from app.contracts.conversation import ConversationFailure, ConversationPage
from app.controler import access
from app.controler.api import create_app
from app.telemetry.metrics import Metrics
from tests.access import AccessIssuer


@pytest.fixture
def runtime():
    issuer = AccessIssuer()
    return SimpleNamespace(
        settings=issuer.settings, access=issuer.auth, issuer=issuer,
        metrics=Metrics(), startup=lambda: None, shutdown=issuer.auth.close,
        conversations=SimpleNamespace(read=AsyncMock(return_value=ConversationPage(flow_id=42))),
    )


def test_conversation_authentication_parameters_and_response(runtime) -> None:
    with TestClient(create_app(runtime)) as client:
        path = "/api/flows/42/conversation"
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert runtime.conversations.read.await_count == 0
        headers = runtime.issuer.headers()
        response = client.get(path, params={"limit": 2}, headers=headers)
        assert response.status_code == 200
        assert response.json()["flow_id"] == 42
        runtime.conversations.read.assert_awaited_once_with(42, None, 2)
        assert response.headers["cache-control"] == "no-store"
        assert client.get(path, params={"limit": 501}, headers=headers).status_code == 422
        runtime.settings.access_owner_email = None
        assert client.get(path, headers=headers).status_code == 503


@pytest.mark.parametrize("claims,status", [
    ({"email": "other@example.com"}, 403),
    ({"email": None, "sub": "", "common_name": "service-token"}, 403),
    ({"type": "org"}, 403),
    ({"sub": ""}, 403),
    ({"exp": 0}, 401),
    ({"iat": int(time.time()) + 3600}, 401),
    ({"nbf": int(time.time()) + 3600}, 401),
    ({"iss": "https://attacker.example"}, 401),
    ({"aud": ["another-app"]}, 401),
    ({"exp": None}, 401),
])
def test_owner_access_rejects_invalid_identity_before_read(runtime, claims, status) -> None:
    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/flows/42/conversation", headers=runtime.issuer.headers(**claims))
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store"
        runtime.conversations.read.assert_not_awaited()


@pytest.mark.parametrize("kind", ["email-header", "cookie", "hs256", "bad-signature", "large", "duplicate"])
def test_owner_access_cannot_be_spoofed(runtime, kind) -> None:
    headers: dict[str, str] | list[tuple[str, str]] = {"Cf-Access-Authenticated-User-Email": "owner@example.com"}
    if kind == "cookie":
        headers = {"Cookie": f"CF_Authorization={runtime.issuer.token()}"}
    elif kind == "hs256":
        headers = {"Cf-Access-Jwt-Assertion": jwt.encode({"email": "owner@example.com"},
                   "a" * 32, algorithm="HS256", headers={"kid": "current"})}
    elif kind == "bad-signature":
        headers = AccessIssuer().headers()
    elif kind == "large":
        headers = {"Cf-Access-Jwt-Assertion": "x" * 20000}
    elif kind == "duplicate":
        headers = list(runtime.issuer.headers().items()) * 2
    with TestClient(create_app(runtime)) as client:
        assert client.get("/api/flows/42/conversation", headers=headers).status_code == 401
        runtime.conversations.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_key_rotation_and_unknown_kid_requests_are_bounded(runtime, monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(access, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    app = create_app(runtime)
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        path = "/api/flows/42/conversation"
        owner_headers = runtime.issuer.headers()
        responses = await asyncio.gather(*(client.get(path, headers=owner_headers) for _ in range(10)))
        assert all(response.status_code == 200 for response in responses)
        assert runtime.issuer.requests == 1
        runtime.issuer.kid = "rotated"
        rotated_headers = runtime.issuer.headers()
        for _ in range(5):
            assert (await client.get(path, headers=rotated_headers)).status_code == 401
        assert runtime.issuer.requests == 1
        clock[0] += 61
        responses = await asyncio.gather(*(client.get(path, headers=rotated_headers) for _ in range(10)))
        assert all(response.status_code == 200 for response in responses)
        assert runtime.issuer.requests == 2
        assert (await client.get(path, headers=owner_headers)).status_code == 401
    await runtime.access.close()


@pytest.mark.asyncio
async def test_expired_key_cache_fails_closed_during_outage(runtime, monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(access, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    app = create_app(runtime)
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        path, headers = "/api/flows/42/conversation", runtime.issuer.headers()
        assert (await client.get(path, headers=headers)).status_code == 200
        runtime.issuer.status = 500
        clock[0] += 3601
        runtime.conversations.read.reset_mock()
        for _ in range(5):
            assert (await client.get(path, headers=headers)).status_code == 503
        assert runtime.issuer.requests == 2
        runtime.conversations.read.assert_not_awaited()
        runtime.issuer.status = 200
        clock[0] += 61
        assert (await client.get(path, headers=headers)).status_code == 200
    await runtime.access.close()


@pytest.mark.parametrize("status,body", [
    (500, None), (302, None), (200, b"invalid"), (200, b'{"keys": []}'),
    (200, b"x" * 65537),
    (200, b'{"keys": [{"kid": "current", "kty": "RSA", "n": "invalid", "e": "AQAB"}]}'),
])
def test_unavailable_or_malformed_access_keys_do_not_allow_queries(runtime, status, body) -> None:
    runtime.issuer.status, runtime.issuer.body = status, body
    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/flows/42/conversation", headers=runtime.issuer.headers())
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        runtime.conversations.read.assert_not_awaited()


@pytest.mark.parametrize(("code", "status"), [
    ("flow_not_found", 404), ("worker_unavailable", 503), ("query_timeout", 504),
    ("cursor_invalid", 409), ("history_unreadable", 502), ("query_busy", 503),
])
def test_conversation_expected_errors(runtime, code, status) -> None:
    runtime.conversations.read.side_effect = ConversationFailure(code)
    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/flows/42/conversation", headers=runtime.issuer.headers())
        assert response.status_code == status
        assert response.json() == {"detail": code}


@pytest.mark.asyncio
async def test_http_disconnect_cancels_pending_query(runtime) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def read(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime.conversations.read = read
    app = create_app(runtime)
    app.state.runtime = runtime

    async def receive():
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    scope = {"type": "http", "method": "GET", "scheme": "http", "path": "/api/flows/42/conversation",
             "raw_path": b"/api/flows/42/conversation", "query_string": b"", "http_version": "1.1",
             "headers": [(b"cf-access-jwt-assertion", runtime.issuer.token().encode())], "server": ("test", 80)}
    await asyncio.wait_for(app(scope, receive, send), 2)
    assert cancelled.is_set()
    await runtime.access.close()
