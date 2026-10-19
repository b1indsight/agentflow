"""Immutable pull-request observations and PR-specific operations."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from app.github._fields import unobserved_field
from app.github.errors import translate_errors
from app.github.feedback import RemoteFeedback, decode_feedback
from app.github.issues import (
    _add_label,
    _comment,
    _fetch_comments,
    _fetch_labels,
    _find_comment,
    _remove_label,
)
from app.github.pagination import Batch, next_cursor, validate_limit
from app.github.review_threads import ReviewThread
from app.github.threads import (
    _THREADS,
    MAX_THREAD_BYTES,
    ReviewThreads,
    ThreadReader,
    belongs_to_pr,
    decode_thread,
)

if TYPE_CHECKING:
    from githubkit_schemas.latest.models import DiffEntry, PullRequestReviewComment

    from app.github.repositories import Repository

_REVIEWS = """
query($owner:String!,$name:String!,$number:Int!,$cursor:String,$limit:Int!){
  repository(owner:$owner,name:$name){pullRequest(number:$number){
    reviews(first:$limit,after:$cursor){
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


class _PullRequestRepository(BaseModel):
    full_name: str


class _PullRequestRef(BaseModel):
    ref: str
    sha: str
    repo: _PullRequestRepository | None


class _PullRequestRead(BaseModel):
    """Required GET facts, including merge_commit_sha omitted by the generated SDK model."""

    number: int
    state: Literal["open", "closed"]
    title: str
    body: str | None
    head: _PullRequestRef
    base: _PullRequestRef
    merged: bool
    merged_at: AwareDatetime | None
    merge_commit_sha: str | None


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
            response = await client.rest.pulls.async_get(
                repo.owner, repo.name, self.number,
            )
            data = _PullRequestRead.model_validate_json(response.raw_response.content)
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

    async def fetch_reviews(
        self, *, cursor: str | None = None, limit: int = 50,
    ) -> Batch[RemoteFeedback]:
        """Request a review batch using actual edit timestamps and its native cursor."""
        validate_limit(limit)
        repo = self.repository
        client = repo.installation._client(auto_retry=False)
        with translate_errors("feedback_page"):
            data = await client.async_graphql(_REVIEWS, {
                "owner": repo.owner, "name": repo.name, "number": self.number, "cursor": cursor, "limit": limit,
            })
            if data.get("errors"):
                raise ValueError("partial review response")
            connection = data["repository"]["pullRequest"]["reviews"]
            return decode_feedback(connection, cursor, limit)

    async def list_files(self) -> AsyncIterator[DiffEntry]:
        """Yield SDK file changes in API order; callers own the evidence quota."""
        repo = self.repository
        client = repo.installation._client(auto_retry=False)
        page = 1
        while True:
            data: list[DiffEntry]
            with translate_errors("list_pull_request_files"):
                data = (await client.rest.pulls.async_list_files(
                    repo.owner, repo.name, self.number, per_page=20, page=page,
                )).parsed_data
            for item in data:
                yield item
            if len(data) < 20:
                break
            page += 1

    async def list_review_comments(self) -> AsyncIterator[PullRequestReviewComment]:
        """Yield inline snippets with their REST diff hunks; callers bound the evidence."""
        repo = self.repository
        client = repo.installation._client(auto_retry=False)
        page = 1
        while True:
            data: list[PullRequestReviewComment]
            with translate_errors("list_pull_request_discussions"):
                data = (await client.rest.pulls.async_list_review_comments(
                    repo.owner, repo.name, self.number, per_page=10, page=page,
                )).parsed_data
            for item in data:
                yield item
            if len(data) < 10:
                break
            page += 1

    async def fetch_threads(
        self, *, cursor: str | None = None, limit: int = 100,
    ) -> Batch[ReviewThread]:
        """Request threads with their first 20 comments and independent comment cursors."""
        validate_limit(limit)
        repo = self.repository
        client = repo.installation._client(auto_retry=False)
        with translate_errors("review_threads"):
            data = await client.async_graphql(_THREADS, {
                "owner": repo.owner, "name": repo.name, "pr": self.number, "cursor": cursor, "limit": limit,
            })
            if data.get("errors") or len(json.dumps(data).encode()) > MAX_THREAD_BYTES:
                raise ValueError("partial or oversized thread batch")
            connection = data["repository"]["pullRequest"]["reviewThreads"]
            following = next_cursor(connection, cursor)
            nodes = connection["nodes"]
            if len(nodes) > limit or (not nodes and following):
                raise ValueError("invalid thread batch size")
            threads = []
            for node in nodes:
                if node is None or not belongs_to_pr(node, repo.full_name, self.number):
                    raise ValueError("missing or foreign thread")
                thread = decode_thread(node, self)
                if thread.limitations:
                    raise ValueError("missing or duplicate comment identity")
                threads.append(thread)
            if len({thread.thread_id for thread in threads}) != len(threads):
                raise ValueError("duplicate thread identity")
            return Batch[ReviewThread](items=tuple(threads), next_cursor=following)

    async def list_review_threads(self) -> ReviewThreads:
        reader = ThreadReader(self, auto_retry=False)
        with translate_errors("list_review_threads"):
            return await reader.all()

    comment = _comment
    find_comment = _find_comment
    fetch_comments = _fetch_comments
    fetch_labels = _fetch_labels
    add_label = _add_label
    remove_label = _remove_label
