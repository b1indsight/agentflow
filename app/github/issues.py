"""Issue observations and the comment/label operations shared with pull requests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from app.github._fields import unobserved_field
from app.github.errors import GitHubError, GitHubErrorKind, translate_errors
from app.github.feedback import RemoteFeedback, decode_feedback
from app.github.pagination import Batch, validate_limit

if TYPE_CHECKING:
    from app.github.pull_requests import PullRequest
    from app.github.repositories import Repository


async def _comment(target: Issue | PullRequest, body: str) -> int:
    """Create a child comment and return its actual database ID."""
    repo = target.repository
    client = repo.installation._client(auto_retry=False)

    with translate_errors("comment"):
        data = (await client.rest.issues.async_create_comment(
            repo.owner, repo.name, target.number, body=body,
        )).parsed_data
        if not isinstance(data.id, int) or data.id <= 0:
            raise ValueError("missing comment ID")
        return data.id


async def _find_comment(target: Issue | PullRequest, marker: str) -> int | None:
    """Scan all pages for an exact recovery marker authored by this App's bot."""
    repo = target.repository
    client = repo.installation._client()

    with translate_errors("find_comment"):
        page = 1
        while True:
            comments: Any = (await client.rest.issues.async_list_comments(
                repo.owner, repo.name, target.number, per_page=100, page=page,
            )).parsed_data
            for comment in comments:
                if (
                    comment.user is not None
                    and comment.user.login == repo.installation.app.bot_login
                    and comment.user.type == "Bot"
                    and marker in (comment.body or "").splitlines()
                ):
                    return int(comment.id)
            if len(comments) < 100:
                return None
            page += 1


_COMMENTS = """
query($owner:String!,$name:String!,$number:Int!,$cursor:String,$limit:Int!){
  repository(owner:$owner,name:$name){issueOrPullRequest(number:$number){
    ... on Issue { comments(first:$limit,after:$cursor) { nodes {
      fullDatabaseId body updatedAt author {login __typename}
    } pageInfo {hasNextPage endCursor} } }
    ... on PullRequest { comments(first:$limit,after:$cursor) { nodes {
      fullDatabaseId body updatedAt author {login __typename}
    } pageInfo {hasNextPage endCursor} } }
  }}
}
"""


async def _fetch_comments(
    target: Issue | PullRequest, *, cursor: str | None = None, limit: int = 50,
) -> Batch[RemoteFeedback]:
    """Request one GraphQL batch of discussion comments, preserving full bodies."""
    validate_limit(limit)
    repo = target.repository
    client = repo.installation._client(auto_retry=False)

    with translate_errors("feedback_page"):
        data = await client.async_graphql(_COMMENTS, {
            "owner": repo.owner, "name": repo.name, "number": target.number,
            "cursor": cursor, "limit": limit,
        })
        if data.get("errors"):
            raise ValueError("partial comment response")
        connection = data["repository"]["issueOrPullRequest"]["comments"]
        return decode_feedback(connection, cursor, limit)


async def _fetch_labels(target: Issue | PullRequest) -> list[str]:
    repo = target.repository
    client = repo.installation._client()

    with translate_errors("get_labels"):
        labels: list[str] = []
        page = 1
        while True:
            data: Any = (await client.rest.issues.async_list_labels_on_issue(
                repo.owner, repo.name, target.number, per_page=100, page=page,
            )).parsed_data
            labels.extend(label.name for label in data)
            if len(data) < 100:
                return labels
            page += 1


async def _add_label(target: Issue | PullRequest, label: str) -> None:
    """Check a configured repository label before adding it without replacement."""
    repo = target.repository
    client = repo.installation._client()
    with translate_errors("get_label (preconfigure the repository status label)"):
        await client.rest.issues.async_get_label(repo.owner, repo.name, label)
    client = repo.installation._client(auto_retry=False)
    with translate_errors("add_label"):
        response = await client.rest.issues.async_add_labels(
            repo.owner, repo.name, target.number, labels=[label],
        )
    labels: Any = response.parsed_data
    if label.casefold() not in {item.name.casefold() for item in labels}:
        raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, "add_label")


async def _remove_label(target: Issue | PullRequest, label: str) -> None:
    """A 404 confirms absence only if the target remains readable without the label."""
    repo = target.repository
    client = repo.installation._client(auto_retry=False)
    try:
        with translate_errors("remove_label"):
            await client.rest.issues.async_remove_label(
                repo.owner, repo.name, target.number, label,
            )
    except GitHubError as error:
        if error.status_code != 404:
            raise
        labels = await _fetch_labels(target)
        if label.casefold() in {item.casefold() for item in labels}:
            raise


class Issue(BaseModel):
    """An immutable observed Issue, or a locally bound identity awaiting refetch."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    number: int = Field(gt=0)
    title: Annotated[str, unobserved_field()]
    body: Annotated[str, unobserved_field()]
    state: Annotated[str, unobserved_field()]
    _repository: Repository | None = PrivateAttr(default=None)

    def __init__(
        self, repository: Repository | None = None, number: int | None = None, **facts: Any,
    ) -> None:
        if repository is not None:
            from app.github.repositories import Repository

            if not isinstance(repository, Repository):
                raise ValueError("repository must be a Repository resource")
        if number is not None:
            facts["number"] = number
        super().__init__(**facts)
        self._repository = repository

    def __getattribute__(self, name: str) -> Any:
        # Unset Pydantic defaults represent unknown observations, never remote facts.
        if (name in type(self).model_fields
                and name not in object.__getattribute__(self, "__pydantic_fields_set__")):
            raise AttributeError(f"{name} has not been observed; call refetch()")
        return super().__getattribute__(name)

    @property
    def repository(self) -> Repository:
        if self._repository is None:
            raise AttributeError("Issue is not bound to a repository")
        return self._repository

    async def refetch(self) -> Issue:
        repo = self.repository
        client = repo.installation._client(auto_retry=False)

        with translate_errors("get_issue"):
            data = (await client.rest.issues.async_get(
                repo.owner, repo.name, self.number,
            )).parsed_data
            if data.number != self.number:
                raise ValueError("Issue response does not match its bound number")
            return Issue(
                repo, data.number, title=data.title, body=data.body or "", state=data.state,
            )


    async def fetch_content_id(self) -> str:
        """Read an Issue node ID while preserving the Projects endpoint's identity checks."""
        repo = self.repository
        client = repo.installation._client(auto_retry=False)

        with translate_errors("projects"):
            data = await client.async_graphql("""query($owner: String!, $name: String!, $number: Int!) {
              repository(owner: $owner, name: $name) { nameWithOwner issue(number: $number) { id number } }
            }""", {"owner": repo.owner, "name": repo.name, "number": self.number})
            if not isinstance(data, dict) or data.get("errors"):
                raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, "project_response")
            repository = data["repository"]
            issue = repository["issue"]
            value = issue["id"]
            if (repository["nameWithOwner"].casefold() != repo.full_name.casefold()
                    or issue["number"] != self.number
                    or not isinstance(value, str) or not value):
                raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, "project_response")
            return value


    # Function descriptors bind the shared Issues endpoint operations to each resource.
    comment = _comment
    find_comment = _find_comment
    fetch_comments = _fetch_comments
    fetch_labels = _fetch_labels
    add_label = _add_label
    remove_label = _remove_label
