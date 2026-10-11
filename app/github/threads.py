"""Bounded GraphQL traversal and immutable thread evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.github.errors import GitHubError, GitHubErrorKind
from app.github.pagination import next_cursor
from app.github.review_threads import ReviewComment, ReviewThread

if TYPE_CHECKING:
    from app.github.pull_requests import PullRequest


class ReviewThreads(BaseModel):
    """A bounded scan of thread resources; limitations prevent automated resolution."""

    model_config = ConfigDict(frozen=True)
    repo: str
    pr_number: int
    fetched_at: AwareDatetime
    threads: tuple[ReviewThread, ...] = ()
    limitations: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.limitations and all(
            "complete" in thread.model_fields_set and thread.complete for thread in self.threads
        )


class ReviewThreadPage(BaseModel):
    """One scan page; partial comment fragments cannot prove resolution coverage."""

    model_config = ConfigDict(frozen=True)
    snapshot: ReviewThreads
    next_cursor: str | None = None

MAX_THREAD_REQUESTS = 50
MAX_THREAD_BYTES = 2_000_000

THREAD_FIELDS = """
id isResolved isOutdated path
pullRequest { number repository { nameWithOwner } }
comments(first:20,after:$comments) {
  nodes { fullDatabaseId id body updatedAt author { login __typename }
          replyTo { fullDatabaseId } originalCommit { oid } }
  pageInfo { hasNextPage endCursor }
}
"""
THREAD_QUERY = "query($id:ID!,$comments:String){node(id:$id){... on PullRequestReviewThread{" + THREAD_FIELDS + "}}}"
_THREADS = """
query($owner:String!,$name:String!,$pr:Int!,$cursor:String,$comments:String){
repository(owner:$owner,name:$name){pullRequest(number:$pr){
reviewThreads(first:100,after:$cursor){nodes{
""" + THREAD_FIELDS + "} pageInfo {hasNextPage endCursor}}}}}"


def belongs_to_pr(node: dict[str, Any], repo: str, pr: int) -> bool:
    """Check the remote PR identity before accepting any thread fragment."""
    pull = node["pullRequest"]
    return pull["number"] == pr and pull["repository"]["nameWithOwner"].casefold() == repo.casefold()


def decode_thread(node: dict[str, Any], pull_request: PullRequest) -> ReviewThread:
    """Decode an identity-checked page; traversal must still prove all pages were read.

    Missing or duplicate comments make the fragment incomplete. Bounded readers retain
    usable observations; durable page readers reject such fragments before advancing.
    """
    raw_comments = node["comments"]["nodes"]
    if len(raw_comments) > 20:
        raise ValueError("invalid comment page size")
    comments: list[ReviewComment] = []
    ids: set[int] = set()
    complete = True
    for raw in raw_comments:
        if raw is None:
            complete = False
            continue
        author = raw["author"]
        comment = ReviewComment(
            github_id=raw["fullDatabaseId"], node_id=raw["id"], body=raw["body"],
            updated_at=raw["updatedAt"], author=author["login"] if author else "deleted-user",
            is_bot=bool(author and author["__typename"] == "Bot"),
            reply_to_id=raw["replyTo"]["fullDatabaseId"] if raw["replyTo"] else None,
            original_commit_id=raw["originalCommit"]["oid"] if raw["originalCommit"] else None,
        )
        if comment.github_id in ids:
            complete = False
            continue
        ids.add(comment.github_id)
        comments.append(comment)
    return ReviewThread(
        pull_request, node["id"], path=node["path"],
        resolved=node["isResolved"], outdated=node["isOutdated"], comments=tuple(comments),
        complete=complete,
    )


class ThreadReader:
    """One read budget shared by thread pages and each thread's independent comment pages."""

    def __init__(self, pull_request: PullRequest, *, auto_retry: bool) -> None:
        self.pull_request = pull_request
        repo = pull_request.repository
        self.client = repo.installation._client(auto_retry=auto_retry)
        self.snapshot = ReviewThreads(repo=repo.full_name, pr_number=pull_request.number,
                                      fetched_at=datetime.now(UTC))
        self._threads: list[ReviewThread] = []
        self._limitations: list[str] = []
        self.requests = 0
        self.bytes = 0

    async def _read(self, query: str, variables: dict[str, object]) -> dict[str, Any] | None:
        if self.requests >= MAX_THREAD_REQUESTS or self.bytes >= MAX_THREAD_BYTES:
            self._limitations.append("threads:budget-exhausted")
            return None
        self.requests += 1
        data = await self.client.async_graphql(query, variables)
        if data.get("errors"):
            raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, "review_threads")
        self.bytes += len(json.dumps(data).encode())
        if self.bytes > MAX_THREAD_BYTES:
            self._limitations.append("threads:byte-limit")
            return None
        return data

    def _cursor(self, page: dict[str, Any], seen: set[str]) -> str | None:
        try:
            cursor = next_cursor(page, None)
        except ValueError:
            self._limitations.append("threads:invalid-cursor")
            return None
        if cursor is None:
            return None
        if cursor in seen:
            self._limitations.append("threads:invalid-cursor")
            return None
        seen.add(cursor)
        return cursor

    async def _thread(self, node: dict[str, Any]) -> None:
        pull = node["pullRequest"]
        if not belongs_to_pr(node, self.snapshot.repo, self.snapshot.pr_number):
            self._limitations.append("thread:identity-mismatch")
            return
        thread = decode_thread(node, self.pull_request)
        fragment = thread
        comments = []
        seen: set[str] = set()
        ids: set[int] = set()
        complete = fragment.complete
        page = node["comments"]
        while True:
            for comment in fragment.comments:
                if comment.github_id in ids:
                    complete = False
                    continue
                ids.add(comment.github_id)
                comments.append(comment)
            cursor = self._cursor(page, seen)
            if cursor is None:
                break
            data = await self._read(THREAD_QUERY, {"id": node["id"], "comments": cursor})
            follow = data.get("node") if data else None
            if (not follow or follow["id"] != node["id"]
                    or follow["pullRequest"] != pull):
                complete = False
                break
            page = follow["comments"]
            fragment = decode_thread(follow, self.pull_request)
            complete &= fragment.complete
        self._threads.append(thread.model_copy(update={
            "comments": tuple(comments), "complete": complete and bool(comments),
        }))

    async def one(self, thread_id: str, *, root_comment_id: int | None = None) -> ReviewThread:
        """Read bounded evidence, checking a known root against all collected comments."""
        data = await self._read(THREAD_QUERY, {"id": thread_id, "comments": None})
        node = data.get("node") if data else None
        if node and node["id"] == thread_id:
            await self._thread(node)
        else:
            self._limitations.append("thread:missing-node")
        observations = {"fetched_at": self.snapshot.fetched_at,
                        "limitations": tuple(self._limitations)}
        if self._threads:
            thread = self._threads[0]
            observed = thread.model_copy(update={
                **observations, "complete": thread.complete and not self._limitations,
            })
        else:
            observed = ReviewThread(
                self.pull_request, thread_id, complete=False, comments=(),
                fetched_at=self.snapshot.fetched_at, limitations=tuple(self._limitations),
            )
        # The root can occur after the first comment page; bind only the final observation.
        observed._validate_root_comment_id(root_comment_id, observed.comments)
        observed._root_comment_id = root_comment_id
        return observed

    async def all(self) -> ReviewThreads:
        owner, name = self.snapshot.repo.split("/")
        cursor = None
        seen: set[str] = set()
        ids: set[str] = set()
        while True:
            data = await self._read(_THREADS, {"owner": owner, "name": name,
                "pr": self.snapshot.pr_number, "cursor": cursor, "comments": None})
            if data is None:
                break
            pull = (data.get("repository") or {}).get("pullRequest")
            if pull is None:
                self._limitations.append("threads:missing-pr")
                break
            page = pull["reviewThreads"]
            for node in page["nodes"]:
                if node is None or node["id"] in ids:
                    self._limitations.append("thread:missing-or-duplicate")
                    continue
                ids.add(node["id"])
                await self._thread(node)
            cursor = self._cursor(page, seen)
            if cursor is None:
                break
        return self.snapshot.model_copy(update={
            "threads": tuple(self._threads), "limitations": tuple(self._limitations),
        })
