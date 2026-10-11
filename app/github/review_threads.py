"""Immutable review-thread resources with the original bounded read evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PrivateAttr

from app.github._fields import unobserved_field
from app.github.errors import GitHubError, GitHubErrorKind, translate_errors

if TYPE_CHECKING:
    from app.github.pull_requests import PullRequest

_RESOLVE = """mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { isResolved } }
}"""


class ReviewComment(BaseModel):
    """Remote comment identity and content version; node_id is never a thread ID."""

    model_config = ConfigDict(frozen=True)
    github_id: int = Field(gt=0)
    node_id: str
    author: str
    is_bot: bool
    body: str
    updated_at: AwareDatetime
    reply_to_id: int | None = None
    original_commit_id: str | None = None


class ReviewThread(BaseModel):
    """Bind known IDs locally, or retain one query's complete thread evidence.

    `refetch()` returns a new observation. `resolve()` returns a copy with only
    confirmed resolution facts changed; it does not refresh comments or coverage.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    pr_number: int = Field(gt=0)
    path: Annotated[str, unobserved_field()]
    resolved: Annotated[bool, unobserved_field()]
    outdated: Annotated[bool, unobserved_field()]
    comments: Annotated[tuple[ReviewComment, ...], unobserved_field()]
    complete: Annotated[bool, unobserved_field()]
    fetched_at: Annotated[AwareDatetime, unobserved_field()]
    limitations: Annotated[tuple[str, ...], unobserved_field()]
    _pull_request: PullRequest | None = PrivateAttr(default=None)
    _root_comment_id: int | None = PrivateAttr(default=None)

    def __init__(
        self, pull_request: PullRequest | None = None, thread_id: str | None = None, *,
        root_comment_id: int | None = None, **facts: Any,
    ) -> None:
        if pull_request is not None:
            from app.github.pull_requests import PullRequest

            if not isinstance(pull_request, PullRequest):
                raise ValueError("review thread parent must be a PullRequest")
            repo, number = pull_request.repository.full_name, pull_request.number
            observed_repo = facts.pop("repo", repo)
            observed_number = facts.pop("pr_number", number)
            if observed_repo.casefold() != repo.casefold() or observed_number != number:
                raise ValueError("review thread facts do not match the bound pull request")
            facts.update(repo=repo, pr_number=number)
        if "comments" in facts:
            # Revalidate local model copies too, so mutable input cannot enter facts.
            comments = tuple(ReviewComment.model_validate(
                comment.model_dump() if isinstance(comment, ReviewComment) else comment,
            ) for comment in facts["comments"])
            facts["comments"] = comments
        self._validate_root_comment_id(root_comment_id, facts.get("comments", ()))
        if thread_id is not None:
            facts["thread_id"] = thread_id
        super().__init__(**facts)
        self._pull_request = pull_request
        self._root_comment_id = root_comment_id

    @staticmethod
    def _validate_root_comment_id(
        root_comment_id: int | None, comments: tuple[ReviewComment, ...],
    ) -> None:
        if root_comment_id is not None:
            if type(root_comment_id) is not int or root_comment_id <= 0:
                raise ValueError("root comment ID must be a positive database ID")
            roots = {comment.github_id for comment in comments if comment.reply_to_id is None}
            if roots and roots != {root_comment_id}:
                raise ValueError("root comment ID does not match the observed review thread")

    def __getattribute__(self, name: str) -> Any:
        if (name in type(self).model_fields
                and name not in object.__getattribute__(self, "__pydantic_fields_set__")):
            raise AttributeError(f"review thread {name} has not been observed")
        return super().__getattribute__(name)

    @property
    def pull_request(self) -> PullRequest:
        if self._pull_request is None:
            raise AttributeError("review thread has no bound pull request")
        return self._pull_request

    @property
    def root_comment_id(self) -> int | None:
        return self._root_comment_id

    async def refetch(self) -> ReviewThread:
        from app.github.threads import ThreadReader

        reader = ThreadReader(self.pull_request, auto_retry=True)
        with translate_errors("get_review_thread"):
            return await reader.one(self.thread_id, root_comment_id=self.root_comment_id)

    async def find_reply(self, marker: str) -> int | None:
        """Find an exact bot marker; incomplete coverage cannot prove absence."""
        observed = await self.refetch()
        bot_login = self.pull_request.repository.installation.app.bot_login
        for comment in observed.comments:
            if (comment.is_bot and comment.author == bot_login
                    and marker in comment.body.splitlines()):
                return comment.github_id
        if not observed.complete:
            raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, "find_review_reply")
        return None

    async def fetch_resolution(self) -> bool:
        client = self.pull_request.repository.installation._client(auto_retry=True)

        with translate_errors("review_thread_resolved"):
            data = await client.async_graphql(
                "query($id:ID!){node(id:$id){... on PullRequestReviewThread{isResolved}}}",
                {"id": self.thread_id},
            )
            value = data["node"]["isResolved"]
            if not isinstance(value, bool):
                raise TypeError("missing review thread resolution state")
            return value


    async def reply(self, body: str) -> int:
        """Reply using the known root database ID, without looking up the thread."""
        root_comment_id = self.root_comment_id
        if root_comment_id is None:
            raise ValueError("reply requires a known root comment database ID")
        repo = self.pull_request.repository
        client = repo.installation._client(auto_retry=False)

        with translate_errors("reply_review_thread"):
            response = await client.rest.pulls.async_create_reply_for_review_comment(
                repo.owner, repo.name, self.pull_request.number, root_comment_id, body=body,
            )
            return int(response.parsed_data.id)


    async def resolve(self) -> ReviewThread:
        client = self.pull_request.repository.installation._client(auto_retry=False)

        with translate_errors("resolve_review_thread"):
            data = await client.async_graphql(_RESOLVE, {"threadId": self.thread_id})
            confirmed = data["resolveReviewThread"]["thread"]["isResolved"]
            if confirmed is not True:
                raise TypeError("missing confirmed review thread resolution")
            return self.model_copy(update={"resolved": confirmed})
