"""Bounded read-only GitHub activity sources, including mutable-object rescans.

REST owns Issue/PR objects; GraphQL preserves review edit clocks and thread identity.
Every call reads one page, and nested replies/reactions get durable
child sources instead of unbounded embedded connections.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.github.errors import GitHubError, GitHubErrorKind, translate_errors
from app.github.repositories import Repository


class Observation(BaseModel):
    """A safe display version; stable keys agree across webhook and API ingestion."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    key: str
    source: str
    kind: str
    occurred_at: AwareDatetime
    updated_at: AwareDatetime
    author: str
    title: str = ""
    body: str = ""
    url: str | None = None
    parent_key: str | None = None
    deleted: bool = False
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)


class SourcePage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    items: list[Observation] = Field(default_factory=list, max_length=50)
    next_cursor: str | None = None
    limitations: list[str] = Field(default_factory=list)
    child_sources: list[str] = Field(default_factory=list)


def sources(pr_number: int | None) -> list[str]:
    """Supported top-level sources; child sources cover all collected comment reactions."""
    names = ["issue.description", "issue.comments", "issue.timeline", "issue.reactions"]
    if pr_number is not None:
        names += [
            "pr.description",
            "pr.comments",
            "pr.timeline",
            "pr.reactions",
            "pr.reviews",
            "pr.review_threads",
            "pr.commits",
            "pr.check_runs",
            "pr.statuses",
        ]
    return names


def _date(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("missing timestamp timezone")
    return parsed.astimezone(UTC)


def _url(value: Any, repo: str) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    return (
        value
        if (
            parsed.scheme == "https"
            and parsed.netloc == "github.com"
            and parsed.path.casefold().startswith(f"/{repo.casefold()}/")
        )
        else None
    )


def _key(repo: str, kind: str, node: dict) -> str:
    identity = node.get("node_id") or node.get("id") or node.get("sha")
    if identity is None and kind == "event" and node.get("event") == "cross-referenced":
        # GitHub omits an event ID here; source identity + occurrence survives source edits.
        source = node.get("source") or {}
        issue = source.get("issue") or {}
        reference = issue.get("node_id") or issue.get("id")
        if isinstance(reference, (str, int)) and not isinstance(reference, bool):
            identity = f"cross-referenced:{reference}:{_date(node['created_at']).isoformat()}"
    if not isinstance(identity, (str, int)) or isinstance(identity, bool):
        raise TypeError("missing source identity")
    return f"github:{repo.casefold()}:{kind}:{identity}"


def _object(
    repo: str, source: str, kind: str, node: dict, *, parent: str | None = None
) -> Observation:
    """Whitelist display facts; never archive SDK/auth/internal request envelopes."""
    occurred = _date(node.get("created_at") or node.get("submitted_at") or node.get("started_at"))
    updated = _date(node.get("updated_at") or node.get("completed_at") or occurred)
    user = node.get("user") or node.get("actor") or node.get("author") or {}
    metadata: dict[str, str | int | bool | None] = {}
    for field in (
        "state",
        "state_reason",
        "path",
        "line",
        "start_line",
        "original_line",
        "side",
        "commit_id",
        "original_commit_id",
        "in_reply_to_id",
        "pull_request_review_id",
        "event",
        "status",
        "conclusion",
        "head_sha",
        "content",
        "context",
        "merged",
        "locked",
    ):
        value = node.get(field)
        if isinstance(value, (str, int, bool)) or value is None:
            metadata[field] = value
    for field in ("label", "assignee", "requested_reviewer", "requested_team", "milestone"):
        value = node.get(field)
        if isinstance(value, dict):
            metadata[field] = (
                value.get("login") or value.get("name") or value.get("slug") or value.get("title")
            )
    hunk = node.get("diff_hunk")
    if isinstance(hunk, str):
        metadata.update(
            code_excerpt=hunk[:2000],
            code_characters=len(hunk),
            code_summary_truncated=len(hunk) > 2000,
        )
    body = node.get("body") or node.get("description")
    if kind == "check":
        output = node.get("output") or {}
        body = "\n\n".join(value for value in (output.get("summary"), output.get("text")) if value)
    return Observation(
        key=_key(repo, kind, node),
        source=source,
        kind=kind,
        occurred_at=occurred,
        updated_at=updated,
        author=user.get("login") or "deleted-user",
        title=node.get("title")
        or node.get("name")
        or node.get("event")
        or node.get("context")
        or "",
        body=body if isinstance(body, str) else "",
        url=_url(node.get("html_url") or node.get("target_url") or node.get("details_url"), repo),
        parent_key=parent,
        metadata=metadata,
    )


def _cursor(scope: list, position: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([scope, position]).encode()).decode()


def _position(cursor: str | None, scope: list) -> str | None:
    if cursor is None:
        return None
    try:
        actual, position = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if actual != scope or not isinstance(position, str) or not position:
            raise ValueError("foreign cursor")
        return position
    except (ValueError, TypeError) as error:
        raise ValueError("source cursor invalid") from error


_THREAD_FIELDS = """
id isResolved isOutdated path line startLine diffSide
pullRequest { number repository { nameWithOwner } }
comments(first:1) { nodes { id body createdAt updatedAt url author { login } } }
"""
_THREADS = (
    """query($owner:String!,$name:String!,$number:Int!,$limit:Int!,$cursor:String) {
 repository(owner:$owner,name:$name) { pullRequest(number:$number) {
 reviewThreads(first:$limit,after:$cursor) { nodes { """
    + _THREAD_FIELDS
    + """
 } pageInfo { hasNextPage endCursor } } } } }"""
)
_REPLIES = """query($id:ID!,$limit:Int!,$cursor:String) { node(id:$id) {
 ... on PullRequestReviewThread { pullRequest { number repository { nameWithOwner } }
 comments(first:$limit,after:$cursor) { nodes { id fullDatabaseId body createdAt updatedAt url
 author { login } replyTo { id } diffHunk commit { oid } path line }
 pageInfo { hasNextPage endCursor } } } } }"""


async def _threads(
    repo: Repository, pr: int, source: str, native: str | None, limit: int
) -> tuple[list[Observation], str | None, list[str]]:
    client = repo.installation._client(auto_retry=False)
    thread = source.partition(":")[2] if ":" in source else None
    data = await client.async_graphql(
        _REPLIES if thread else _THREADS,
        {"id": thread, "limit": min(limit, 2), "cursor": native}
        if thread
        else {
            "owner": repo.owner,
            "name": repo.name,
            "number": pr,
            "limit": limit,
            "cursor": native,
        },
    )
    if data.get("errors"):
        raise ValueError("partial thread query")
    target = data["node"] if thread else data["repository"]["pullRequest"]
    if thread and (
        target["pullRequest"]["number"] != pr
        or target["pullRequest"]["repository"]["nameWithOwner"].casefold()
        != repo.full_name.casefold()
    ):
        raise ValueError("foreign review thread")
    connection = target["comments" if thread else "reviewThreads"]
    info, nodes = connection["pageInfo"], connection["nodes"]
    following = info["endCursor"] if info["hasNextPage"] else None
    if info["hasNextPage"] and (not following or following == native):
        raise ValueError("thread cursor did not advance")
    items, children = [], []
    for node in nodes:
        if thread:
            native_node = {
                "id": node["id"],
                "body": node["body"],
                "created_at": node["createdAt"],
                "updated_at": node["updatedAt"],
                "html_url": node["url"],
                "user": node.get("author"),
                "path": node.get("path"),
                "line": node.get("line"),
                "diff_hunk": node.get("diffHunk"),
                "commit_id": node.get("commit", {}).get("oid"),
            }
            parent_key = f"github:{repo.full_name.casefold()}:thread:{thread}"
            item = _object(repo.full_name, source, "inline_comment", native_node, parent=parent_key)
            if node.get("fullDatabaseId"):
                children.append(f"inline.reactions:{node['fullDatabaseId']}")
        else:
            if (
                node["pullRequest"]["number"] != pr
                or node["pullRequest"]["repository"]["nameWithOwner"].casefold()
                != repo.full_name.casefold()
            ):
                raise ValueError("foreign review thread")
            first = node["comments"]["nodes"][0]
            item = Observation(
                key=f"github:{repo.full_name.casefold()}:thread:{node['id']}",
                source=source,
                kind="thread",
                occurred_at=_date(first["createdAt"]),
                updated_at=_date(first["updatedAt"]),
                author=(first.get("author") or {}).get("login") or "deleted-user",
                url=_url(first["url"], repo.full_name),
                metadata={
                    "path": node["path"],
                    "line": node.get("line"),
                    "resolved": node["isResolved"],
                    "outdated": node["isOutdated"],
                    "state_timestamp_unavailable": True,
                },
            )
            children.append(f"pr.review_thread_replies:{node['id']}")
        items.append(item)
    return items, following, children


_REVIEWS = """query($owner:String!,$name:String!,$number:Int!,$limit:Int!,$cursor:String) {
 repository(owner:$owner,name:$name) { pullRequest(number:$number) {
 reviews(first:$limit,after:$cursor) { nodes { id body createdAt updatedAt url state
 author { login } } pageInfo { hasNextPage endCursor } } } } }"""
_REVIEW_REACTIONS = """query($id:ID!,$limit:Int!,$cursor:String) { node(id:$id) {
 ... on PullRequestReview { pullRequest { number repository { nameWithOwner } }
 reactions(first:$limit,after:$cursor) { nodes { id content createdAt user { login } }
 pageInfo { hasNextPage endCursor } } } } }"""


async def _reviews(
    repo: Repository, pr: int, source: str, native: str | None, limit: int
) -> tuple[list[Observation], str | None, list[str]]:
    identity = source.partition(":")[2]
    client = repo.installation._client(auto_retry=False)
    data = await client.async_graphql(
        _REVIEW_REACTIONS if identity else _REVIEWS,
        {"id": identity, "limit": limit, "cursor": native}
        if identity
        else {
            "owner": repo.owner,
            "name": repo.name,
            "number": pr,
            "limit": min(limit, 2),
            "cursor": native,
        },
    )
    if data.get("errors"):
        raise ValueError("partial review connection")
    target = data["node"] if identity else data["repository"]["pullRequest"]
    if identity and (
        target["pullRequest"]["number"] != pr
        or target["pullRequest"]["repository"]["nameWithOwner"].casefold()
        != repo.full_name.casefold()
    ):
        raise ValueError("foreign review")
    connection = target["reactions" if identity else "reviews"]
    info = connection["pageInfo"]
    following = info["endCursor"] if info["hasNextPage"] else None
    if info["hasNextPage"] and (not following or following == native):
        raise ValueError("nonadvancing review cursor")
    items, children = [], []
    for node in connection["nodes"]:
        if identity:
            native_node = {
                "id": node["id"],
                "content": node["content"],
                "created_at": node["createdAt"],
                "user": node.get("user"),
            }
            item = _object(repo.full_name, source, "reaction", native_node)
        else:
            native_node = {
                "id": node["id"],
                "body": node["body"],
                "created_at": node["createdAt"],
                "updated_at": node["updatedAt"],
                "html_url": node["url"],
                "user": node.get("author"),
                "state": node["state"],
            }
            item = _object(repo.full_name, source, "review", native_node)
            children.append(f"review.reactions:{node['id']}")
        items.append(item)
    return items, following, children


async def read_source(
    repo: Repository,
    issue_number: int,
    pr_number: int | None,
    source: str,
    cursor: str | None = None,
    limit: int = 50,
) -> SourcePage:
    """Fetch one bounded connection page; cursor binds repository, target, source and size."""
    if not 1 <= limit <= 50:
        raise ValueError("invalid page size")
    child = re.fullmatch(
        r"(comment.reactions|inline.reactions):([1-9][0-9]*)|(commit.check_runs|commit.statuses):([0-9a-f]{40,64})|(pr.review_thread_replies|review.reactions):([A-Za-z0-9_=-]{1,256})",
        source,
    )
    if source not in sources(pr_number) and not child:
        raise ValueError("unsupported source")
    scope = [repo.full_name.casefold(), issue_number, pr_number, source, limit]
    native = _position(cursor, scope)
    client = repo.installation._client(auto_retry=False)
    owner, name = repo.owner, repo.name
    # Two maximal GitHub discussion bodies still fit below the source-byte budget.
    size = min(limit, 2)
    limitations: list[str] = []
    children: list[str] = []
    with translate_errors("console_source"):
        if source in ("pr.check_runs", "pr.statuses"):
            assert pr_number is not None
            pull = await client.rest.pulls.async_get(owner, name, pr_number)
            # A moving PR head must never retarget a continuation midway through a page set.
            family = source.removeprefix("pr.")
            return SourcePage(child_sources=[f"commit.{family}:{pull.parsed_data.head.sha}"])
        if source == "pr.review_threads" or source.startswith("pr.review_thread_replies:"):
            if pr_number is None:
                raise ValueError("missing PR")
            items, following, children = await _threads(repo, pr_number, source, native, size)
            limitations = ["thread_state_has_no_remote_change_timestamp"]
        elif source == "pr.reviews" or source.startswith("review.reactions:"):
            if pr_number is None:
                raise ValueError("missing PR")
            items, following, children = await _reviews(repo, pr_number, source, native, size)
        else:
            page = int(native) if native else 1
            if page < 1:
                raise ValueError("invalid page")
            number = pr_number if source.startswith("pr.") else issue_number
            if number is None:
                raise ValueError("missing PR")
            kind, parent = "", None
            if source.endswith("description"):
                response = await client.rest.issues.async_get(owner, name, number)
                nodes = [response.parsed_data.model_dump(mode="json")]
                kind = "description"
                following = None
                limitations = ["pre_ingestion_edits_and_deleted_bodies_unrecoverable"]
            elif source.endswith("comments"):
                response_comments = await client.rest.issues.async_list_comments(
                    owner, name, number, per_page=size, page=page
                )
                comments: list[BaseModel] = response_comments.parsed_data
                nodes = [n.model_dump(mode="json") for n in comments]
                kind = "comment"
            elif source.endswith("timeline"):
                response_timeline = await client.rest.issues.async_list_events_for_timeline(
                    owner, name, number, per_page=size, page=page
                )
                events: list[BaseModel] = response_timeline.parsed_data
                nodes = [n.model_dump(mode="json") for n in events]
                kind = "event"
            elif source == "pr.commits":
                response_commits = await client.rest.pulls.async_list_commits(
                    owner, name, number, per_page=size, page=page
                )
                commits: list[BaseModel] = response_commits.parsed_data
                nodes = [n.model_dump(mode="json") for n in commits]
                kind = "commit"
                limitations = ["github_pr_commit_list_maximum_250"]
            elif source.endswith("reactions") or source.startswith(
                ("comment.reactions:", "inline.reactions:")
            ):
                if source.startswith("comment.reactions:"):
                    reactions = await client.rest.reactions.async_list_for_issue_comment(
                        owner, name, int(source.partition(":")[2]), per_page=size, page=page
                    )
                elif source.startswith("inline.reactions:"):
                    reactions = (
                        await client.rest.reactions.async_list_for_pull_request_review_comment(
                            owner, name, int(source.partition(":")[2]), per_page=size, page=page
                        )
                    )
                else:
                    reactions = await client.rest.reactions.async_list_for_issue(
                        owner, name, number, per_page=size, page=page
                    )
                nodes = [n.model_dump(mode="json") for n in reactions.parsed_data]
                kind = "reaction"
            elif source.startswith(("commit.check_runs:", "commit.statuses:")):
                sha = source.partition(":")[2]
                if "check_runs" in source:
                    checks = await client.rest.checks.async_list_for_ref(
                        owner, name, sha, filter_="all", per_page=size, page=page
                    )
                    nodes = [n.model_dump(mode="json") for n in checks.parsed_data.check_runs]
                    kind = "check"
                else:
                    statuses = await client.rest.repos.async_list_commit_statuses_for_ref(
                        owner, name, sha, per_page=size, page=page
                    )
                    nodes = [n.model_dump(mode="json") for n in statuses.parsed_data]
                    kind = "status"
                limitations = ["expired_remote_logs_unrecoverable"]
            else:
                raise ValueError("unsupported source")
            if not source.endswith("description"):
                following = str(page + 1) if len(nodes) == size else None
            items = []
            for node in nodes:
                if kind == "event" and node.get("event") in (
                    "commented",
                    "reviewed",
                    "committed",
                    "line-commented",
                ):
                    # Canonical comment/review/commit sources own these identities and bodies.
                    continue
                if kind == "commit":
                    commit = node["commit"]
                    node = {
                        **node,
                        "created_at": commit["committer"]["date"],
                        "body": commit["message"],
                    }
                    children.extend(
                        (f"commit.check_runs:{node['sha']}", f"commit.statuses:{node['sha']}")
                    )
                if kind == "comment":
                    children.append(f"comment.reactions:{node['id']}")
                if kind == "review" and not node.get("submitted_at"):
                    limitations.append("pending_review_timestamp_unavailable")
                    continue
                if not (
                    node.get("created_at") or node.get("submitted_at") or node.get("started_at")
                ):
                    limitations.append("source_event_timestamp_unavailable")
                    continue
                item = _object(repo.full_name, source, kind, node, parent=parent)
                items.append(item)
    if "reactions" in source:
        limitations.append("reaction_removals_have_no_webhook_or_revision_clock")
    result = SourcePage(
        items=items,
        next_cursor=_cursor(scope, following) if following else None,
        limitations=sorted(set(limitations)),
        child_sources=children,
    )
    if len(result.model_dump_json().encode()) > 1_048_576:
        raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, "console_source_bytes")
    return result


def webhook_observations(event_type: str, payload: dict) -> list[Observation]:
    """Archive only explicit observations; remote absence/404 never means deletion."""
    repo = (payload.get("repository") or {}).get("full_name")
    if not isinstance(repo, str):
        return []
    action = payload.get("action")
    target: dict = {}
    kind, source = "", "webhook"
    if event_type in ("issue_comment", "pull_request_review_comment"):
        target = payload.get("comment") or {}
        kind = "comment" if event_type == "issue_comment" else "inline_comment"
        source = (
            "pr.comments" if (payload.get("issue") or {}).get("pull_request") else "issue.comments"
        )
    elif event_type == "pull_request_review":
        target, kind, source = payload.get("review") or {}, "review", "pr.reviews"
    elif event_type in ("issues", "pull_request"):
        target = payload.get("issue" if event_type == "issues" else "pull_request") or {}
        kind, source = (
            "description",
            "issue.description" if event_type == "issues" else "pr.description",
        )
    elif event_type in ("check_run", "status"):
        target, kind = (
            payload.get("check_run") or payload,
            "check" if event_type == "check_run" else "status",
        )
    else:
        return []
    if (
        action != "deleted"
        and kind in ("comment", "inline_comment", "review", "description")
        and "body" not in target
    ):
        return []
    try:
        item = _object(repo, source, kind, target)
    except (ValueError, TypeError, KeyError):
        return []
    if action == "deleted":
        # GitHub IDs cannot be resurrected. Keep its remote timestamp, with a separate
        # collection timestamp; a repeated delete delivery remains idempotent.
        item = item.model_copy(
            update={
                "deleted": True,
                "metadata": {**item.metadata, "deletion_time_unavailable": True},
            }
        )
    if event_type in ("issues", "pull_request") and action not in ("edited", "opened", "deleted"):
        actor = (payload.get("sender") or {}).get("login") or "deleted-user"
        meta: dict[str, str | int | bool | None] = {
            **item.metadata,
            "action": str(action),
            "actor": actor,
        }
        for field in ("label", "assignee", "requested_reviewer", "requested_team"):
            value = payload.get(field) or {}
            meta[field] = value.get("login") or value.get("name") or value.get("slug")
        # The canonical timeline source owns the action's event identity. Preserve the
        # observed payload on its resource version, not a second delivery-shaped event.
        item = item.model_copy(update={"metadata": meta})
    return [item]
