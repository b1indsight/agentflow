"""Bounded GraphQL traversal, kept at the GitHub adapter's untyped SDK boundary."""

import json
from datetime import UTC, datetime
from typing import Any

from app.github_models import (
    GitHubAdapterError,
    GitHubErrorKind,
    ReviewComment,
    ReviewThread,
    ReviewThreads,
)

MAX_THREAD_REQUESTS = 50
MAX_THREAD_BYTES = 2_000_000

_FIELDS = """
id isResolved isOutdated path
pullRequest { number repository { nameWithOwner } }
comments(first:20,after:$cursor) {
  nodes { databaseId id body updatedAt author { login __typename }
          replyTo { databaseId } originalCommit { oid } }
  pageInfo { hasNextPage endCursor }
}
"""
_THREAD = "query($id:ID!,$cursor:String){node(id:$id){... on PullRequestReviewThread{" + _FIELDS + "}}}"
_THREADS = """
query($owner:String!,$name:String!,$pr:Int!,$cursor:String){
repository(owner:$owner,name:$name){pullRequest(number:$pr){
reviewThreads(first:100,after:$cursor){nodes{
""" + _FIELDS.replace("after:$cursor", "after:null") + "} pageInfo {hasNextPage endCursor}}}}}"


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
        info = page["pageInfo"]
        if not isinstance(info["hasNextPage"], bool):
            raise TypeError("invalid page completeness")
        if not info["hasNextPage"]:
            return None
        cursor = info["endCursor"]
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            self.snapshot.limitations.append("threads:invalid-cursor")
            return None
        seen.add(cursor)
        return cursor

    async def _thread(self, node: dict[str, Any]) -> None:
        pull = node["pullRequest"]
        if (pull["number"] != self.snapshot.pr_number
                or pull["repository"]["nameWithOwner"].casefold() != self.snapshot.repo.casefold()):
            self.snapshot.limitations.append("thread:identity-mismatch")
            return
        comments = []
        seen: set[str] = set()
        ids: set[int] = set()
        complete = True
        page = node["comments"]
        while True:
            for raw in page["nodes"]:
                if raw is None or raw["databaseId"] in ids:
                    complete = False
                    continue
                ids.add(raw["databaseId"])
                author = raw["author"]
                comments.append(ReviewComment(
                    github_id=raw["databaseId"], node_id=raw["id"], body=raw["body"],
                    updated_at=raw["updatedAt"], author=author["login"] if author else "deleted-user",
                    is_bot=bool(author and author["__typename"] == "Bot"),
                    reply_to_id=raw["replyTo"]["databaseId"] if raw["replyTo"] else None,
                    original_commit_id=raw["originalCommit"]["oid"] if raw["originalCommit"] else None,
                ))
            cursor = self._cursor(page, seen)
            if cursor is None:
                break
            data = await self._read(_THREAD, {"id": node["id"], "cursor": cursor})
            follow = data.get("node") if data else None
            if (not follow or follow["id"] != node["id"]
                    or follow["pullRequest"] != pull):
                complete = False
                break
            page = follow["comments"]
        self.snapshot.threads.append(ReviewThread(
            thread_id=node["id"], repo=self.snapshot.repo, pr_number=self.snapshot.pr_number,
            path=node["path"], resolved=node["isResolved"], outdated=node["isOutdated"],
            comments=comments, complete=complete and bool(comments),
        ))

    async def one(self, thread_id: str) -> ReviewThreads:
        data = await self._read(_THREAD, {"id": thread_id, "cursor": None})
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
                "pr": self.snapshot.pr_number, "cursor": cursor})
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
