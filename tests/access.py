"""Locally signed Access assertions and an in-memory public-key endpoint."""

import json
import time

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from app.controler.access import OwnerAccess
from app.controler.config import ServerSettings


class AccessIssuer:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "current"
        self.requests = 0
        self.status = 200
        self.body: bytes | None = None
        self.settings = ServerSettings(
            database_url="postgresql://localhost/test", worker_tokens={"worker": SecretStr("secret")},
            access_issuer="https://test.cloudflareaccess.com", access_audience="test-app",
            access_owner_email="owner@example.com",
        )
        self.auth = OwnerAccess(self.settings, client=httpx.AsyncClient(
            transport=httpx.MockTransport(self.respond),
        ))

    def respond(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://test.cloudflareaccess.com/cdn-cgi/access/certs"
        self.requests += 1
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid=self.kid, alg="RS256", use="sig")
        return httpx.Response(self.status, content=self.body if self.body is not None else
                              json.dumps({"keys": [jwk]}).encode())

    def token(self, **changes) -> str:
        now = int(time.time())
        claims = {"iss": self.settings.access_issuer, "aud": [self.settings.access_audience],
                  "iat": now, "exp": now + 300, "sub": "owner-id", "type": "app",
                  "email": "owner@example.com"}
        claims.update(changes)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})

    def headers(self, **changes) -> dict[str, str]:
        return {"Cf-Access-Jwt-Assertion": self.token(**changes)}
