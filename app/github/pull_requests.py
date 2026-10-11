"""Immutable pull-request observations and PR-specific operations."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from app.github._fields import unobserved_field
from app.github.errors import translate_errors
from app.github.feedback import FeedbackPage, RemoteFeedback
from app.github.issues import (
    _add_label,
    _comment,
    _fetch_labels,
    _find_comment,
    _list_comments_page,
    _remove_label,
)
from app.github.pagination import next_cursor
from app.github.review_threads import ReviewThread
from app.github.thread_pages import _THREAD_PAGE_QUERY, ThreadCursor
from app.github.threads import (
    MAX_THREAD_BYTES,
    THREAD_QUERY,
    ReviewThreadPage,
    ReviewThreads,
    ThreadReader,
    belongs_to_pr,
    decode_thread,
)

if TYPE_CHECKING:
    from app.github.repositories import Repository

_REVIEWS = """
query($owner:String!,$name:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$name){pullRequest(number:$number){
    reviews(first:50,after:$cursor){
      nodes { fullDatabaseId body updatedAt state author {login __typename} }
      pageInfo {hasNextPage endCursor}
    }
  }}
}
"""


class PullRequestMerge(BaseModel):
    """Required facts from a merged pull_request.closed webhook, before event reservation."""

    model_config = ConfigDict(frozen=True)
    number: int = Field(gt=0)
    merged: Literal[True]
    merged_at: AwareDatetime
    merge_commit_sha: str = Field(min_length=1)


def _observed(data: Any, field: str) -> bool:
    """SDK model defaults do not mean GitHub included the field in its response."""
    fields = getattr(data, "model_fields_set", None)
    return field in (fields if fields is not None else vars(data))


class PullRequest(BaseModel):
    """A concrete PR identity with the facts observed by its construction or query."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    number: int = Field(gt=0)
    state: Annotated[Literal["open", "closed"], unobserved_field()]
    head_branch: Annotated[str, unobserved_field()]
    head_sha: Annotated[str, unobserved_field()]
    base_branch: Annotated[str, unobserved_field()]
    repo: Annotated[str, unobserved_field()]
    title: Annotated[str, unobserved_field()]
    body: Annotated[str, unobserved_field()]
    base_sha: Annotated[str, unobserved_field()]
    head_repository: Annotated[str | None, unobserved_field()]
    merged: Annotated[bool, unobserved_field()]
    merged_at: Annotated[AwareDatetime | None, unobserved_field()]
    merge_commit_sha: Annotated[str | None, unobserved_field()]
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
        if repository is not None:
            facts.setdefault("repo", repository.full_name)
        super().__init__(**facts)
        if (repository is not None and self.repo.casefold()
                != repository.full_name.casefold()):
            raise ValueError("pull-request facts do not match the bound repository")
        self._repository = repository

    def __getattribute__(self, name: str) -> Any:
        if (name in type(self).model_fields
                and name not in object.__getattribute__(self, "__pydantic_fields_set__")):
            raise AttributeError(f"{name} has not been observed; call refetch()")
        return super().__getattribute__(name)

    @model_validator(mode="after")
    def _require_merge_facts(self) -> PullRequest:
        if "merged" in self.model_fields_set and self.merged:
            required = {"state", "merged_at", "merge_commit_sha"}
            if not required <= self.model_fields_set:
                raise ValueError("merged PR requires observed merge facts")
            if self.state != "closed" or self.merged_at is None or not self.merge_commit_sha:
                raise ValueError("merged PR requires closed state, timestamp and merge commit SHA")
        return self

    @property
    def repository(self) -> Repository:
        if self._repository is None:
            raise AttributeError("PullRequest is not bound to a repository")
        return self._repository

    @classmethod
    def _from_response(
        cls, repository: Repository, data: Any, *, full: bool = False,
    ) -> PullRequest:
        """Decode GET fields or only observed list/create fields, without another request."""
        fields = {
            "number": data.number, "state": data.state, "head_branch": data.head.ref,
            "head_sha": data.head.sha, "base_branch": data.base.ref,
        }
        for field in ("title", "merged", "merged_at", "merge_commit_sha"):
            if full or _observed(data, field):
                fields[field] = getattr(data, field)
        if full or _observed(data, "body"):
            fields["body"] = data.body or ""
        if full or (_observed(data.base, "repo") and data.base.repo is not None):
            fields["repo"] = data.base.repo.full_name
        if full or _observed(data.base, "sha"):
            fields["base_sha"] = data.base.sha
        if full or _observed(data.head, "repo"):
            fields["head_repository"] = (
                data.head.repo.full_name if data.head.repo is not None else None
            )
        return cls(repository, **fields)

    async def refetch(self) -> PullRequest:
        """Read current full PR state; a missing PR is an error, not a close event."""
        repo = self.repository
        client = repo.installation._client(auto_retry=False)

        with translate_errors("get_pull_request"):
            data: Any = (await client.rest.pulls.async_get(
                repo.owner, repo.name, self.number,
            )).parsed_data
            if data.number != self.number:
                raise ValueError("pull-request response does not match its bound number")
            return PullRequest._from_response(repo, data, full=True)

    async def fetch_reviewers(self, *, since: datetime) -> set[str]:
        """Return requested reviewers and people who reviewed since the durable effect."""
        repo = self.repository
        client = repo.installation._client()

        with translate_errors("get_reviewers"):
            pull = (await client.rest.pulls.async_get(
                repo.owner, repo.name, self.number,
            )).parsed_data
            reviewers = {user.login for user in (pull.requested_reviewers or [])}
            page = 1
            while True:
                data: Any = (await client.rest.pulls.async_list_reviews(
                    repo.owner, repo.name, self.number, per_page=100, page=page,
                )).parsed_data
                reviewers.update(
                    review.user.login for review in data
                    if review.user is not None and review.submitted_at is not None
                    and review.submitted_at >= since and review.state.casefold() != "pending"
                )
                if len(data) < 100:
                    return reviewers
                page += 1

    async def request_reviewers(self, reviewers: list[str]) -> None:
        if not reviewers:
            return
        repo = self.repository
        client = repo.installation._client(auto_retry=False)
        with translate_errors("request_reviewers"):
            await client.rest.pulls.async_request_reviewers(
                repo.owner, repo.name, self.number, reviewers=reviewers,
            )

    async def list_reviews_page(self, cursor: str | None) -> FeedbackPage:
        """Read a durable page using edit timestamps, rather than REST submitted_at."""
        repo = self.repository
        client = repo.installation._client(auto_retry=False)
        with translate_errors("feedback_page"):
            data = await client.async_graphql(_REVIEWS, {
                "owner": repo.owner, "name": repo.name, "number": self.number, "cursor": cursor,
            })
            if data.get("errors"):
                raise ValueError("partial review response")
            connection = data["repository"]["pullRequest"]["reviews"]
            following = next_cursor(connection, cursor)
            return FeedbackPage(items=[RemoteFeedback(
                github_id=node["fullDatabaseId"], author=node["author"]["login"], body=node["body"],
                updated_at=node["updatedAt"], state=node["state"].lower(),
                is_bot=node["author"]["__typename"] == "Bot",
            ) for node in connection["nodes"] if node["author"] is not None], next_cursor=following)

    async def list_files(self) -> AsyncIterator[Any]:
        """Yield parsed files in API order; callers may stop after their evidence quota."""
        repo = self.repository
        client = repo.installation._client(auto_retry=False)
        page = 1
        while True:
            data: list[Any]
            with translate_errors("list_pull_request_files"):
                data = (await client.rest.pulls.async_list_files(
                    repo.owner, repo.name, self.number, per_page=20, page=page,
                )).parsed_data
            for item in data:
                yield item
            if len(data) < 20:
                break
            page += 1

    async def list_discussions(
        self, source: Literal["conversation", "review_body", "inline"],
    ) -> AsyncIterator[Any]:
        """Yield parsed discussions for one source, stopping when its final page is read."""
        repo = self.repository
        client = repo.installation._client(auto_retry=False)
        page = 1
        while True:
            data: list[Any]
            with translate_errors("list_pull_request_discussions"):
                if source == "conversation":
                    data = (await client.rest.issues.async_list_comments(
                        repo.owner, repo.name, self.number, per_page=10, page=page,
                    )).parsed_data
                elif source == "review_body":
                    data = (await client.rest.pulls.async_list_reviews(
                        repo.owner, repo.name, self.number, per_page=10, page=page,
                    )).parsed_data
                else:
                    data = (await client.rest.pulls.async_list_review_comments(
                        repo.owner, repo.name, self.number, per_page=10, page=page,
                    )).parsed_data
            for item in data:
                yield item
            if len(data) < 10:
                break
            page += 1

    async def list_review_thread_page(self, cursor: str | None) -> ReviewThreadPage:
        """Read resolved and unresolved threads with at most one request per invocation."""
        with translate_errors("review_thread_page"):
            from githubkit.exception import GraphQLFailed

            state = ThreadCursor.model_validate_json(cursor) if cursor else ThreadCursor()
            if bool(state.thread_id) != bool(state.comments):
                raise ValueError("invalid nested cursor")
            repository = self.repository
            repo, pr = repository.full_name, self.number
            client = repository.installation._client(auto_retry=False)
            snapshot = ReviewThreads(repo=repo, pr_number=pr, fetched_at=datetime.now(UTC))
            threads: list[ReviewThread] = []
            limitations: list[str] = []
            owner, name = repo.split("/")
            try:
                data = await client.async_graphql(
                    THREAD_QUERY if state.thread_id else _THREAD_PAGE_QUERY,
                    {"id": state.thread_id, "comments": state.comments} if state.thread_id else
                    {"owner": owner, "name": name, "pr": pr, "after": state.after, "comments": None},
                )
            except GraphQLFailed as error:
                # Only this exact missing nested lookup can advance, and it remains incomplete evidence.
                errors = error.response.errors or []
                if (not state.thread_id or error.response.data != {"node": None}
                        or not errors or any(item.type != "NOT_FOUND" for item in errors)):
                    raise
                data = {"node": None}
            if data.get("errors") or len(json.dumps(data).encode()) > MAX_THREAD_BYTES:
                raise ValueError("partial or oversized thread page")
            following = state.following
            if state.thread_id:
                node = data["node"]
                if node is None:
                    # A thread removed between nested pages must not trap the entire PR scan forever.
                    limitations.append("thread:missing-node")
                elif node["id"] != state.thread_id:
                    raise ValueError("thread identity changed")
            else:
                connection = data["repository"]["pullRequest"]["reviewThreads"]
                following = next_cursor(connection, state.after)
                nodes = connection["nodes"]
                if len(nodes) > 1 or (not nodes and following):
                    raise ValueError("invalid thread page size")
                node = nodes[0] if nodes else None
                if nodes and node is None:
                    raise ValueError("missing thread entry")
            continuation = ThreadCursor(after=following).model_dump_json() if following else None
            if node is not None:
                if not belongs_to_pr(node, repo, pr):
                    raise ValueError("thread does not belong to tracked PR")
                comments_page = node["comments"]
                comments_after = next_cursor(comments_page, state.comments)
                thread = decode_thread(node, self)
                if not thread.complete:
                    raise ValueError("missing or duplicate comment identity")
                threads.append(thread.model_copy(update={
                    # Even the final nested page is a fragment, never a complete coverage snapshot.
                    "complete": bool(thread.comments) and not comments_after and not state.thread_id,
                }))
                if comments_after:
                    continuation = ThreadCursor(
                        after=state.after, thread_id=node["id"], comments=comments_after, following=following,
                    ).model_dump_json()
            snapshot = snapshot.model_copy(update={"threads": tuple(threads), "limitations": tuple(limitations)})
            return ReviewThreadPage(snapshot=snapshot, next_cursor=continuation)

    async def list_review_threads(self) -> ReviewThreads:
        reader = ThreadReader(self, auto_retry=False)
        with translate_errors("list_review_threads"):
            return await reader.all()

    comment = _comment
    find_comment = _find_comment
    list_comments_page = _list_comments_page
    fetch_labels = _fetch_labels
    add_label = _add_label
    remove_label = _remove_label
