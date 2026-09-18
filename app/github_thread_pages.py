"""One-request thread traversal with durable outer and nested comment continuations."""

import json
from datetime import UTC, datetime
from typing import Any

from githubkit.exception import GraphQLFailed
from pydantic import BaseModel, ConfigDict

from app.github_models import ReviewComment, ReviewThread, ReviewThreadPage, ReviewThreads
from app.github_threads import MAX_THREAD_BYTES

_FIELDS = """
id isResolved isOutdated path
pullRequest {number repository {nameWithOwner}}
comments(first:20,after:$comments){
  nodes {fullDatabaseId id body updatedAt author {login __typename}
         replyTo {fullDatabaseId} originalCommit {oid}}
  pageInfo {hasNextPage endCursor}
}
"""
_THREADS = """
query($owner:String!,$name:String!,$pr:Int!,$after:String,$comments:String){
repository(owner:$owner,name:$name){pullRequest(number:$pr){
reviewThreads(first:1,after:$after){nodes{
""" + _FIELDS + "} pageInfo {hasNextPage endCursor}}}}}"
_COMMENTS = "query($id:ID!,$comments:String){node(id:$id){... on PullRequestReviewThread{" + _FIELDS + "}}}"


class ThreadCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    after: str | None = None
    thread_id: str | None = None
    comments: str | None = None
    following: str | None = None


def next_cursor(page: dict[str, Any], previous: str | None) -> str | None:
    info = page["pageInfo"]
    if not isinstance(info["hasNextPage"], bool):
        raise TypeError("invalid page completeness")
    cursor = info["endCursor"] if info["hasNextPage"] else None
    if info["hasNextPage"] and (not isinstance(cursor, str) or not cursor or cursor == previous):
        raise ValueError("invalid thread cursor")
    return cursor


async def read_thread_page(client: Any, repo: str, pr: int, cursor: str | None) -> ReviewThreadPage:
    """Read resolved and unresolved threads alike; each invocation makes at most one request."""
    state = ThreadCursor.model_validate_json(cursor) if cursor else ThreadCursor()
    if bool(state.thread_id) != bool(state.comments):
        raise ValueError("invalid nested cursor")
    snapshot = ReviewThreads(repo=repo, pr_number=pr, fetched_at=datetime.now(UTC))
    owner, name = repo.split("/")
    try:
        data = await client.async_graphql(
            _COMMENTS if state.thread_id else _THREADS,
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
            snapshot.limitations.append("thread:missing-node")
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
        pull = node["pullRequest"]
        if pull["number"] != pr or pull["repository"]["nameWithOwner"].casefold() != repo.casefold():
            raise ValueError("thread does not belong to tracked PR")
        comments_page = node["comments"]
        comments_after = next_cursor(comments_page, state.comments)
        if len(comments_page["nodes"]) > 20:
            raise ValueError("invalid comment page size")
        comments = [ReviewComment(
            github_id=raw["fullDatabaseId"], node_id=raw["id"], body=raw["body"],
            updated_at=raw["updatedAt"], author=raw["author"]["login"] if raw["author"] else "deleted-user",
            is_bot=bool(raw["author"] and raw["author"]["__typename"] == "Bot"),
            reply_to_id=raw["replyTo"]["fullDatabaseId"] if raw["replyTo"] else None,
            original_commit_id=raw["originalCommit"]["oid"] if raw["originalCommit"] else None,
        ) for raw in comments_page["nodes"]]
        if len({comment.github_id for comment in comments}) != len(comments):
            raise ValueError("duplicate comment identity")
        snapshot.threads.append(ReviewThread(
            thread_id=node["id"], repo=repo, pr_number=pr, path=node["path"],
            resolved=node["isResolved"], outdated=node["isOutdated"], comments=comments,
            # Even the final nested page is a fragment, never a complete coverage snapshot.
            complete=bool(comments) and not comments_after and not state.thread_id,
        ))
        if comments_after:
            continuation = ThreadCursor(
                after=state.after, thread_id=node["id"], comments=comments_after, following=following,
            ).model_dump_json()
    return ReviewThreadPage(snapshot=snapshot, next_cursor=continuation)
