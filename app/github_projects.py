"""Small installation-authenticated Projects adapter; the caller owns recovery."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from app.github_models import GitHubAdapterError, GitHubErrorKind
from app.project_models import (
    ProjectConfig,
    ProjectItem,
    ProjectItemsPage,
    ProjectStatus,
    ProjectTarget,
)

if TYPE_CHECKING:
    from app.github_client import GitHubApp

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
  node(id: $project) { ... on ProjectV2 { id items(first: 100, after: $after) {
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


def require(value: bool, reason: str = "response") -> None:
    if not value:
        raise GitHubAdapterError(GitHubErrorKind.UNEXPECTED_RESPONSE, f"project_{reason}")


def following(connection: Any, previous: str | None) -> str | None:
    info = connection["pageInfo"]
    require(type(info["hasNextPage"]) is bool)
    cursor = info["endCursor"]
    if info["hasNextPage"]:
        require(isinstance(cursor, str) and bool(cursor) and cursor != previous)
        return str(cursor)
    return None


class GitHubProjects:
    def __init__(self, github: GitHubApp, config: ProjectConfig) -> None:
        self.github = github
        self.config = config

    async def _execute[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        return await self.github._execute("projects", operation)

    async def _query(self, query: str, variables: dict[str, Any]) -> Any:
        client = self.github._installation_client(self.config.installation_id, auto_retry=False)
        data = await client.async_graphql(query, variables)
        require(isinstance(data, dict) and not data.get("errors"))
        return data

    async def validate(self, repo: str) -> ProjectTarget:
        async def read() -> ProjectTarget:
            owner, name = self.github._split_repo(repo)
            app = self.github._app_client(auto_retry=False)
            installation = (await app.rest.apps.async_get_repo_installation(owner, name)).parsed_data
            require(installation.id == self.config.installation_id
                    and installation.account.type == "Organization"
                    and installation.account.login.casefold() == owner.casefold(), "installation")
            data = await self._query(TARGET_QUERY, {"project": self.config.project_id, "field": self.config.status_field_id})
            project, field = data["project"], data["field"]
            require(project["id"] == self.config.project_id and project["closed"] is False
                    and project["viewerCanUpdate"] is True, "target")
            require(project["owner"]["__typename"] == "Organization"
                    and project["owner"]["login"].casefold() == owner.casefold(), "owner")
            require(field["id"] == self.config.status_field_id
                    and field["project"]["id"] == self.config.project_id, "field")
            require(set(self.config.options.values()) <= {option["id"] for option in field["options"]}, "options")
            return ProjectTarget(title=project["title"], status_name=field["name"])
        return await self._execute(read)

    async def issue_id(self, repo: str, number: int) -> str:
        async def read() -> str:
            owner, name = self.github._split_repo(repo)
            data = await self._query("""query($owner: String!, $name: String!, $number: Int!) {
              repository(owner: $owner, name: $name) { nameWithOwner issue(number: $number) { id number } }
            }""", {"owner": owner, "name": name, "number": number})
            repository = data["repository"]
            require(repository["nameWithOwner"].casefold() == repo.casefold()
                    and repository["issue"]["number"] == number)
            value = repository["issue"]["id"]
            require(isinstance(value, str) and bool(value))
            return str(value)
        return await self._execute(read)

    def _item(self, data: Any) -> ProjectItem:
        require(data["project"]["id"] == self.config.project_id and type(data["isArchived"]) is bool)
        content = data["content"]
        return ProjectItem(id=data["id"], content_id=content["id"] if content else None,
                           archived=data["isArchived"])

    async def items_page(self, cursor: str | None) -> ProjectItemsPage:
        async def read() -> ProjectItemsPage:
            data = (await self._query(ITEMS_QUERY, {"project": self.config.project_id, "after": cursor}))["node"]
            require(data["id"] == self.config.project_id)
            connection = data["items"]
            items = [self._item(node) for node in connection["nodes"]]
            return ProjectItemsPage(items=items, next_cursor=following(connection, cursor),
                                    complete=all(item.content_id is not None for item in items))
        return await self._execute(read)

    async def read_item(self, item_id: str) -> ProjectItem | None:
        async def read() -> ProjectItem | None:
            cursor = None
            for _ in range(10):
                data = (await self._query(ITEM_QUERY, {"item": item_id, "after": cursor}))["node"]
                if data is None:
                    return None
                item = self._item(data)
                require(item.id == item_id)
                for value in data["fieldValues"]["nodes"]:
                    require(isinstance(value, dict))
                    if value.get("field", {}).get("id") == self.config.status_field_id:
                        item.option_id = value["optionId"]
                        return item
                cursor = following(data["fieldValues"], cursor)
                if cursor is None:
                    return item
            raise GitHubAdapterError(GitHubErrorKind.UNEXPECTED_RESPONSE, "project_field_pagination")
        return await self._execute(read)

    async def add_issue(self, content_id: str) -> ProjectItem:
        async def write() -> ProjectItem:
            data = await self._query(ADD, {"project": self.config.project_id, "content": content_id})
            item = self._item(data["addProjectV2ItemById"]["item"])
            require(item.content_id == content_id and not item.archived)
            return item
        return await self._execute(write)

    async def set_status(self, item_id: str, status: ProjectStatus) -> None:
        async def write() -> None:
            data = await self._query(SET, {"project": self.config.project_id, "item": item_id,
                "field": self.config.status_field_id, "option": self.config.options[status]})
            require(data["updateProjectV2ItemFieldValue"]["projectV2Item"]["id"] == item_id)
        await self._execute(write)
