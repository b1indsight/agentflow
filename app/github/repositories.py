"""Repository facts and operations scoped to one bound installation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, TypeAdapter

from app.github._fields import unobserved_field
from app.github.errors import GitHubError, GitHubErrorKind, translate_errors
from app.github.issues import Issue
from app.github.pull_requests import PullRequest

if TYPE_CHECKING:
    from app.github.apps import Installation


class RemoteRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    branch: str
    sha: str


class LabelDefinition(BaseModel):
    """Defaults used only when a repository label does not already exist."""

    model_config = ConfigDict(frozen=True)
    name: str = Field(min_length=1)
    color: str = Field(pattern=r"^[0-9a-fA-F]{6}$")
    description: str = Field(max_length=100)


class _LabelName(BaseModel):
    name: str


_LABEL_NAMES = TypeAdapter(list[_LabelName])


class PushCapability(BaseModel):
    """A short-lived Git credential that redacts its token in repr and JSON."""

    model_config = ConfigDict(frozen=True)
    token: SecretStr
    repository: str
    permissions: dict[str, Literal["read", "write"]]
    expires_at: datetime


def split_repo(repo: str) -> tuple[str, str]:
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubError(GitHubErrorKind.VALIDATION, "repository")
    return parts[0], parts[1]


def normalize_branch(branch: str) -> str:
    normalized = branch.removeprefix("refs/heads/").removeprefix("heads/")
    if not normalized:
        raise GitHubError(GitHubErrorKind.VALIDATION, "branch")
    return normalized


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


class Repository(BaseModel):
    """Bind locally with ``Repository(installation, 'owner/name')``; refetch is explicit."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    full_name: str
    default_branch: Annotated[str, unobserved_field()]
    _installation: Installation | None = PrivateAttr(default=None)

    def __init__(
        self, installation: Installation | None = None, full_name: str | None = None,
        **facts: Any,
    ) -> None:
        if installation is not None:
            from app.github.apps import Installation

            if not isinstance(installation, Installation):
                raise ValueError("installation must be an Installation resource")
        if full_name is not None:
            facts["full_name"] = full_name
        super().__init__(**facts)
        split_repo(self.full_name)
        self._installation = installation

    def __getattribute__(self, name: str) -> Any:
        if (name in type(self).model_fields
                and name not in object.__getattribute__(self, "__pydantic_fields_set__")):
            raise AttributeError(f"{name} has not been observed; call refetch()")
        return super().__getattribute__(name)

    @property
    def installation(self) -> Installation:
        if self._installation is None:
            raise AttributeError("Repository is not bound to an installation")
        return self._installation

    @property
    def owner(self) -> str:
        return split_repo(self.full_name)[0]

    @property
    def name(self) -> str:
        return split_repo(self.full_name)[1]

    async def refetch(self) -> Repository:
        client = self.installation._client(auto_retry=False)

        with translate_errors("get_repository"):
            data = (await client.rest.repos.async_get(self.owner, self.name)).parsed_data
            if data.full_name.casefold() != self.full_name.casefold():
                raise ValueError("repository response does not match its bound identity")
            return Repository(
                self.installation, data.full_name, default_branch=data.default_branch,
            )

    async def fetch_issue(self, number: int) -> Issue:
        return await Issue(self, number).refetch()

    async def fetch_pull_request(self, number: int) -> PullRequest:
        return await PullRequest(self, number).refetch()

    async def ensure_labels(self, definitions: Sequence[LabelDefinition]) -> tuple[str, ...]:
        """Create missing definitions only; uncertain writes are recovered by the next full read.

        A 422 is success only after a positive lookup of the concurrently created label.
        Return the names created by this call, leaving all existing metadata untouched.
        """
        client = self.installation._client(auto_retry=False)
        existing: set[str] = set()
        page = 1
        with translate_errors("list_repository_labels"):
            while True:
                result = await client.rest.issues.async_list_labels_for_repo(
                    self.owner, self.name, per_page=100, page=page,
                )
                labels = _LABEL_NAMES.validate_python(result.parsed_data, from_attributes=True)
                existing.update(label.name.casefold() for label in labels)
                if len(labels) < 100:
                    break
                page += 1
        created: list[str] = []
        for definition in definitions:
            if definition.name.casefold() in existing:
                continue
            try:
                with translate_errors(f"create_label ({definition.name})"):
                    data = (await client.rest.issues.async_create_label(
                        self.owner, self.name, name=definition.name, color=definition.color,
                        description=definition.description,
                    )).parsed_data
            except GitHubError as error:
                if error.status_code != 422:
                    raise
                try:
                    with translate_errors(f"get_label ({definition.name})"):
                        data = (await client.rest.issues.async_get_label(
                            self.owner, self.name, definition.name,
                        )).parsed_data
                except GitHubError as lookup:
                    if lookup.status_code == 404:
                        raise error from None
                    raise
            else:
                created.append(definition.name)
            with translate_errors("repository_label_identity"):
                if data.name.casefold() != definition.name.casefold():
                    raise ValueError("label response identity mismatch")
            existing.add(definition.name.casefold())
        return tuple(created)

    async def fetch_ref(self, branch: str, *, retry_transient: bool = False) -> RemoteRef:
        """Durable callers own retries; Flow tools opt into the original bounded SDK retry."""
        branch = normalize_branch(branch)
        client = self.installation._client(auto_retry=retry_transient)

        with translate_errors("get_ref"):
            data = (await client.rest.git.async_get_ref(
                self.owner, self.name, f"heads/{branch}",
            )).parsed_data
            return RemoteRef(branch=branch, sha=data.object_.sha)

    async def compare_commits(self, base: str, head: str) -> bool:
        """Prove a normal forward delivery from GitHub's comparison of immutable SHAs."""
        client = self.installation._client(auto_retry=False)

        with translate_errors("compare_commits"):
            data = (await client.rest.repos.async_compare_commits(
                self.owner, self.name, f"{base}...{head}", per_page=1,
            )).parsed_data
            return (data.status == "ahead" and data.merge_base_commit.sha == base
                    and data.base_commit.sha == base and data.behind_by == 0)

    async def find_pull_request_by_head(self, branch: str) -> PullRequest | None:
        branch = normalize_branch(branch)
        client = self.installation._client()

        with translate_errors("find_pull_request_by_head"):
            pulls: Any = (await client.rest.pulls.async_list(
                self.owner, self.name, state="all", head=f"{self.owner}:{branch}", per_page=2,
            )).parsed_data
            if not pulls:
                return None
            if len(pulls) != 1:
                raise GitHubError(
                    GitHubErrorKind.UNEXPECTED_RESPONSE, "find_pull_request_by_head",
                )
            return PullRequest._from_response(self, pulls[0])

    async def open_pull_request(
        self, head: str, base: str, title: str, body: str,
    ) -> PullRequest:
        client = self.installation._client(auto_retry=False)

        with translate_errors("open_pull_request"):
            data = (await client.rest.pulls.async_create(
                self.owner, self.name, title=title, head=head, base=base, body=body,
            )).parsed_data
            return PullRequest._from_response(self, data)

    async def create_push_capability(self) -> PushCapability:
        client = self.installation.app._client()

        with translate_errors("create_push_capability"):
            data = (await client.rest.apps.async_create_installation_access_token(
                self.installation.installation_id, repositories=[self.name],
                permissions={"contents": "write"},
            )).parsed_data
            permissions = _permissions(data.permissions)
            repositories = [item.full_name for item in (data.repositories or [])]
            # GitHub may include implicit read-only repository metadata access.
            if permissions not in (
                {"contents": "write"}, {"contents": "write", "metadata": "read"},
            ) or repositories != [self.full_name]:
                raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, "create_push_capability")
            return PushCapability(
                token=SecretStr(data.token), repository=self.full_name,
                permissions=permissions, expires_at=datetime.fromisoformat(data.expires_at),
            )
