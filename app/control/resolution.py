"""Plan per-thread explanations and conservative whole-thread completion from frozen feedback."""

from dataclasses import dataclass, field

from app.github_models import ReviewComment, ReviewThread
from app.models import FeedbackRef, Flow, ReviewItemKind
from app.protocol import RevisionResult
from app.result_models import CommentEffect, Effect, ResolveEffect, ThreadReplyEffect
from app.review_models import ReviewBatch, ReviewFeedback


def matches(comment: ReviewComment, feedback: ReviewFeedback) -> bool:
    """Never substitute a review/conversation ID or a newer content version for inline feedback."""
    return (feedback.kind is ReviewItemKind.INLINE and not feedback.deleted
            and comment.github_id == feedback.github_id and comment.author == feedback.author
            and comment.body == feedback.body and comment.updated_at == feedback.updated_at)


def covered_thread(thread: ReviewThread, addressed: list[ReviewFeedback]) -> bool:
    """Every human comment is an unresolved input unless a verified revision addressed its version."""
    humans = [comment for comment in thread.comments if not comment.is_bot]
    return thread.complete and bool(humans) and all(
        any(matches(comment, item) for item in addressed) for comment in humans
    )


def feedback_link(repo: str, number: int, kind: ReviewItemKind, github_id: int) -> str:
    anchor = {ReviewItemKind.INLINE: "discussion_r", ReviewItemKind.REVIEW_BODY: "pullrequestreview-",
              ReviewItemKind.CONVERSATION: "issuecomment-"}[kind]
    return f"https://github.com/{repo}/pull/{number}#{anchor}{github_id}"


@dataclass
class ResolutionNotes:
    effects: list[Effect] = field(default_factory=list)
    blocked: list[FeedbackRef] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def plan_resolution(
    flow: Flow, number: int, batch: ReviewBatch, result: RevisionResult,
    historical: list[ReviewFeedback],
) -> ResolutionNotes:
    """Freeze one reply and one resolution check per mapped thread; keep unmapped feedback pending."""
    assert flow.installation_id is not None and result.output is not None
    notes = ResolutionNotes()
    members = {(f.item_id, f.version): f for f in batch.feedback}
    addressed = [members[(f.item_id, f.version)] for f in result.output.addressed]
    reported = {(f.item_id, f.version): f.note for f in result.output.addressed}
    reported.update({(f.item_id, f.version): "暂缓：" + f.reason for f in result.output.deferred})
    groups: dict[str, list[ReviewFeedback]] = {}
    mapped: dict[str, ReviewThread] = {}
    conversation = []
    threads = batch.threads
    for item in batch.feedback:
        text = reported.get((item.item_id, item.version))
        link = feedback_link(flow.repo, number, item.kind, item.github_id)
        if item.kind is not ReviewItemKind.INLINE:
            if text is not None:
                conversation.append(f"[{item.kind.value} {item.github_id}]({link})：{text}")
            continue
        candidates = [t for t in threads.threads if any(matches(c, item) for c in t.comments)] if (
            threads and threads.complete and threads.repo == flow.repo and threads.pr_number == number
        ) else []
        if len(candidates) != 1:
            reason = f"[{item.github_id}]({link})：线程映射缺失、不完整或评论版本已变，保留待处理。"
            notes.problems.append(reason)
            notes.blocked.append(FeedbackRef(item_id=item.item_id, version=item.version))
            conversation.append(f"{reason}\n{text or ''}")
            continue
        thread = candidates[0]
        mapped[thread.thread_id] = thread
        groups.setdefault(thread.thread_id, []).append(item)
    questions = [f"❓ {q}" for q in result.output.needs_clarification]
    associated = (len(groups) == 1 and not notes.blocked
                  and all(f.kind is ReviewItemKind.INLINE for f in batch.feedback))
    if not associated:
        conversation.extend(questions)
    coverage_by_comment: dict[int, list[ReviewFeedback]] = {}
    for item in [*historical, *addressed]:
        if item.kind is ReviewItemKind.INLINE:
            coverage_by_comment.setdefault(item.github_id, []).append(item)
    commit = f"已核验提交：[ `{result.commit_sha}` ](https://github.com/{flow.repo}/commit/{result.commit_sha})"
    for thread_id, items in groups.items():
        thread = mapped[thread_id]
        roots = [c for c in thread.comments if c.reply_to_id is None]
        lines = [f"#{f.github_id}：{reported[(f.item_id, f.version)]}" for f in items
                 if (f.item_id, f.version) in reported]
        if associated:
            lines.extend(questions)
        reply_key = f"thread-note:{thread_id}" if lines else None
        if lines and len(roots) == 1:
            assert reply_key is not None
            notes.effects.append(ThreadReplyEffect(
                installation_id=flow.installation_id, repo=flow.repo, key=reply_key, number=number, thread_id=thread_id,
                root_comment_id=roots[0].github_id, body="\n\n".join([*lines, commit]),
                feedback_refs=[FeedbackRef(item_id=f.item_id, version=f.version) for f in items],
            ))
        elif lines:
            reason = f"{thread_id}：无法确认根评论，保留待处理。"
            notes.problems.append(reason)
            notes.blocked.extend(FeedbackRef(item_id=f.item_id, version=f.version) for f in items)
            conversation.append(reason)
            reply_key = None
        coverage = [item for comment in thread.comments
                    for item in coverage_by_comment.get(comment.github_id, []) if matches(comment, item)]
        notes.effects.append(ResolveEffect(
            installation_id=flow.installation_id, repo=flow.repo, key=f"resolve:{thread_id}", number=number, thread_id=thread_id,
            snapshot=thread, covered=coverage, reply_key=reply_key,
            allow_resolve=not questions and covered_thread(thread, coverage),
        ))
    if conversation:
        notes.effects.insert(0, CommentEffect(installation_id=flow.installation_id, repo=flow.repo, key="feedback-notes", number=number,
            body="\n\n".join([*conversation, commit])))
    return notes
