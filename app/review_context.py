"""Fetch and bound PR evidence before control freezes a review batch for execution."""

from __future__ import annotations

from app.context_models import (
    MAX_CONTEXT_BYTES,
    MAX_DISCUSSIONS,
    MAX_FILES,
    SOURCE_CHARS,
    ContextDiscussion,
    ReviewContext,
)
from app.github_models import PullRequestDetails
from app.models import Flow
from app.ports import GitHubClient
from app.review_models import ReviewBatch


async def build_review_context(
    github: GitHubClient, flow: Flow, batch: ReviewBatch, pr_number: int,
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
    pr = await github.get_pull_request(installation, flow.repo, pr_number)
    issue = await github.get_issue(installation, flow.repo, flow.issue_number)
    files = await github.list_pull_request_files(installation, flow.repo, pr_number)
    discussion = await github.list_pull_request_discussions(installation, flow.repo, pr_number)
    after = await github.get_pull_request(installation, flow.repo, pr_number)
    if (pr.head_sha, pr.base_sha, pr.state, pr.merged) != (
        after.head_sha, after.base_sha, after.state, after.merged,
    ):
        limitations.append("pull_request:changed-during-read")
    if pr.number != pr_number or pr.state != "open" or pr.merged:
        limitations.append("pull_request:not-current-open-pr")
    if issue and issue.number != flow.issue_number:
        limitations.append("issue:identity-mismatch")
    pr = pr.model_copy(update={"title": clip(pr.title, "pr-title"),
                               "body": clip(pr.body, "pr-body")})
    if issue:
        issue = issue.model_copy(update={"title": clip(issue.title, "issue-title"),
                                         "body": clip(issue.body, "issue-body")})

    bounded_files = []
    if files:
        if files.truncated or len(files.files) > MAX_FILES:
            limitations.append("files:truncated")
        for item in files.files[:MAX_FILES]:
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
        if discussion.truncated:
            limitations.append("discussion:truncated")
        for comment in discussion.discussions:
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
        issue=issue, pull_request=PullRequestDetails.model_validate(pr.model_dump()), files=bounded_files,
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
