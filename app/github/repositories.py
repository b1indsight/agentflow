"""Repository facts and operations scoped to one bound installation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, TypeAdapter

from app.github._fields import unobserved_field
from app.github._names import split_repo
from app.github.apps import Installation
from app.github.errors import GitHubError, GitHubErrorKind, translate_errors
from app.github.pagination import Batch


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


def normalize_branch(branch: str) -> str:
    normalized = branch.removeprefix("refs/heads/").removeprefix("heads/")
    if not normalized:
        raise GitHubError(GitHubErrorKind.VALIDATION, "branch")
    return normalized


class Repository(BaseModel):
    """Bind locally; use ``fetch_repository(installation, 'owner/name')`` to read metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    full_name: str
    default_branch: Annotated[str, unobserved_field()]
    archived: Annotated[bool, unobserved_field()]
    _installation: Installation | None = PrivateAttr(default=None)

    def __init__(
        self, installation: Installation | None = None, full_name: str | None = None,
        **facts: Any,
    ) -> None:
        if installation is not None and not isinstance(installation, Installation):
            raise ValueError("installation must be an Installation resource")
        if full_name is not None:
            facts["full_name"] = full_name
        super().__init__(**facts)
        split_repo(self.full_name)
        self._installation = installation

    def __getattribute__(self, name: str) -> Any:
        if (name in type(self).model_fields
                and name not in object.__getattribute__(self, "__pydantic_fields_set__")):
            raise AttributeError(f"{name} has not been observed; call fetch_repository()")
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

    async def verify_installation(self) -> None:
        """Public repository metadata can be readable even without an installation grant."""
        client = self.installation.app._client(auto_retry=False)
        with translate_errors("get_repo_installation"):
            grant = (await client.rest.apps.async_get_repo_installation(self.owner, self.name)).parsed_data
            if grant.id != self.installation.installation_id or grant.suspended_at is not None:
                raise GitHubError(GitHubErrorKind.PERMISSION, "get_repo_installation")

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

    async def compare_commits(self, base: str, head: str) -> bool:
        """Prove a normal forward delivery from GitHub's comparison of immutable SHAs."""
        client = self.installation._client(auto_retry=False)

        with translate_errors("compare_commits"):
            data = (await client.rest.repos.async_compare_commits(
                self.owner, self.name, f"{base}...{head}", per_page=1,
            )).parsed_data
            return (data.status == "ahead" and data.merge_base_commit.sha == base
                    and data.base_commit.sha == base and data.behind_by == 0)


async def fetch_repository(installation: Installation, full_name: str) -> Repository:
    """Fetch a fresh repository bound to the supplied installation."""
    owner, name = split_repo(full_name)
    client = installation._client(auto_retry=False)
    with translate_errors("get_repository"):
        data = (await client.rest.repos.async_get(owner, name)).parsed_data
        if data.full_name.casefold() != full_name.casefold():
            raise ValueError("repository response does not match its requested identity")
        return Repository(installation, data.full_name, default_branch=data.default_branch, archived=data.archived)


async def fetch_repositories(installation: Installation, *, page: int = 1) -> Batch[Repository]:
    """Read one page of granted repositories, omitting archived repositories."""
    client = installation._client(auto_retry=False)
    with translate_errors("list_installation_repositories"):
        data = (await client.rest.apps.async_list_repos_accessible_to_installation(
            per_page=100, page=page,
        )).parsed_data
        return Batch(
            items=tuple(Repository(installation, item.full_name) for item in data.repositories if not item.archived),
            next_cursor=str(page + 1) if len(data.repositories) == 100 else None,
        )


async def fetch_ref(
    installation: Installation, full_name: str, branch: str, *, retry_transient: bool = False,
) -> RemoteRef:
    """Durable callers own retries; Flow tools opt into the bounded SDK retry."""
    owner, name = split_repo(full_name)
    branch = normalize_branch(branch)
    client = installation._client(auto_retry=retry_transient)
    with translate_errors("get_ref"):
        data = (await client.rest.git.async_get_ref(owner, name, f"heads/{branch}")).parsed_data
        return RemoteRef(branch=branch, sha=data.object_.sha)
