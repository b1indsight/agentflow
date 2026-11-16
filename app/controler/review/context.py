"""Fetch feedback-related PR evidence before control freezes a review batch."""

from __future__ import annotations

from app.domain.context_models import (
    MAX_CONTEXT_BYTES,
    MAX_DISCUSSIONS,
    SOURCE_CHARS,
    ContextDiscussion,
    ReviewContext,
)
from app.domain.models import Flow, ReviewItemKind
from app.domain.review_models import ReviewBatch
from app.github import PullRequest, Repository, ReviewThread
from app.github.threads import MAX_THREAD_BYTES, MAX_THREAD_REQUESTS


async def _related_discussions(
    pr: PullRequest, batch: ReviewBatch, limitations: list[str],
) -> list[ContextDiscussion]:
    """Locate inline feedback by source ID, then retain only its thread's comments.

    Thread pages include the first comment page. Inspect all those identities before
    spending the shared read budget on unrelated continuations. Frozen mappings avoid
    the scan entirely; neither a different source's equal ID nor a newer edit replaces
    frozen feedback. All network reads keep the pre-Assignment, no-auto-retry boundary.
    """
    wanted = {item.github_id for item in batch.feedback
              if item.kind is ReviewItemKind.INLINE and item.updated_at <= batch.created_at}
    if not wanted:
        return []
    pending = wanted.copy()
    candidates: dict[str, ReviewThread] = {}
    selected: set[str] = set()
    requests = 0
    read_bytes = 0

    def within_budget() -> bool:
        return requests < MAX_THREAD_REQUESTS and read_bytes < MAX_THREAD_BYTES

    def remember(thread: ReviewThread) -> None:
        if (thread.repo.casefold() != pr.repo.casefold() or thread.pr_number != pr.number
                or "comments" not in thread.model_fields_set):
            return
        candidates[thread.thread_id] = thread
        matches = wanted.intersection(comment.github_id for comment in thread.comments.items)
        if matches:
            selected.add(thread.thread_id)
            pending.difference_update(matches)

    saved = batch.threads
    if saved and saved.repo.casefold() == pr.repo.casefold() and saved.pr_number == pr.number:
        for thread in saved.threads:
            remember(thread)

    cursor = None
    seen_cursors: set[str] = set()
    while pending and within_budget():
        thread_page = await pr.fetch_threads(cursor=cursor, limit=100)
        requests += 1
        read_bytes += len(thread_page.model_dump_json().encode())
        if read_bytes > MAX_THREAD_BYTES:
            break
        for thread in thread_page.items:
            remember(thread)
        cursor = thread_page.next_cursor
        if cursor is None or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)

    discussions: list[ContextDiscussion] = []
    # Selected threads go first so unknown, unrelated reply pages cannot exhaust
    # the budget needed to finish already located evidence.
    ordered = sorted(candidates.values(), key=lambda thread: thread.thread_id not in selected)
    for thread in ordered:
        if thread.thread_id not in selected and not pending:
            continue
        comments = list(thread.comments.items)
        ids = {comment.github_id for comment in comments}
        cursor = thread.comments.next_cursor
        seen_cursors = set()
        complete = "complete" in thread.model_fields_set and thread.complete
        problems = list(thread.limitations) if "limitations" in thread.model_fields_set else []
        while cursor is not None and within_budget():
            if cursor in seen_cursors:
                problems.append("comments:invalid-cursor")
                break
            seen_cursors.add(cursor)
            # Rebind persisted facts locally; stored resources have no live parent.
            bound = ReviewThread(pr, thread.thread_id)
            comment_page = await bound.fetch_comments(cursor=cursor, limit=20)
            requests += 1
            read_bytes += len(comment_page.model_dump_json().encode())
            if read_bytes > MAX_THREAD_BYTES:
                break
            for comment in comment_page.items:
                if comment.github_id in ids:
                    problems.append("comments:duplicate-identity")
                    continue
                ids.add(comment.github_id)
                comments.append(comment)
            cursor = comment_page.next_cursor
            complete = cursor is None
        matches = wanted.intersection(ids)
        if not matches:
            continue
        pending.difference_update(matches)
        if not complete or cursor is not None:
            problems.append("comments:incomplete")
        limitations.extend(f"thread:{thread.thread_id}:{problem}" for problem in problems)
        for comment in comments:
            if comment.github_id in wanted or comment.updated_at > batch.created_at:
                continue
            discussions.append(ContextDiscussion(
                kind="inline", github_id=comment.github_id, author=comment.author,
                body=comment.body, updated_at=comment.updated_at,
                path=thread.path if "path" in thread.model_fields_set else None,
                original_commit_id=comment.original_commit_id,
            ))
    # Missing mappings matter only while an actual frozen inline input is unlocated.
    # Unread unrelated pages and absent code snippets are not incomplete evidence.
    limitations.extend(f"inline:{github_id}:thread-unavailable" for github_id in sorted(pending))
    return discussions


async def build_review_context(
    repo: Repository, flow: Flow, batch: ReviewBatch, pr_number: int,
) -> ReviewContext:
    """Capture bounded current-PR facts, frozen feedback and its related threads.

    The PR is read on both sides of the other reads to detect moving head/base revisions.
    Original Issue/task bodies, full PR patches and unrelated discussion prefixes are
    deliberately not fetched. Existing ReviewContext fields remain for historical JSON.
    """
    if flow.installation_id is None:
        raise ValueError("review context requires a GitHub installation")
    limitations: list[str] = []

    def clip(value: str, source: str) -> str:
        if len(value) > SOURCE_CHARS:
            limitations.append(f"{source[:100]}:truncated")
        return value[:SOURCE_CHARS]

    # API failures belong to the pre-Assignment retry boundary, not a frozen partial context.
    pr = await repo.fetch_pull_request(pr_number)
    discussion = await _related_discussions(pr, batch, limitations)
    after = await pr.refetch()
    if (pr.head_sha, pr.base_sha, pr.state, pr.merged) != (
        after.head_sha, after.base_sha, after.state, after.merged,
    ):
        limitations.append("pull_request:changed-during-read")
    if pr.number != pr_number or pr.state != "open" or pr.merged:
        limitations.append("pull_request:not-current-open-pr")
    pr = pr.model_copy(update={"title": clip(pr.title, "pr-title"),
                               "body": clip(pr.body, "pr-body")})

    def bound_comment(item: ContextDiscussion) -> ContextDiscussion:
        source = f"{item.kind}:{item.github_id}"
        limitations.extend(f"{source}:{reason}" for reason in item.limitations)
        return item.model_copy(update={
            "body": clip(item.body, source),
            "author": clip(item.author, source),
            "path": clip(item.path, source) if item.path is not None else None,
            "original_commit_id": clip(item.original_commit_id, source)
                if item.original_commit_id is not None else None,
            "diff_hunk": clip(item.diff_hunk, source) if item.diff_hunk is not None else None,
        })

    frozen = [item for item in batch.feedback if item.updated_at <= batch.created_at]
    if len(frozen) > MAX_DISCUSSIONS:
        limitations.append("feedback:truncated")
    feedback = [bound_comment(ContextDiscussion(**item.model_dump(include={
        "kind", "github_id", "author", "body", "updated_at", "item_id", "version",
        "path", "original_commit_id", "diff_hunk",
    }))) for item in frozen[:MAX_DISCUSSIONS]]
    if len(discussion) > MAX_DISCUSSIONS:
        limitations.append("discussion:truncated")
    discussions = [bound_comment(item) for item in discussion[:MAX_DISCUSSIONS]]
    if len(batch.open_points) > MAX_DISCUSSIONS:
        limitations.append("open-points:truncated")
    snapshot = ReviewContext(
        batch_id=batch.id, repo=flow.repo, issue_number=flow.issue_number, pr_number=pr_number,
        pull_request=pr, discussions=discussions, feedback=feedback,
        open_points=[clip(point, "open-points") for point in batch.open_points[:MAX_DISCUSSIONS]],
        limitations=sorted(set(limitations)),
    )
    # JSON bytes, not character count, bound the wire/journal size even for multibyte discussion.
    if len(snapshot.model_dump_json().encode()) > MAX_CONTEXT_BYTES:
        snapshot.limitations.append("context:truncated")
        for collection in (snapshot.discussions, snapshot.feedback, snapshot.open_points):
            while collection and len(snapshot.model_dump_json().encode()) > MAX_CONTEXT_BYTES:
                collection.pop()
        if len(snapshot.model_dump_json().encode()) > MAX_CONTEXT_BYTES:
            # Even diagnostic metadata can be adversarially large; keep the durable batch as source.
            snapshot = snapshot.model_copy(update={
                "pull_request": None, "limitations": ["context:truncated"],
            })
    return snapshot
