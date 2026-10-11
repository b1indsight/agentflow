"""Fetch and bound PR evidence before control freezes a review batch for execution."""

from __future__ import annotations

from app.domain.context_models import (
    MAX_CONTEXT_BYTES,
    MAX_DISCUSSIONS,
    MAX_FILES,
    SOURCE_CHARS,
    ContextDiscussion,
    ContextFile,
    ReviewContext,
)
from app.domain.models import Flow
from app.domain.review_models import ReviewBatch
from app.github import Repository


async def build_review_context(
    repo: Repository, flow: Flow, batch: ReviewBatch, pr_number: int,
) -> ReviewContext:
    """Capture bounded evidence and explicit incompleteness; callers persist once after network I/O.

    The PR is read on both sides of the other reads to detect moving head/base revisions.
    Feedback from after the batch cutoff never joins this already settled decision.
    """
    installation = flow.installation_id
    if installation is None:
        raise ValueError("review context requires a GitHub installation")
    limitations: list[str] = []

    def clip(value: str, source: str) -> str:
        if len(value) > SOURCE_CHARS:
            limitations.append(f"{source[:100]}:truncated")
        return value[:SOURCE_CHARS]

    # API failures belong to the pre-Assignment retry boundary, not a frozen partial context.
    pr = await repo.fetch_pull_request(pr_number)
    issue = await repo.fetch_issue(flow.issue_number)
    files: list[ContextFile] = []
    files_truncated = False
    async for item in pr.list_files():
        if len(files) == MAX_FILES:
            files_truncated = True
            break
        patch = item.patch
        file_limitations = []
        if patch is None:
            file_limitations.append("patch:unavailable-or-binary")
        elif len(patch) > SOURCE_CHARS:
            file_limitations.append("patch:truncated")
        files.append(ContextFile(
            path=item.filename, status=item.status,
            patch=patch[:SOURCE_CHARS] if patch is not None else None,
            previous_path=item.previous_filename, limitations=file_limitations,
        ))

    discussion: list[ContextDiscussion] = []
    discussion_truncated = False
    for kind in ("conversation", "review_body", "inline"):
        count = 0
        async for item in pr.list_discussions(kind):
            if count == MAX_DISCUSSIONS // 3:
                discussion_truncated = True
                break
            body = item.body or ""
            hunk = item.diff_hunk if kind == "inline" else None
            discussion_limitations = []
            if len(body) > SOURCE_CHARS:
                discussion_limitations.append("body:truncated")
            if hunk and len(hunk) > SOURCE_CHARS:
                discussion_limitations.append("diff_hunk:truncated")
            discussion.append(ContextDiscussion(
                kind=kind, github_id=item.id,
                author=item.user.login if item.user else "[deleted]",
                body=body[:SOURCE_CHARS],
                updated_at=item.submitted_at if kind == "review_body" else item.updated_at,
                path=item.path if kind == "inline" else None,
                original_commit_id=item.original_commit_id if kind == "inline" else None,
                diff_hunk=hunk[:SOURCE_CHARS] if hunk else hunk, limitations=discussion_limitations,
            ))
            count += 1
    after = await pr.refetch()
    if (pr.head_sha, pr.base_sha, pr.state, pr.merged) != (
        after.head_sha, after.base_sha, after.state, after.merged,
    ):
        limitations.append("pull_request:changed-during-read")
    if pr.number != pr_number or pr.state != "open" or pr.merged:
        limitations.append("pull_request:not-current-open-pr")
    if issue.number != flow.issue_number:
        limitations.append("issue:identity-mismatch")
    pr = pr.model_copy(update={"title": clip(pr.title, "pr-title"),
                               "body": clip(pr.body, "pr-body")})
    issue = issue.model_copy(update={"title": clip(issue.title, "issue-title"),
                                     "body": clip(issue.body, "issue-body")})

    bounded_files = []
    if files:
        if files_truncated or len(files) > MAX_FILES:
            limitations.append("files:truncated")
        for item in files[:MAX_FILES]:
            limitations.extend(f"file:{item.path}:{reason}" for reason in item.limitations)
            if item.patch is None:
                limitations.append(f"patch:{item.path}:unavailable-or-binary")
            bounded_files.append(item.model_copy(update={
                "patch": clip(item.patch, f"patch:{item.path}") if item.patch is not None else None,
            }))

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

    if len(batch.feedback) > MAX_DISCUSSIONS:
        limitations.append("feedback:truncated")
    feedback = [bound_comment(ContextDiscussion(**item.model_dump(include={
        "kind", "github_id", "author", "body", "updated_at", "item_id", "version",
        "path", "original_commit_id", "diff_hunk",
    }))) for item in batch.feedback[:MAX_DISCUSSIONS]]
    frozen_ids = {(item.kind, item.github_id) for item in batch.feedback}
    discussions = []
    if discussion:
        if discussion_truncated:
            limitations.append("discussion:truncated")
        for comment in discussion:
            if (comment.kind, comment.github_id) in frozen_ids:
                continue
            if comment.updated_at is None:
                limitations.append(f"discussion:{comment.github_id}:missing-time")
                continue
            if comment.updated_at > batch.created_at:
                continue
            discussions.append(bound_comment(comment))
    if len(discussions) > MAX_DISCUSSIONS:
        limitations.append("discussion:truncated")
    if len(batch.open_points) > MAX_DISCUSSIONS:
        limitations.append("open-points:truncated")
    snapshot = ReviewContext(
        batch_id=batch.id, repo=flow.repo, issue_number=flow.issue_number, pr_number=pr_number,
        issue=issue, pull_request=pr, files=bounded_files,
        discussions=discussions[:MAX_DISCUSSIONS], feedback=feedback,
        open_points=[clip(point, "open-points") for point in batch.open_points[:MAX_DISCUSSIONS]],
        limitations=sorted(set(limitations)),
    )
    # JSON bytes, not character count, bound the wire/journal size even for multibyte discussion.
    if len(snapshot.model_dump_json().encode()) > MAX_CONTEXT_BYTES:
        snapshot.limitations.append("context:truncated")
        for collection in (snapshot.files, snapshot.discussions, snapshot.feedback,
                           snapshot.open_points):
            while collection and len(snapshot.model_dump_json().encode()) > MAX_CONTEXT_BYTES:
                collection.pop()
        if len(snapshot.model_dump_json().encode()) > MAX_CONTEXT_BYTES:
            # Even diagnostic metadata can be adversarially large; keep the durable batch as source.
            snapshot = snapshot.model_copy(update={
                "issue": None, "pull_request": None, "limitations": ["context:truncated"],
            })
    return snapshot
