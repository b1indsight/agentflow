"""Bounded GraphQL traversal, kept at the GitHub adapter's untyped SDK boundary."""

import json
from datetime import UTC, datetime
from typing import Any

from app.domain.github_models import (
    GitHubAdapterError,
    GitHubErrorKind,
    ReviewComment,
    ReviewThread,
    ReviewThreads,
)
from app.github.pagination import next_cursor

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


def decode_thread(node: dict[str, Any], repo: str, pr: int) -> ReviewThread:
    """Decode an identity-checked page; traversal must still prove all pages were read.

    Missing or duplicate comments make the fragment incomplete. Snapshot readers retain
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
        thread_id=node["id"], repo=repo, pr_number=pr, path=node["path"],
        resolved=node["isResolved"], outdated=node["isOutdated"], comments=comments,
        complete=complete,
    )


class ThreadReader:
    """One read budget shared by thread pages and each thread's independent comment pages."""

    def __init__(self, client: Any, repo: str, pr: int) -> None:
        self.client = client
        self.snapshot = ReviewThreads(repo=repo, pr_number=pr, fetched_at=datetime.now(UTC))
        self.requests = 0
        self.bytes = 0

    async def _read(self, query: str, variables: dict[str, object]) -> dict[str, Any] | None:
        if self.requests >= MAX_THREAD_REQUESTS or self.bytes >= MAX_THREAD_BYTES:
            self.snapshot.limitations.append("threads:budget-exhausted")
            return None
        self.requests += 1
        data = await self.client.async_graphql(query, variables)
        if data.get("errors"):
            raise GitHubAdapterError(GitHubErrorKind.UNEXPECTED_RESPONSE, "review_threads")
        self.bytes += len(json.dumps(data).encode())
        if self.bytes > MAX_THREAD_BYTES:
            self.snapshot.limitations.append("threads:byte-limit")
            return None
        return data

    def _cursor(self, page: dict[str, Any], seen: set[str]) -> str | None:
        try:
            cursor = next_cursor(page, None)
        except ValueError:
            self.snapshot.limitations.append("threads:invalid-cursor")
            return None
        if cursor is None:
            return None
        if cursor in seen:
            self.snapshot.limitations.append("threads:invalid-cursor")
            return None
        seen.add(cursor)
        return cursor

    async def _thread(self, node: dict[str, Any]) -> None:
        pull = node["pullRequest"]
        if not belongs_to_pr(node, self.snapshot.repo, self.snapshot.pr_number):
            self.snapshot.limitations.append("thread:identity-mismatch")
            return
        thread = decode_thread(node, self.snapshot.repo, self.snapshot.pr_number)
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
            fragment = decode_thread(follow, self.snapshot.repo, self.snapshot.pr_number)
            complete &= fragment.complete
        self.snapshot.threads.append(thread.model_copy(update={
            "comments": comments, "complete": complete and bool(comments),
        }))

    async def one(self, thread_id: str) -> ReviewThreads:
        data = await self._read(THREAD_QUERY, {"id": thread_id, "comments": None})
        node = data.get("node") if data else None
        if node and node["id"] == thread_id:
            await self._thread(node)
        else:
            self.snapshot.limitations.append("thread:missing-node")
        return self.snapshot

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
                self.snapshot.limitations.append("threads:missing-pr")
                break
            page = pull["reviewThreads"]
            for node in page["nodes"]:
                if node is None or node["id"] in ids:
                    self.snapshot.limitations.append("thread:missing-or-duplicate")
                    continue
                ids.add(node["id"])
                await self._thread(node)
            cursor = self._cursor(page, seen)
            if cursor is None:
                break
        return self.snapshot
