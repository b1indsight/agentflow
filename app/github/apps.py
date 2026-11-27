"""App and installation identities bound to native githubkit authentication."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr

from app.github._names import split_repo
from app.github.errors import GitHubError, GitHubErrorKind, translate_errors
from app.github.pagination import Batch

if TYPE_CHECKING:
    from githubkit import AppAuthStrategy, AppInstallationAuthStrategy, GitHub


class _AppSettings(Protocol):
    github_app_id: int
    github_private_key_path: Path
    github_request_timeout_seconds: float


class PushCapability(BaseModel):
    """A short-lived Git credential that redacts its token in repr and JSON."""

    model_config = ConfigDict(frozen=True)
    token: SecretStr
    repository: str
    permissions: dict[str, Literal["read", "write"]]
    expires_at: datetime
    bot_login: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*\[bot\]$")
    bot_user_id: int = Field(gt=0)


def _permissions(value: Any) -> dict[str, Literal["read", "write"]]:
    if isinstance(value, dict):
        raw = value
    elif hasattr(value, "model_dump"):
        raw = value.model_dump(exclude_none=True, exclude_unset=True)
    else:
        raw = vars(value)
    permissions: dict[str, Literal["read", "write"]] = {}
    for key, item in raw.items():
        if item in {"read", "write"}:
            permissions[key] = item
    return permissions


class GitHubApp(BaseModel):
    """Read App credentials once; binding an installation performs no network I/O."""

    model_config = ConfigDict(frozen=True)
    app_id: int = Field(gt=0)
    _sdk: GitHub[AppAuthStrategy] = PrivateAttr()
    _bot_login: str | None = PrivateAttr(default=None)
    _bot_login_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

    def __init__(self, settings: _AppSettings) -> None:
        from githubkit import AppAuthStrategy, GitHub

        if settings.github_app_id <= 0:
            raise ValueError("AGENTFLOW_GITHUB_APP_ID must be a positive integer")
        key = settings.github_private_key_path.read_text()
        if not key.strip():
            raise ValueError("AGENTFLOW_GITHUB_PRIVATE_KEY_PATH points to an empty file")
        super().__init__(app_id=settings.github_app_id)
        self._sdk = GitHub(
            AppAuthStrategy(self.app_id, key),
            timeout=settings.github_request_timeout_seconds,
        )

    @property
    def bot_login(self) -> str:
        """Read the resolved identity without network I/O, including in DB-only consumers."""
        if self._bot_login is None:
            raise RuntimeError("App bot identity has not been resolved")
        return self._bot_login

    async def resolve_bot_login(self) -> str:
        """Derive the bot from this App's JWT; share successful reads, but retry failures."""
        if self._bot_login is None:
            async with self._bot_login_lock:
                if self._bot_login is None:
                    client = self._client(auto_retry=False)
                    with translate_errors("get_app"):
                        app = (await client.rest.apps.async_get_authenticated()).parsed_data
                        if app is None or app.id != self.app_id or not app.slug:
                            raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, "get_app")
                        self._bot_login = f"{app.slug}[bot]"
        return self.bot_login

    def _client(self, *, auto_retry: bool = True) -> GitHub[AppAuthStrategy]:
        # Copy native configuration so an effect cannot change another call's retry policy.
        client = self._sdk.with_auth(self._sdk.auth)
        if not auto_retry:
            client.config = replace(client.config, auto_retry=None)
        return client

    def installation(self, installation_id: int) -> Installation:
        return Installation(self, installation_id)

    async def repository_read_token(self, repository: str) -> SecretStr:
        """Issue a credential for cloning just this repository's contents."""
        owner, name = split_repo(repository)
        client = self._client(auto_retry=False)
        with translate_errors("repository_read_token"):
            grant = (await client.rest.apps.async_get_repo_installation(owner, name)).parsed_data
            token = (await client.rest.apps.async_create_installation_access_token(
                grant.id, repositories=[name], permissions={"contents": "read"},
            )).parsed_data
            if ([item.full_name for item in token.repositories or []] != [repository]
                    or _permissions(token.permissions) not in (
                        {"contents": "read"}, {"contents": "read", "metadata": "read"},
                    )):
                raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, "repository_read_token")
            return SecretStr(token.token)

    async def fetch_installations(self, *, page: int = 1) -> Batch[Installation]:
        """Read one inventory page, excluding suspended installations without losing pagination."""
        client = self._client(auto_retry=False)
        with translate_errors("list_installations"):
            data = (await client.rest.apps.async_list_installations(
                per_page=100, page=page,
            )).parsed_data
            return Batch(
                items=tuple(self.installation(item.id) for item in data
                            if item.suspended_at is None),
                next_cursor=str(page + 1) if len(data) == 100 else None,
            )


class Installation(BaseModel):
    """An installation identity and its native, dynamically refreshed credentials."""

    model_config = ConfigDict(frozen=True)
    installation_id: int = Field(gt=0)
    _app: GitHubApp = PrivateAttr()
    _sdk: GitHub[AppInstallationAuthStrategy] = PrivateAttr()

    def __init__(self, app: GitHubApp, installation_id: int) -> None:
        super().__init__(installation_id=installation_id)
        self._app = app
        self._sdk = app._sdk.with_auth(app._sdk.auth.as_installation(self.installation_id))

    @property
    def app(self) -> GitHubApp:
        return self._app

    def _client(self, *, auto_retry: bool = True) -> GitHub[AppInstallationAuthStrategy]:
        client = self._sdk.with_auth(self._sdk.auth)
        if not auto_retry:
            client.config = replace(client.config, auto_retry=None)
        return client

    async def create_push_capability(self, full_name: str) -> PushCapability:
        """Issue a contents-write token scoped to one repository in this installation."""
        _, name = split_repo(full_name)
        bot_login = await self.app.resolve_bot_login()
        client = self.app._client()
        with translate_errors("create_push_capability"):
            data = (await client.rest.apps.async_create_installation_access_token(
                self.installation_id, repositories=[name], permissions={"contents": "write"},
            )).parsed_data
            permissions = _permissions(data.permissions)
            repositories = [item.full_name for item in (data.repositories or [])]
            # GitHub may include implicit read-only repository metadata access.
            if permissions not in (
                {"contents": "write"}, {"contents": "write", "metadata": "read"},
            ) or repositories != [full_name]:
                raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, "create_push_capability")
            installation_client = self._client()
            bot = (await installation_client.rest.users.async_get_by_username(bot_login)).parsed_data
            if bot.login.lower() != bot_login.lower() or bot.type != "Bot" or bot.id <= 0:
                raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, "create_push_capability")
            return PushCapability(
                token=SecretStr(data.token), repository=full_name,
                permissions=permissions, expires_at=datetime.fromisoformat(data.expires_at),
                bot_login=bot_login, bot_user_id=bot.id,
            )
