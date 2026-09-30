"""Execute durable GitHub intents without holding a database transaction across network I/O."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from app.control.archive import verify_archive
from app.control.thread_effects import has_pending_thread_feedback, resolve_thread, revision_current
from app.control.tools import FlowToolRejected, FlowToolService
from app.github_models import GitHubAdapterError, GitHubErrorKind
from app.github_retry import retry_delay
from app.models import TERMINAL_STATES, DeliveryState, FlowState
from app.ports import GitHubClient
from app.protocol import FlowToolErrorKind
from app.result_models import (
    CommentEffect,
    EffectReceipt,
    EffectRecord,
    EffectState,
    LabelEffect,
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


class _PendingReviewFeedback(RuntimeError):
    """A notification became unnecessary while its remote prerequisites were being queried."""


def _pending_feedback(store: Store, plan: ResultPlan) -> bool:
    frozen = {(f.item_id, f.version) for f in plan.consumed}
    return any((f.item_id, f.version) not in frozen for f in store.reviews.pending(plan.flow_id))


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
                cancelled = flow is not None and flow.state is FlowState.CANCELLED
                if flow is None or (flow.state in TERMINAL_STATES and not cancelled):
                    tx.results.skip_remaining(plan.job_id, "Flow is terminal")
                    return None
                if flow.current_attempt_id != plan.attempt_id:
                    tx.results.skip_remaining(plan.job_id, "result attempt is no longer current")
                    return "result attempt is no longer current"
                job = tx.get_job(plan.job_id)
                if job is None:
                    raise ValueError("result job disappeared")
                if plan.archive is not None:
                    try:
                        verify_archive(tx, flow, job, plan.archive)
                    except ValueError as error:
                        raise ResultPending(str(error)) from error
                delivery = tx.get_delivery(plan.flow_id)
                retired = cancelled or (delivery is not None and delivery.supersedes(job.kind))
                if retired:
                    tx.results.retire_for_merge(plan.job_id)
                if not retired and plan.revision is not None and plan.resolution_version == 0:
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
                receipt = (await self._recover_retired(plan, record, records) if retired
                           else await self._execute(plan, record, records))
            except _PendingReviewFeedback as exc:
                self.store.results.finish(record, EffectState.SUCCEEDED,
                                          receipt=EffectReceipt(pending_reason=str(exc)))
            except ResultPending as exc:
                cause = exc.__cause__ if isinstance(exc.__cause__, GitHubAdapterError) else None
                self.store.results.finish(
                    record,
                    EffectState.UNCERTAIN,
                    error=str(exc),
                    delay=self._delay(record, cause),
                )
                raise
            except (GitHubAdapterError, FlowToolRejected) as exc:
                # An error while querying an earlier uncertain write cannot prove it was unsent.
                definite_rejection = (
                    isinstance(exc, GitHubAdapterError)
                    and (exc.definitely_unsent or (
                        exc.status_code is not None
                        and 400 <= exc.status_code < 500
                        and exc.status_code not in {408}
                    ))
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
            delivery = tx.get_delivery(flow.id)
            if plan.archive is not None:
                try:
                    verify_archive(tx, flow, job, plan.archive)
                except ValueError as error:
                    raise ResultPending(str(error)) from error
            if delivery is not None and delivery.supersedes(job.kind):
                raise FlowToolRejected(FlowToolErrorKind.CONFLICT, "PR closed or merged before GitHub dispatch")
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
                if (plan.revision is not None
                        and isinstance(record.payload, (LabelEffect, LabelsEffect, ReviewersEffect))
                        and _pending_feedback(tx, plan)):
                    raise _PendingReviewFeedback("出现新反馈，暂停 re-review")
            tx.results.mark_dispatched(record)

    @staticmethod
    def _comment_marker(plan: ResultPlan, effect: CommentEffect | ThreadReplyEffect) -> str:
        identity = (f"archive:{effect.repo}:{plan.flow_id}:{effect.key}" if plan.archive is not None
                    else f"{plan.job_id}:{plan.digest}:{effect.key}")
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return f"<!-- flowtool:effect:{digest} -->"

    async def _recover_retired(
        self, plan: ResultPlan, record: EffectRecord, records: list[EffectRecord],
    ) -> EffectReceipt:
        """Observe dispatched writes after merge/cancel; never reissue a mutation."""
        effect = record.payload
        installation, repo = effect.installation_id, effect.repo
        if isinstance(effect, (CommentEffect, ThreadReplyEffect)):
            # This path already reconciles by marker and forbids resending a dispatched comment.
            return await self._execute(plan, record, records)
        if isinstance(effect, PullRequestEffect):
            found = await self.github.find_pull_request_by_head(installation, repo, effect.branch)
            delivery = self.store.get_delivery(plan.flow_id)
            flow = self.store.get_flow(plan.flow_id)
            cancelled_opening = (flow is not None and flow.state is FlowState.CANCELLED
                                 and delivery is not None and delivery.state is DeliveryState.OPENING
                                 and delivery.pr_number is None)
            if (found is None or delivery is None or (not cancelled_opening and found.number != delivery.pr_number)
                    or found.head_branch != effect.branch or found.base_branch != effect.base_branch):
                raise ResultPending("retired PR creation awaits its remote identity")
            if cancelled_opening:
                from app.control.service import ControlService
                from app.github_models import PullRequestMerge

                remote = await self.github.get_pull_request(installation, repo, found.number)
                if (remote.repo != repo or remote.number != found.number or remote.head_repository != repo
                        or remote.head_branch != effect.branch or remote.base_branch != effect.base_branch
                        or remote.head_sha != effect.head_sha):
                    raise ResultPending("cancelled PR identity changed during recovery")
                with self.store.transaction() as tx:
                    fresh = tx.lock_workflow(plan.flow_id)
                    current = tx.get_delivery(plan.flow_id)
                    if (fresh is None or fresh.state is not FlowState.CANCELLED
                            or fresh.current_attempt_id != plan.attempt_id or current is None
                            or plan.delivery is None or current.version != plan.delivery.version
                            or current.state is not DeliveryState.OPENING or current.pr_number is not None
                            or current.branch != effect.branch or current.head_sha != effect.head_sha):
                        raise ResultPending("cancelled delivery changed during recovery")
                    current.pr_number = remote.number
                    current.url = f"https://github.com/{repo}/pull/{remote.number}"
                    if remote.merged:
                        assert remote.merged_at is not None and remote.merge_commit_sha is not None
                        ControlService.record_merge(tx, fresh, current, PullRequestMerge(
                            number=remote.number, merged=True, merged_at=remote.merged_at,
                            merge_commit_sha=remote.merge_commit_sha,
                        ))
                    else:
                        current.state = DeliveryState.CLOSED if remote.state == 'closed' else DeliveryState.OPEN
                        current.version += 1
                        tx.update_delivery(current)
                found = remote
            return EffectReceipt(pull_request=found)
        if isinstance(effect, ResolveEffect):
            resolved = await self.github.review_thread_resolved(installation, effect.thread_id)
            return EffectReceipt(thread_id=effect.thread_id, remote_resolved=resolved,
                                 pending_reason="PR merged; observed thread without resolving")
        number = effect.number
        if number == "created_pr":
            created = [r.receipt.pull_request for r in records
                       if r.receipt is not None and r.receipt.pull_request is not None]
            if len(created) != 1:
                raise ResultPending("merged PR notification awaits its creation receipt")
            number = created[0].number
        if isinstance(effect, (LabelsEffect, LabelEffect)):
            await self.github.get_labels(installation, repo, number)
        else:
            assert record.dispatched_at is not None
            await self.github.get_reviewers(installation, repo, number, since=record.dispatched_at)
        return EffectReceipt(pending_reason="PR merged; observed notification without reissuing it")

    async def _execute(
        self,
        plan: ResultPlan,
        record: EffectRecord,
        records: list[EffectRecord],
    ) -> EffectReceipt:
        effect = record.payload
        installation, repo = effect.installation_id, effect.repo
        if isinstance(effect, (CommentEffect, ThreadReplyEffect)):
            marker = self._comment_marker(plan, effect)
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
                current = await self.github.get_pull_request(installation, repo, effect.number)
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
            if pull_request.state == "closed":
                # PR creation may have succeeded before INITIAL's final local commit.
                # Bind its verified identity and retire intent instead of re-opening it.
                remote = await self.github.get_pull_request(installation, repo, pull_request.number)
                if (remote.repo != repo or remote.number != pull_request.number or remote.head_repository != repo
                        or remote.head_branch != effect.branch or remote.base_branch != effect.base_branch
                        or remote.head_sha != effect.head_sha):
                    raise FlowToolRejected(FlowToolErrorKind.CONFLICT, "closed INITIAL PR identity changed")
                if remote.state == "closed":
                    from app.control.lifecycle import pause_delivery
                    from app.control.service import ControlService
                    from app.github_models import PullRequestMerge

                    with self.store.transaction() as tx:
                        fresh = tx.lock_workflow(plan.flow_id)
                        current_delivery = tx.get_delivery(plan.flow_id)
                        if (fresh is None or current_delivery is None or plan.delivery is None
                                or fresh.current_attempt_id != plan.attempt_id
                                or current_delivery.version != plan.delivery.version
                                or current_delivery.head_sha != effect.head_sha
                                or current_delivery.pr_number not in {None, remote.number}):
                            raise ResultPending("INITIAL delivery changed during closed PR verification")
                        current_delivery.pr_number = remote.number
                        current_delivery.url = f"https://github.com/{repo}/pull/{remote.number}"
                        if remote.merged:
                            assert remote.merged_at is not None and remote.merge_commit_sha is not None
                            ControlService.record_merge(tx, fresh, current_delivery, PullRequestMerge(
                                number=remote.number, merged=True, merged_at=remote.merged_at,
                                merge_commit_sha=remote.merge_commit_sha,
                            ))
                        else:
                            pause_delivery(tx, current_delivery)
                    return EffectReceipt(pull_request=remote)
                pull_request = remote
            FlowToolService.validate_pull_request(
                pull_request,
                effect.branch,
                effect.base_branch,
                effect.head_sha,
            )
            return EffectReceipt(pull_request=pull_request)
        if isinstance(effect, (LabelEffect, LabelsEffect, ReviewersEffect)):
            number = effect.number
            if plan.revision is not None:
                assert isinstance(number, int)
                if (any(r.receipt and r.receipt.pending_reason for r in records)
                        or _pending_feedback(self.store, plan)
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
            if isinstance(effect, (LabelEffect, LabelsEffect)):
                labels = {label.casefold() for label in await self.github.get_labels(installation, repo, number)}
                # Old frozen list intents also own only their named status labels.
                desired = ([(effect.label, effect.present)] if isinstance(effect, LabelEffect)
                           else [(label, True) for label in effect.labels])
                for label, present in desired:
                    if (label.casefold() in labels) == present:
                        continue
                    self._dispatch(plan, record)
                    if present:
                        await self.github.add_label(installation, repo, number, label)
                        labels.add(label.casefold())
                    else:
                        await self.github.remove_label(installation, repo, number, label)
                        labels.discard(label.casefold())
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
        return retry_delay(record.attempts, self.store.now(),
                           error if isinstance(error, GitHubAdapterError) else None)
