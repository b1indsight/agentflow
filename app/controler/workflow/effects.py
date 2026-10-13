"""Execute durable GitHub intents without holding a database transaction across network I/O."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from app.contracts.result_models import (
    CommentEffect,
    EffectReceipt,
    EffectRecord,
    EffectState,
    LabelEffect,
    PullRequestEffect,
    ResolveEffect,
    ResultPending,
    ResultPlan,
    ReviewersEffect,
    ThreadReplyEffect,
    unique_pull_request_receipt,
)
from app.controler.workflow.archive import verify_archive
from app.controler.workflow.lifecycle import (
    DeliveryConflict,
    adopt_created_pull_request,
    validate_created_pull_request,
    validate_open_pull_request,
)
from app.controler.workflow.thread_effects import (
    has_pending_thread_feedback,
    resolve_thread,
    revision_current,
)
from app.domain.models import TERMINAL_STATES, DeliveryState, FlowState
from app.domain.review_models import BatchState
from app.github import (
    GitHubApp,
    GitHubError,
    GitHubErrorKind,
    Issue,
    PullRequest,
    ReviewThread,
    retry_delay,
)
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.results import ResultStore
from app.persistence.reviews import ReviewStore
from app.persistence.stops import StopStore
from app.persistence.workers import WorkerStore


class _PendingReviewFeedback(RuntimeError):
    """A notification became unnecessary while its remote prerequisites were being queried."""


def _pending_feedback(plan: ResultPlan, *, reviews: ReviewStore) -> bool:
    frozen = {(f.item_id, f.version) for f in plan.consumed}
    return any((f.item_id, f.version) not in frozen for f in reviews.pending(plan.flow_id))


class ResultEffects:
    """Resume ordered effects; an uncertain non-idempotent write is reconciled, never guessed."""

    def __init__(
        self,
        github: GitHubApp,
        *,
        database: Database,
        deliveries: DeliveryStore,
        flows: FlowStore,
        jobs: JobStore,
        result_store: ResultStore,
        reviews: ReviewStore,
        stops: StopStore,
        workers: WorkerStore,
    ) -> None:
        self.database = database
        self.deliveries = deliveries
        self.flows = flows
        self.jobs = jobs
        self.result_store = result_store
        self.reviews = reviews
        self.stops = stops
        self.workers = workers
        self.github = github

    async def run(self, plan: ResultPlan) -> str | None:
        """Return a permanent failure, or finish all effects; pending work raises ResultPending."""
        while True:
            with self.database.transaction() as tx:
                deliveries_tx = self.deliveries.using(tx)
                jobs_tx = self.jobs.using(tx)
                result_store_tx = self.result_store.using(tx)
                workers_tx = self.workers.using(tx)
                flow = tx.lock_workflow(plan.flow_id)
                cancelled = flow is not None and flow.state is FlowState.CANCELLED
                if flow is None or (flow.state in TERMINAL_STATES and not cancelled):
                    result_store_tx.skip_remaining(plan.job_id, "Flow is terminal")
                    return None
                if flow.current_attempt_id != plan.attempt_id:
                    result_store_tx.skip_remaining(plan.job_id, "result attempt is no longer current")
                    return "result attempt is no longer current"
                job = jobs_tx.get_job(plan.job_id)
                if job is None:
                    raise ValueError("result job disappeared")
                if plan.archive is not None:
                    try:
                        verify_archive(
                            flow,
                            job,
                            plan.archive,
                            deliveries=deliveries_tx,
                            jobs=jobs_tx,
                            workers=workers_tx,
                        )
                    except ValueError as error:
                        raise ResultPending(str(error)) from error
                delivery = deliveries_tx.get_delivery(plan.flow_id)
                retired = cancelled or (delivery is not None and delivery.supersedes(job.kind))
                if retired:
                    result_store_tx.retire_for_merge(plan.job_id)
            records = self.result_store.list_effects(plan.job_id)
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
            record = self.result_store.claim(plan.job_id, uuid4().hex)
            if record is None:
                raise ResultPending("result effects are awaiting retry or reconciliation")
            uncertain = record.dispatched_at is not None
            try:
                receipt = (await self._recover_retired(plan, record, records) if retired
                           else await self._execute(plan, record, records))
            except _PendingReviewFeedback as exc:
                self.result_store.finish(record, EffectState.SUCCEEDED,
                                          receipt=EffectReceipt(pending_reason=str(exc)))
            except ResultPending as exc:
                cause = exc.__cause__ if isinstance(exc.__cause__, GitHubError) else None
                self.result_store.finish(
                    record,
                    EffectState.UNCERTAIN,
                    error=str(exc),
                    delay=self._delay(record, cause),
                )
                raise
            except (GitHubError, DeliveryConflict) as exc:
                # An error while querying an earlier uncertain write cannot prove it was unsent.
                definite_rejection = isinstance(exc, GitHubError) and exc.definitely_rejected
                ambiguous = isinstance(exc, GitHubError) and (
                    uncertain or (record.dispatched_at is not None and not definite_rejection)
                )
                retryable = isinstance(exc, GitHubError) and exc.retryable
                if (isinstance(record.payload, (ThreadReplyEffect, ResolveEffect))
                        and not ambiguous and not retryable):
                    # A definite permission/deletion failure leaves a diagnostic and pending input;
                    # already delivered code must not trigger another Codex run to repair GitHub.
                    self.result_store.finish(record, EffectState.SUCCEEDED, receipt=EffectReceipt(
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
                self.result_store.finish(
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
                self.result_store.finish(record, EffectState.SUCCEEDED, receipt=receipt)

    def _dispatch(self, plan: ResultPlan, record: EffectRecord) -> None:
        # Check cancellation again after remote reads, immediately before authorizing the write.
        with self.database.transaction() as tx:
            deliveries_tx = self.deliveries.using(tx)
            jobs_tx = self.jobs.using(tx)
            result_store_tx = self.result_store.using(tx)
            reviews_tx = self.reviews.using(tx)
            workers_tx = self.workers.using(tx)
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
            delivery = deliveries_tx.get_delivery(flow.id)
            if plan.archive is not None:
                try:
                    verify_archive(
                        flow,
                        job,
                        plan.archive,
                        deliveries=deliveries_tx,
                        jobs=jobs_tx,
                        workers=workers_tx,
                    )
                except ValueError as error:
                    raise ResultPending(str(error)) from error
            if delivery is not None and delivery.supersedes(job.kind):
                raise DeliveryConflict("PR closed or merged before GitHub dispatch")
            if plan.batch_id is not None:
                batch = reviews_tx.for_job(job)
                delivery = deliveries_tx.get_delivery(flow.id)
                if (batch.id != plan.batch_id
                        or batch.state not in {BatchState.READY, BatchState.PROCESSING}
                        or delivery is None or delivery.state is not DeliveryState.OPEN):
                    raise DeliveryConflict("review batch or PR is no longer active")
                if plan.delivery is not None and delivery != plan.delivery:
                    raise DeliveryConflict("delivery changed before GitHub dispatch")
                if isinstance(record.payload, ResolveEffect) and has_pending_thread_feedback(
                    plan, record.payload, reviews=reviews_tx
                ):
                    raise DeliveryConflict("new feedback arrived before resolve dispatch")
                if (
                    plan.revision is not None
                    and isinstance(record.payload, (LabelEffect, ReviewersEffect))
                    and _pending_feedback(plan, reviews=reviews_tx)
                ):
                    raise _PendingReviewFeedback("出现新反馈，暂停 re-review")
            result_store_tx.mark_dispatched(record)

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
        repository = self.github.installation(installation).repository(repo)
        if isinstance(effect, (CommentEffect, ThreadReplyEffect)):
            # This path already reconciles by marker and forbids resending a dispatched comment.
            return await self._execute(plan, record, records)
        if isinstance(effect, PullRequestEffect):
            found = await repository.find_pull_request_by_head(effect.branch)
            delivery = self.deliveries.get_delivery(plan.flow_id)
            flow = self.flows.get_flow(plan.flow_id)
            cancelled_opening = (flow is not None and flow.state is FlowState.CANCELLED
                                 and delivery is not None and delivery.state is DeliveryState.OPENING
                                 and delivery.pr_number is None)
            if (found is None or delivery is None or (not cancelled_opening and found.number != delivery.pr_number)
                    or found.head_branch != effect.branch or found.base_branch != effect.base_branch):
                raise ResultPending("retired PR creation awaits its remote identity")
            if cancelled_opening:
                remote = await repository.fetch_pull_request(found.number)
                if plan.delivery is None:
                    raise ResultPending("cancelled PR intent is missing")
                try:
                    validate_created_pull_request(remote, repo, found.number, plan.delivery)
                except DeliveryConflict as error:
                    raise ResultPending(str(error)) from error
                adopt_created_pull_request(
                    plan.attempt_id,
                    plan.delivery,
                    remote,
                    database=self.database,
                    deliveries=self.deliveries,
                    flows=self.flows,
                    jobs=self.jobs,
                    stops=self.stops,
                    workers=self.workers,
                )
                found = remote
            return EffectReceipt(pull_request=found)
        if isinstance(effect, ResolveEffect):
            resolved = await ReviewThread(PullRequest(repository, effect.number), effect.thread_id).fetch_resolution()
            return EffectReceipt(thread_id=effect.thread_id, remote_resolved=resolved,
                                 pending_reason="PR merged; observed thread without resolving")
        number = effect.number
        if number == "created_pr":
            try:
                number = unique_pull_request_receipt(records).number
            except ValueError as error:
                raise ResultPending(str(error)) from error
        if isinstance(effect, LabelEffect):
            await Issue(repository, number).fetch_labels()
        else:
            assert record.dispatched_at is not None
            await PullRequest(repository, number).fetch_reviewers(since=record.dispatched_at)
        return EffectReceipt(pending_reason="PR merged; observed notification without reissuing it")

    async def _execute(
        self,
        plan: ResultPlan,
        record: EffectRecord,
        records: list[EffectRecord],
    ) -> EffectReceipt:
        effect = record.payload
        installation, repo = effect.installation_id, effect.repo
        repository = self.github.installation(installation).repository(repo)
        if isinstance(effect, (CommentEffect, ThreadReplyEffect)):
            marker = self._comment_marker(plan, effect)

            if isinstance(effect, ThreadReplyEffect):
                thread = ReviewThread(PullRequest(repository, effect.number), effect.thread_id,
                                      root_comment_id=effect.root_comment_id)
                comment_id = await thread.find_reply(marker)
            else:
                issue = Issue(repository, effect.number)
                comment_id = await issue.find_comment(marker)
            if comment_id is None:
                # Batch answers follow this same marker-before-retry rule in JobCoordinator._answer.
                if record.dispatched_at is not None:
                    raise ResultPending("comment outcome is unknown; waiting for its remote marker")
                if isinstance(effect, ThreadReplyEffect) and not await revision_current(repository, plan, effect.number):
                    return EffectReceipt(thread_id=effect.thread_id,
                                         pending_reason="PR 已关闭或 head 已变化，未发送线程说明")
                self._dispatch(plan, record)
                body = f"{effect.body}\n\n{marker}"
                if isinstance(effect, ThreadReplyEffect):
                    comment_id = await thread.reply(body)
                else:
                    comment_id = await issue.comment(body)
            return EffectReceipt(comment_id=comment_id,
                                 thread_id=effect.thread_id if isinstance(effect, ThreadReplyEffect) else None)
        if isinstance(effect, PullRequestEffect):
            pull_request = await repository.find_pull_request_by_head(effect.branch)
            if pull_request is None:
                if record.dispatched_at is not None:
                    raise ResultPending("pull request outcome is unknown; waiting for its head")
                self._dispatch(plan, record)
                try:
                    pull_request = await repository.open_pull_request(head=effect.branch, base=effect.base_branch, title=effect.title, body=effect.body)
                except GitHubError as exc:
                    if exc.kind not in {GitHubErrorKind.CONFLICT, GitHubErrorKind.VALIDATION}:
                        raise
                    pull_request = await repository.find_pull_request_by_head(effect.branch)
                    if pull_request is None:
                        raise
            if pull_request.state == "closed":
                # PR creation may have succeeded before INITIAL's final local commit.
                # Bind its verified identity and retire intent instead of re-opening it.
                remote = await repository.fetch_pull_request(pull_request.number)
                if plan.delivery is None:
                    raise ResultPending("INITIAL PR intent is missing")
                validate_created_pull_request(remote, repo, pull_request.number, plan.delivery)
                if remote.state == "closed":
                    adopt_created_pull_request(
                        plan.attempt_id,
                        plan.delivery,
                        remote,
                        database=self.database,
                        deliveries=self.deliveries,
                        flows=self.flows,
                        jobs=self.jobs,
                        stops=self.stops,
                        workers=self.workers,
                    )
                    return EffectReceipt(pull_request=remote)
                pull_request = remote
            validate_open_pull_request(
                pull_request,
                effect.branch,
                effect.base_branch,
                effect.head_sha,
            )
            return EffectReceipt(pull_request=pull_request)
        if isinstance(effect, (LabelEffect, ReviewersEffect)):
            number = effect.number
            if plan.revision is not None:
                assert isinstance(number, int)
                if (
                    any(r.receipt and r.receipt.pending_reason for r in records)
                    or _pending_feedback(plan, reviews=self.reviews)
                    or not await revision_current(repository, plan, number)
                ):
                    return EffectReceipt(pending_reason="仍有未决反馈或 PR 已变化，暂停 re-review")
            if number == "created_pr":
                number = unique_pull_request_receipt(records).number
            if isinstance(effect, LabelEffect):
                issue = Issue(repository, number)
                labels = {label.casefold() for label in await issue.fetch_labels()}
                if (effect.label.casefold() in labels) != effect.present:
                    self._dispatch(plan, record)
                    if effect.present:
                        await issue.add_label(effect.label)
                    else:
                        await issue.remove_label(effect.label)
            else:
                # Include completed reviews since dispatch so recovery does not request them again.
                pull_request = PullRequest(repository, number)
                satisfied = await pull_request.fetch_reviewers(since=(record.dispatched_at or self.database.now()).replace(microsecond=0))
                missing = sorted(set(effect.reviewers) - satisfied)
                if missing:
                    if record.dispatched_at is not None:
                        raise ResultPending(
                            "review request outcome is unknown; waiting for remote state"
                        )
                    self._dispatch(plan, record)
                    await pull_request.request_reviewers(missing)
            return EffectReceipt()
        return await resolve_thread(
            repository,
            plan,
            record,
            records,
            self._dispatch,
            database=self.database,
            reviews=self.reviews,
        )

    def _delay(
        self,
        record: EffectRecord,
        error: GitHubError | DeliveryConflict | None = None,
    ) -> float:
        return retry_delay(record.attempts, self.database.now(),
                           error if isinstance(error, GitHubError) else None)
