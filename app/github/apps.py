"""App and installation identities bound to native githubkit authentication."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from app.github.errors import translate_errors
from app.github.pagination import Batch
from app.github.projects import Project, ProjectConfig
from app.github.repositories import Repository

if TYPE_CHECKING:
    from githubkit import AppAuthStrategy, AppInstallationAuthStrategy, GitHub

    from app.controler.config import ServerSettings


class GitHubApp(BaseModel):
    """Read App credentials once; binding an installation performs no network I/O."""

    model_config = ConfigDict(frozen=True)
    app_id: int = Field(gt=0)
    bot_login: str
    _sdk: GitHub[AppAuthStrategy] = PrivateAttr()

    def __init__(self, settings: ServerSettings) -> None:
        from githubkit import AppAuthStrategy, GitHub

        if settings.github_app_id <= 0:
            raise ValueError("FLOWTOOL_GITHUB_APP_ID must be a positive integer")
        key = settings.github_private_key_path.read_text()
        if not key.strip():
            raise ValueError("FLOWTOOL_GITHUB_PRIVATE_KEY_PATH points to an empty file")
        super().__init__(app_id=settings.github_app_id, bot_login=settings.bot_login)
        self._sdk = GitHub(
            AppAuthStrategy(self.app_id, key),
            timeout=settings.github_request_timeout_seconds,
        )

    def _client(self, *, auto_retry: bool = True) -> GitHub[AppAuthStrategy]:
        # Copy native configuration so an effect cannot change another call's retry policy.
        client = self._sdk.with_auth(self._sdk.auth)
        if not auto_retry:
            client.config = replace(client.config, auto_retry=None)
        return client

    def installation(self, installation_id: int) -> Installation:
        return Installation(self, installation_id)

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

    def repository(self, full_name: str) -> Repository:
        return Repository(self, full_name)

    async def fetch_repositories(self, *, page: int = 1) -> Batch[Repository]:
        """Read one page of granted repositories, omitting archived repositories."""
        client = self._client(auto_retry=False)
        with translate_errors("list_installation_repositories"):
            data = (await client.rest.apps.async_list_repos_accessible_to_installation(
                per_page=100, page=page,
            )).parsed_data
            return Batch(
                items=tuple(self.repository(item.full_name) for item in data.repositories
                            if not item.archived),
                next_cursor=str(page + 1) if len(data.repositories) == 100 else None,
            )

    def project(self, config: ProjectConfig) -> Project:
        return Project(self, config)
