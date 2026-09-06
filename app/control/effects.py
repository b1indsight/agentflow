"""Execute durable GitHub intents without holding a database transaction across network I/O."""

from __future__ import annotations

import hashlib
from datetime import UTC
from email.utils import parsedate_to_datetime
from uuid import uuid4

from app.control.tools import FlowToolRejected, FlowToolService
from app.github_models import GitHubAdapterError, GitHubErrorKind
from app.models import TERMINAL_STATES
from app.ports import GitHubClient
from app.result_models import (
    CommentEffect,
    EffectReceipt,
    EffectRecord,
    EffectState,
    LabelsEffect,
    PullRequestEffect,
    ResultPending,
    ResultPlan,
    ReviewersEffect,
)
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
            tx.results.mark_dispatched(record)

    async def _execute(
        self,
        plan: ResultPlan,
        record: EffectRecord,
        records: list[EffectRecord],
    ) -> EffectReceipt:
        effect = record.payload
        installation, repo = effect.installation_id, effect.repo
        if isinstance(effect, CommentEffect):
            digest = hashlib.sha256(
                f"{plan.job_id}:{plan.digest}:{effect.key}".encode()
            ).hexdigest()
            marker = f"<!-- flowtool:effect:{digest} -->"
            found = await self.github.find_comment(installation, repo, effect.number, marker)
            if found is not None:
                return EffectReceipt(comment_id=found)
            if record.dispatched_at is not None:
                raise ResultPending("comment outcome is unknown; waiting for its remote marker")
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
        if not await self.github.review_thread_resolved(installation, effect.thread_id):
            self._dispatch(plan, record)
            await self.github.resolve_review_thread(installation, effect.thread_id)
        return EffectReceipt()

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
