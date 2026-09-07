"""Execute durable GitHub intents without holding a database transaction across network I/O."""

from __future__ import annotations

import hashlib
from datetime import UTC
from email.utils import parsedate_to_datetime
from uuid import uuid4

from app.control.thread_effects import has_pending_thread_feedback, resolve_thread, revision_current
from app.control.tools import FlowToolRejected, FlowToolService
from app.github_models import GitHubAdapterError, GitHubErrorKind
from app.models import TERMINAL_STATES, DeliveryState
from app.ports import GitHubClient
from app.protocol import FlowToolErrorKind
from app.result_models import (
    CommentEffect,
    EffectReceipt,
    EffectRecord,
    EffectState,
    LabelsEffect,
    PullRequestEffect,
    ResolveEffect,
    ResultPending,
    ResultPlan,
    ReviewersEffect,
    ThreadReplyEffect,
)
from app.review_models import BatchState
from app.store import Store


class ResultEffects:
    """Resume ordered effects; an uncertain non-idempotent write is reconciled, never guessed."""

    def __init__(self, store: Store, github: GitHubClient) -> None:
        self.store = store
        self.github = github

    async def run(self, plan: ResultPlan) -> str | None:
        """Return a permanent failure, or finish all effects; pending work raises ResultPending."""
        while True:
            with self.store.transaction() as tx:
                flow = tx.lock_workflow(plan.flow_id)
                if flow is None or flow.state in TERMINAL_STATES:
                    tx.results.skip_remaining(plan.job_id, "Flow is terminal")
                    return None
                if flow.current_attempt_id != plan.attempt_id:
                    tx.results.skip_remaining(plan.job_id, "result attempt is no longer current")
                    return "result attempt is no longer current"
                if plan.revision is not None and plan.resolution_version == 0:
                    raise ResultPending("pre-upgrade revision effects require manual reconciliation")
            records = self.store.results.list_effects(plan.job_id)
            unfinished = [
                r
                for r in records
                if r.state
                not in {
                    EffectState.SUCCEEDED,
                    EffectState.SKIPPED,
                }
            ]
            if not unfinished:
                return None
            if unfinished[0].state is EffectState.FAILED:
                return unfinished[0].last_error or "GitHub effect failed"
            record = self.store.results.claim(plan.job_id, uuid4().hex)
            if record is None:
                raise ResultPending("result effects are awaiting retry or reconciliation")
            uncertain = record.dispatched_at is not None
            try:
                receipt = await self._execute(plan, record, records)
            except ResultPending as exc:
                self.store.results.finish(
                    record,
                    EffectState.UNCERTAIN,
                    error=str(exc),
                    delay=self._delay(record),
                )
                raise
            except (GitHubAdapterError, FlowToolRejected) as exc:
                # An error while querying an earlier uncertain write cannot prove it was unsent.
                definite_rejection = (
                    isinstance(exc, GitHubAdapterError)
                    and exc.status_code is not None
                    and 400 <= exc.status_code < 500
                    and exc.status_code not in {408}
                )
                ambiguous = isinstance(exc, GitHubAdapterError) and (
                    uncertain or (record.dispatched_at is not None and not definite_rejection)
                )
                retryable = isinstance(exc, GitHubAdapterError) and exc.retryable
                if (isinstance(record.payload, (ThreadReplyEffect, ResolveEffect))
                        and not ambiguous and not retryable):
                    # A definite permission/deletion failure leaves a diagnostic and pending input;
                    # already delivered code must not trigger another Codex run to repair GitHub.
                    self.store.results.finish(record, EffectState.SUCCEEDED, receipt=EffectReceipt(
                        thread_id=record.payload.thread_id, pending_reason=str(exc),
                    ))
                    continue
                state = (
                    EffectState.UNCERTAIN
                    if ambiguous
                    else EffectState.RETRY
                    if retryable
                    else EffectState.FAILED
                )
                self.store.results.finish(
                    record,
                    state,
                    error=str(exc),
                    delay=self._delay(record, exc),
                    definitely_unsent=definite_rejection and not uncertain,
                )
                if state is EffectState.FAILED:
                    return str(exc)
                raise ResultPending(str(exc)) from exc
            else:
                self.store.results.finish(record, EffectState.SUCCEEDED, receipt=receipt)

    def _dispatch(self, plan: ResultPlan, record: EffectRecord) -> None:
        # Check cancellation again after remote reads, immediately before authorizing the write.
        with self.store.transaction() as tx:
            flow = tx.lock_workflow(plan.flow_id)
            job = tx.lock_job(plan.job_id)
            if (
                flow is None
                or flow.state in TERMINAL_STATES
                or job is None
                or job.result_applied_at is not None
                or job.result_digest != plan.digest
                or flow.current_attempt_id != plan.attempt_id
            ):
                raise ResultPending("Flow changed before GitHub dispatch")
            if plan.batch_id is not None:
                batch = tx.reviews.for_job(job)
                delivery = tx.get_delivery(flow.id)
                if (batch.id != plan.batch_id
                        or batch.state not in {BatchState.READY, BatchState.PROCESSING}
                        or delivery is None or delivery.state is not DeliveryState.OPEN):
                    raise FlowToolRejected(FlowToolErrorKind.CONFLICT,
                                           "review batch or PR is no longer active")
                if plan.delivery is not None and delivery != plan.delivery:
                    raise FlowToolRejected(FlowToolErrorKind.CONFLICT,
                                           "delivery changed before GitHub dispatch")
                if isinstance(record.payload, ResolveEffect) and has_pending_thread_feedback(
                    tx, plan, record.payload,
                ):
                    raise FlowToolRejected(FlowToolErrorKind.CONFLICT,
                                           "new feedback arrived before resolve dispatch")
            tx.results.mark_dispatched(record)

    async def _execute(
        self,
        plan: ResultPlan,
        record: EffectRecord,
        records: list[EffectRecord],
    ) -> EffectReceipt:
        effect = record.payload
        installation, repo = effect.installation_id, effect.repo
        if isinstance(effect, (CommentEffect, ThreadReplyEffect)):
            digest = hashlib.sha256(
                f"{plan.job_id}:{plan.digest}:{effect.key}".encode()
            ).hexdigest()
            marker = f"<!-- flowtool:effect:{digest} -->"
            if isinstance(effect, ThreadReplyEffect):
                found = await self.github.find_review_reply(
                    installation, repo, effect.number, effect.thread_id, marker,
                )
            else:
                found = await self.github.find_comment(installation, repo, effect.number, marker)
            if found is not None:
                return EffectReceipt(comment_id=found)
            if record.dispatched_at is not None:
                raise ResultPending("comment outcome is unknown; waiting for its remote marker")
            if isinstance(effect, ThreadReplyEffect):
                if not await revision_current(self.github, plan, effect.number):
                    return EffectReceipt(thread_id=effect.thread_id,
                                         pending_reason="PR 已关闭或 head 已变化，未发送线程说明")
                self._dispatch(plan, record)
                reply = await self.github.reply_review_thread(
                    installation, repo, effect.number, effect.root_comment_id,
                    f"{effect.body}\n\n{marker}",
                )
                return EffectReceipt(comment_id=reply, thread_id=effect.thread_id)
            if plan.batch_id is not None and effect.key in {"answer", "triage"}:
                job = self.store.get_job(plan.job_id)
                if job is None:
                    raise FlowToolRejected(FlowToolErrorKind.CONFLICT, "review job disappeared")
                batch = self.store.reviews.for_job(job)
                snapshot = batch.context.pull_request if batch.context is not None else None
                current = await self.github.get_review_pull_request(installation, repo, effect.number)
                if (snapshot is None or current.number != effect.number
                        or current.state != "open" or current.merged):
                    raise FlowToolRejected(FlowToolErrorKind.CONFLICT,
                                           "PR is no longer open for the Judge snapshot")
                if (current.head_sha != snapshot.head_sha or current.base_sha != snapshot.base_sha):
                    with self.store.transaction() as tx:
                        tx.lock_workflow(job.flow_id)
                        tx.reviews.mark_context_stale(job)
                    raise FlowToolRejected(FlowToolErrorKind.CONFLICT,
                                           "PR changed since the Judge snapshot; retry needs new context")
            self._dispatch(plan, record)
            comment_id = await self.github.comment(
                installation,
                repo,
                effect.number,
                f"{effect.body}\n\n{marker}",
            )
            return EffectReceipt(comment_id=comment_id)
        if isinstance(effect, PullRequestEffect):
            pull_request = await self.github.find_pull_request_by_head(
                installation,
                repo,
                effect.branch,
            )
            if pull_request is None:
                if record.dispatched_at is not None:
                    raise ResultPending("pull request outcome is unknown; waiting for its head")
                self._dispatch(plan, record)
                try:
                    pull_request = await self.github.open_pull_request(
                        installation,
                        repo,
                        head=effect.branch,
                        base=effect.base_branch,
                        title=effect.title,
                        body=effect.body,
                    )
                except GitHubAdapterError as exc:
                    if exc.kind not in {GitHubErrorKind.CONFLICT, GitHubErrorKind.VALIDATION}:
                        raise
                    pull_request = await self.github.find_pull_request_by_head(
                        installation,
                        repo,
                        effect.branch,
                    )
                    if pull_request is None:
                        raise
            FlowToolService.validate_pull_request(
                pull_request,
                effect.branch,
                effect.base_branch,
                effect.head_sha,
            )
            return EffectReceipt(pull_request=pull_request)
        if isinstance(effect, (LabelsEffect, ReviewersEffect)):
            number = effect.number
            if plan.revision is not None:
                assert isinstance(number, int)
                frozen = {(f.item_id, f.version) for f in plan.consumed}
                if (any(r.receipt and r.receipt.pending_reason for r in records)
                        or any((f.item_id, f.version) not in frozen
                               for f in self.store.reviews.pending(plan.flow_id))
                        or not await revision_current(self.github, plan, number)):
                    return EffectReceipt(pending_reason="仍有未决反馈或 PR 已变化，暂停 re-review")
            if number == "created_pr":
                receipts = [
                    r.receipt.pull_request
                    for r in records
                    if r.receipt is not None and r.receipt.pull_request is not None
                ]
                if len(receipts) != 1:
                    raise ValueError(
                        "PR-dependent effect requires exactly one persisted PR receipt"
                    )
                number = receipts[0].number
            if isinstance(effect, LabelsEffect):
                labels = await self.github.get_labels(installation, repo, number)
                if set(labels) != set(effect.labels):
                    self._dispatch(plan, record)
                    await self.github.set_labels(installation, repo, number, effect.labels)
            else:
                # Include completed reviews since dispatch so recovery does not request them again.
                satisfied = await self.github.get_reviewers(
                    installation,
                    repo,
                    number,
                    since=(record.dispatched_at or self.store.now()).replace(microsecond=0),
                )
                missing = sorted(set(effect.reviewers) - satisfied)
                if missing:
                    if record.dispatched_at is not None:
                        raise ResultPending(
                            "review request outcome is unknown; waiting for remote state"
                        )
                    self._dispatch(plan, record)
                    await self.github.request_reviewers(installation, repo, number, missing)
            return EffectReceipt()
        return await resolve_thread(self.store, self.github, plan, record, records, self._dispatch)

    def _delay(
        self,
        record: EffectRecord,
        error: GitHubAdapterError | FlowToolRejected | None = None,
    ) -> float:
        delay = min(300.0, 2.0 ** min(record.attempts, 8))
        if isinstance(error, GitHubAdapterError) and error.retry_after:
            try:
                requested = float(error.retry_after)
            except ValueError:
                try:
                    deadline = parsedate_to_datetime(error.retry_after)
                    requested = (deadline.astimezone(UTC) - self.store.now()).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    requested = 0.0
            delay = max(delay, requested)
        return delay
