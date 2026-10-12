"""Immutable Projects resources; the caller owns durable write recovery."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StringConstraints,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)

from app.domain.project_models import ProjectStatus
from app.github._fields import unobserved_field
from app.github.errors import GitHubError, GitHubErrorKind, translate_errors
from app.github.pagination import next_cursor

if TYPE_CHECKING:
    from app.github.apps import Installation
    from app.github.repositories import Repository

NodeID = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    installation_id: int = Field(gt=0)
    project_id: NodeID
    status_field_id: NodeID
    options: dict[ProjectStatus, NodeID]

    @model_validator(mode="after")
    def all_statuses(self) -> ProjectConfig:
        if set(self.options) != set(ProjectStatus):
            raise ValueError("Projects requires mappings for all five logical statuses")
        return self


class ProjectTarget(BaseModel):
    title: str
    status_name: str


ITEM_FIELDS = "id project { id } isArchived content { __typename ... on Issue { id } ... on PullRequest { id } ... on DraftIssue { id } }"
TARGET_QUERY = """query($project: ID!, $field: ID!) {
  project: node(id: $project) { ... on ProjectV2 {
    id title closed viewerCanUpdate owner { __typename ... on Organization { login } }
  } }
  field: node(id: $field) { ... on ProjectV2SingleSelectField {
    id name project { id } options { id name }
  } }
}"""
ITEMS_QUERY = """query($project: ID!, $after: String) {
  node(id: $project) { ... on ProjectV2 { id items(first: 100, after: $after, archivedStates: [ARCHIVED, NOT_ARCHIVED]) {
    nodes { ITEM_FIELDS } pageInfo { hasNextPage endCursor }
  } } }
}""".replace("ITEM_FIELDS", ITEM_FIELDS)
ITEM_QUERY = """query($item: ID!, $after: String) {
  node(id: $item) { ... on ProjectV2Item { ITEM_FIELDS
    fieldValues(first: 100, after: $after) {
      nodes { ... on ProjectV2ItemFieldSingleSelectValue {
        optionId field { ... on ProjectV2FieldCommon { id } }
      } } pageInfo { hasNextPage endCursor }
    }
  } }
}""".replace("ITEM_FIELDS", ITEM_FIELDS)
ADD = """mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
    item { ITEM_FIELDS }
  }
}""".replace("ITEM_FIELDS", ITEM_FIELDS)
SET = """mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item,
    fieldId: $field, value: {singleSelectOptionId: $option}}) { projectV2Item { id } }
}"""
_OPTION_ID: TypeAdapter[str | None] = TypeAdapter(NodeID | None)


def require(value: bool, reason: str = "response") -> None:
    if not value:
        raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, f"project_{reason}")


class Project(BaseModel):
    """Bind an installation to a validated, fixed Projects target without a request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    installation_id: int = Field(gt=0)
    project_id: NodeID
    status_field_id: NodeID
    options: Mapping[ProjectStatus, NodeID]
    _installation: Installation = PrivateAttr()

    def __init__(self, installation: Installation, config: ProjectConfig) -> None:
        # Reuse the configuration contract, but never retain its mutable options dict.
        facts = ProjectConfig.model_validate(config.model_dump())
        if facts.installation_id != installation.installation_id:
            raise ValueError("project installation does not match its parent")
        super().__init__(**facts.model_dump())
        self._installation = installation

    @field_validator("options", mode="after")
    @classmethod
    def _freeze_options(cls, value: Mapping[ProjectStatus, str]) -> Mapping[ProjectStatus, str]:
        return MappingProxyType(dict(value))

    @field_serializer("options")
    def _serialize_options(self, value: Mapping[ProjectStatus, str]) -> dict[ProjectStatus, str]:
        return dict(value)

    @property
    def installation(self) -> Installation:
        return self._installation

    async def _query(self, query: str, variables: dict[str, Any]) -> Any:
        client = self.installation._client(auto_retry=False)
        data = await client.async_graphql(query, variables)
        require(isinstance(data, dict) and not data.get("errors"))
        return data

    async def validate(self, repo: Repository) -> ProjectTarget:  # type: ignore[override]
        from githubkit_schemas.latest.models import SimpleUser

        with translate_errors("projects"):
            require(repo.installation.installation_id == self.installation_id, "installation")
            owner, name = repo.owner, repo.name
            app = self.installation.app._client(auto_retry=False)
            installation = (await app.rest.apps.async_get_repo_installation(owner, name)).parsed_data
            account = installation.account
            require(installation.id == self.installation_id
                    and isinstance(account, SimpleUser) and account.type == "Organization"
                    and account.login.casefold() == owner.casefold(), "installation")
            data = await self._query(TARGET_QUERY, {"project": self.project_id, "field": self.status_field_id})
            project, field = data["project"], data["field"]
            require(project["id"] == self.project_id and project["closed"] is False
                    and project["viewerCanUpdate"] is True, "target")
            require(project["owner"]["__typename"] == "Organization"
                    and project["owner"]["login"].casefold() == owner.casefold(), "owner")
            require(field["id"] == self.status_field_id
                    and field["project"]["id"] == self.project_id, "field")
            require(set(self.options.values()) <= {option["id"] for option in field["options"]}, "options")
            return ProjectTarget(title=project["title"], status_name=field["name"])

    def _item(self, data: Any) -> ProjectItem:
        require(data["project"]["id"] == self.project_id and type(data["isArchived"]) is bool)
        content = data["content"]
        return ProjectItem(self, data["id"], content_id=content["id"] if content else None,
                           archived=data["isArchived"])

    async def list_items_page(self, cursor: str | None) -> ProjectItemsPage:
        with translate_errors("projects"):
            data = (await self._query(ITEMS_QUERY, {"project": self.project_id, "after": cursor}))["node"]
            require(data["id"] == self.project_id)
            connection = data["items"]
            items = tuple(self._item(node) for node in connection["nodes"])
            return ProjectItemsPage(items=items, next_cursor=next_cursor(connection, cursor),
                                    complete=all(item.content_id is not None for item in items))

    async def fetch_item(self, item_id: str) -> ProjectItem | None:
        with translate_errors("projects"):
            cursor = None
            for _ in range(10):
                data = (await self._query(ITEM_QUERY, {"item": item_id, "after": cursor}))["node"]
                if data is None:
                    return None
                item = self._item(data)
                require(item.id == item_id)
                for value in data["fieldValues"]["nodes"]:
                    require(isinstance(value, dict))
                    if value.get("field", {}).get("id") == self.status_field_id:
                        return item.model_copy(update={"option_id": _OPTION_ID.validate_python(value["optionId"])})
                cursor = next_cursor(data["fieldValues"], cursor)
                if cursor is None:
                    return item.model_copy(update={"option_id": None})
            raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, "project_field_pagination")

    async def add_issue(self, content_id: str) -> ProjectItem:
        with translate_errors("projects"):
            data = await self._query(ADD, {"project": self.project_id, "content": content_id})
            item = self._item(data["addProjectV2ItemById"]["item"])
            require(item.content_id == content_id and not item.archived)
            return item


class ProjectItem(BaseModel):
    """An immutable item observation, or a locally bound ID awaiting observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: NodeID
    content_id: Annotated[NodeID | None, unobserved_field()]
    archived: Annotated[bool, unobserved_field()]
    option_id: Annotated[NodeID | None, unobserved_field()]
    _project: Project | None = PrivateAttr(default=None)

    def __init__(self, project: Project | None = None, id: str | None = None, **facts: Any) -> None:
        if project is not None and not isinstance(project, Project):
            raise ValueError("project must be a Project resource")
        if id is not None:
            facts["id"] = id
        super().__init__(**facts)
        self._project = project

    def __getattribute__(self, name: str) -> Any:
        if (name in type(self).model_fields
                and name not in object.__getattribute__(self, "__pydantic_fields_set__")):
            raise AttributeError(f"project item {name} has not been observed")
        return super().__getattribute__(name)

    @property
    def project(self) -> Project:
        if self._project is None:
            raise ValueError("project item is not bound to a project")
        return self._project

    async def refetch(self) -> ProjectItem | None:
        return await self.project.fetch_item(self.id)

    async def set_status(self, status: ProjectStatus) -> ProjectItem:
        """Return a copy with the confirmed option; retain all older observations."""
        option_id = self.project.options[status]

        with translate_errors("projects"):
            data = await self.project._query(SET, {"project": self.project.project_id, "item": self.id,
                "field": self.project.status_field_id, "option": option_id})
            require(data["updateProjectV2ItemFieldValue"]["projectV2Item"]["id"] == self.id)
            # model_copy deliberately skips validators; confirm the new fact with
            # the same node-ID contract used by the resource's public field.
            confirmed = _OPTION_ID.validate_python(option_id)
            return self.model_copy(update={"option_id": confirmed})


class ProjectItemsPage(BaseModel):
    """One membership page; invisible content prevents proving an item's absence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ProjectItem, ...]
    next_cursor: str | None
    complete: bool
