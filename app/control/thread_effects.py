"""Refresh remote thread evidence around mutations and retain unprocessed feedback for the next batch."""

from collections.abc import Callable

from app.control.resolution import covered_thread
from app.github_models import GitHubAdapterError, ReviewThreads
from app.models import TERMINAL_STATES, ReviewItemKind
from app.ports import GitHubClient
from app.result_models import EffectReceipt, EffectRecord, ResolveEffect, ResultPending, ResultPlan
from app.store import Store


def observe(store: Store, plan: ResultPlan, snapshot: ReviewThreads) -> None:
    """Persist observations separately from ACK; a crash must not lose webhook-missing feedback."""
    with store.transaction() as tx:
        flow = tx.lock_workflow(plan.flow_id)
        if (flow is None or flow.state in TERMINAL_STATES
                or flow.current_attempt_id != plan.attempt_id):
            raise ResultPending("Flow changed during thread refresh")
        assert plan.batch_id is not None
        tx.reviews.observe_threads(flow.id, snapshot, tx.reviews.get_batch(plan.batch_id).feedback)


async def revision_current(github: GitHubClient, plan: ResultPlan, number: int) -> bool:
    if plan.revision is None or plan.delivery is None:
        return False
    effect = plan.effects[0]
    current = await github.get_pull_request(effect.installation_id, effect.repo, number)
    return (current.number == number and current.state == "open" and not current.merged
            and current.head_sha == plan.revision.commit_sha
            and current.head_branch == plan.delivery.branch
            and current.base_branch == plan.delivery.base_branch
            and current.head_repository == effect.repo)


def has_pending_thread_feedback(store: Store, plan: ResultPlan, effect: ResolveEffect) -> bool:
    """Fence locally known updates too, including input whose thread mapping has not arrived yet."""
    assert plan.batch_id is not None
    batch = store.reviews.get_batch(plan.batch_id)
    other_ids = {c.github_id for t in batch.threads.threads if t.thread_id != effect.thread_id
                 for c in t.comments} if batch.threads else set()
    covered_refs = {(f.item_id, f.version) for f in effect.covered}
    return any(
        f.kind is ReviewItemKind.INLINE and f.github_id not in other_ids
        and (f.item_id, f.version) not in covered_refs for f in store.reviews.pending(plan.flow_id)
    )


async def resolve_thread(
    store: Store, github: GitHubClient, plan: ResultPlan, record: EffectRecord,
    records: list[EffectRecord], dispatch: Callable[[ResultPlan, EffectRecord], None],
) -> EffectReceipt:
    """A successful note and exact human coverage are required even on uncertain-write recovery."""
    effect = record.payload
    assert isinstance(effect, ResolveEffect)
    snapshot = await github.get_review_thread(
        effect.installation_id, effect.repo, effect.number, effect.thread_id,
    )
    observe(store, plan, snapshot)
    thread = next((t for t in snapshot.threads if t.thread_id == effect.thread_id), None)
    local_pending = has_pending_thread_feedback(store, plan, effect)
    reason = None
    receipt = EffectReceipt(thread_id=effect.thread_id,
                            remote_resolved=thread.resolved if thread else None)
    if not snapshot.complete or thread is None:
        reason = "线程数据缺失或不完整"
    elif local_pending or not effect.allow_resolve or not covered_thread(thread, effect.covered):
        reason = "线程仍有未处理反馈或新的评论版本"
    elif not any(r.effect_key == effect.reply_key and r.receipt is not None
                 and r.receipt.comment_id is not None and not r.receipt.pending_reason for r in records):
        reason = "线程处理说明尚未确认发送"
    elif not await revision_current(github, plan, effect.number):
        reason = "PR 已关闭或 head 已变化"
    if reason:
        return receipt.model_copy(update={"pending_reason": f"{effect.thread_id}：{reason}"})
    assert thread is not None
    if not thread.resolved:
        dispatch(plan, record)
        await github.resolve_review_thread(effect.installation_id, effect.thread_id)
        # GitHub has no conditional resolve. Retain raced comments without automatically reopening.
        try:
            after = await github.get_review_thread(
                effect.installation_id, effect.repo, effect.number, effect.thread_id,
            )
        except GitHubAdapterError as error:
            # A rejected confirmation read says nothing about the completed mutation.
            raise ResultPending(f"resolve confirmation is pending: {error}") from error
        observe(store, plan, after)
        current = next((t for t in after.threads if t.thread_id == effect.thread_id), None)
        if current is None or not after.complete:
            raise ResultPending("resolve outcome awaits a complete remote thread read")
        receipt = EffectReceipt(thread_id=effect.thread_id, remote_resolved=current.resolved)
        if not current.resolved:
            raise ResultPending("resolve outcome awaits remote confirmation")
        if not covered_thread(current, effect.covered):
            return receipt.model_copy(update={"pending_reason": f"{effect.thread_id}：resolve 期间出现新反馈"})
    return receipt.model_copy(update={"remote_resolved": True})
