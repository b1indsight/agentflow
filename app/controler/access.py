"""Single-owner Access authentication; no credentials or sessions are persisted."""

from __future__ import annotations

import asyncio
import time

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import HTTPException
from pydantic import BaseModel, Field, JsonValue, ValidationError

from app.controler.config import ServerSettings


class _PublicKeys(BaseModel):
    keys: list[dict[str, JsonValue]] = Field(min_length=1, max_length=16)


class OwnerAccess:
    """Verify signed application assertions and authorize the configured owner.

    Cache public keys for one hour. Serialize refreshes and rate-limit them to once
    per minute, including failures and unknown key IDs, to bound unauthenticated I/O.
    Expired keys are never used if refresh fails. MFA is enforced by Access policies.
    """

    def __init__(self, settings: ServerSettings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client if client is not None else httpx.AsyncClient(
            timeout=5.0, follow_redirects=False, trust_env=False,
        )
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires_at = 0.0
        self._refresh_after = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def authenticate(self, token: str | None) -> None:
        """Reject before business I/O; only the signed assertion header is accepted."""
        headers = {"Cache-Control": "no-store"}
        settings = self.settings
        if not (settings.access_issuer and settings.access_audience and settings.access_owner_email):
            raise HTTPException(503, "conversation_api_disabled", headers=headers)
        try:
            if not token or len(token) > 16384:
                raise jwt.InvalidTokenError()
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (header.get("alg") != "RS256" or not isinstance(kid, str)
                    or not 1 <= len(kid) <= 256):
                raise jwt.InvalidTokenError()
            key = await self._key(kid)
            claims = jwt.decode(
                token, key, algorithms=["RS256"], issuer=settings.access_issuer,
                audience=settings.access_audience,
                options={"require": ["iss", "aud", "exp", "iat", "sub", "type"]},
            )
        except (jwt.InvalidTokenError, TypeError, ValueError, OverflowError) as exc:
            raise HTTPException(401, "unauthorized", headers=headers) from exc
        email = claims.get("email")
        if (claims.get("type") != "app" or not claims.get("sub")
                or not isinstance(email, str)
                or email.casefold() != settings.access_owner_email.casefold()
                or "common_name" in claims):
            raise HTTPException(403, "forbidden", headers=headers)

    async def _key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            now = time.monotonic()
            if now >= self._expires_at or kid not in self._keys:
                if now >= self._refresh_after:
                    self._refresh_after = now + 60
                    try:
                        # Bound the entire response, not just each socket read timeout.
                        async with asyncio.timeout(5):
                            keys = await self._fetch_keys()
                    except (httpx.HTTPError, TimeoutError, ValidationError,
                            jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
                        # A failed rotation probe must not invalidate still-fresh known keys.
                        raise HTTPException(503, "access_keys_unavailable",
                                            headers={"Cache-Control": "no-store"}) from exc
                    self._keys = keys
                    self._expires_at = time.monotonic() + 3600
                if time.monotonic() >= self._expires_at:
                    raise HTTPException(503, "access_keys_unavailable",
                                        headers={"Cache-Control": "no-store"})
            key = self._keys.get(kid)
            if key is None:
                raise jwt.InvalidTokenError()
            return key

    async def _fetch_keys(self) -> dict[str, jwt.PyJWK]:
        """Read only the configured team's bounded JWKS; never follow token URLs."""
        async with self.client.stream("GET", f"{self.settings.access_issuer}/cdn-cgi/access/certs") as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 65536:
                    raise ValueError("Access key response too large")
        document = _PublicKeys.model_validate_json(body)
        keys: dict[str, jwt.PyJWK] = {}
        for raw in document.keys:
            kid = raw.get("kid")
            if (raw.get("kty") != "RSA" or raw.get("use", "sig") != "sig"
                    or raw.get("alg", "RS256") != "RS256" or "d" in raw
                    or not isinstance(kid, str) or not 1 <= len(kid) <= 256 or kid in keys):
                raise ValueError("Invalid Access public key")
            key = jwt.PyJWK(raw, algorithm="RS256")
            if not isinstance(key.key, RSAPublicKey) or key.key.key_size < 2048:
                raise ValueError("Access RSA public key must be at least 2048 bits")
            keys[kid] = key
        return keys
