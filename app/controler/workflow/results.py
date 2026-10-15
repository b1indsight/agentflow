"""Prepare immutable result intent, recover remote effects, then commit local consequences once."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from loguru import logger
from pluggy import PluginManager

from app.contracts.protocol import (
    ArchiveResult,
    Failed,
    InitialTaskResult,
    RevisionTaskResult,
    Succeeded,
    parse_worker_message,
)
from app.contracts.result_models import (
    CommentEffect,
    Effect,
    LabelEffect,
    PullRequestEffect,
    ResultPending,
    ResultPlan,
    ReviewersEffect,
    RunUpdate,
    ThreadReplyEffect,
    unique_pull_request_receipt,
)
from app.controler.config import ServerSettings
from app.controler.review.resolution import plan_resolution
from app.controler.workflow.archive import archive_effects, verify_archive
from app.controler.workflow.effects import ResultEffects
from app.controler.workflow.initial import initial_delivery_mismatch
from app.controler.workflow.revisions import (
    frozen_revision,
    revision_mismatch,
    verify_revision_remote,
)
from app.domain.models import (
    TERMINAL_STATES,
    AddressedComment,
    DeferredComment,
    DeliveryState,
    FeedbackRef,
    Flow,
    FlowDelivery,
    FlowState,
    Job,
    JobKind,
    JobState,
    ReviewItemKind,
)
from app.domain.review_models import BatchState
from app.github import GitHubApp, GitHubError
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.results import ResultStore
from app.persistence.reviews import ReviewStore
from app.persistence.workers import WorkerStore
from app.plugins import emit_hook


class ResultApplication:
    """One entry point for both a live worker's accepted result and control recovery."""

    def __init__(
        self,
        github: GitHubApp,
        settings: ServerSettings,
        plugins: PluginManager,
        *,
        database: Database,
        effects: ResultEffects,
    ) -> None:
        self.database = database
        self.deliveries = DeliveryStore(database)
        self.flows = FlowStore(database)
        self.jobs = JobStore(database)
        self.result_store = ResultStore(database)
        self.reviews = ReviewStore(database)
        self.workers = WorkerStore(database)
        self.github = github
        self.effects = effects
        self.settings = settings
        self.plugins = plugins
        self._result_locks: dict[int, asyncio.Lock] = {}
        self._recovery_lock = asyncio.Lock()
        self._recovery_cursor = self._recovery_ceiling = 0

    async def apply(self, job_id: int) -> JobState:
        """Serialize and apply a persisted terminal result for live or recovered work.

        Call after the worker result or control failure has been saved with its
        digest. Missing workflow records, missing results, or conflicting digests
        raise ValueError. An already applied result returns its stored job state.

        Applications of the same job share a local lock. Remote verification and
        effects run outside workflow transactions; the frozen plan and receipts
        let retries resume unfinished work. Local consequences and the completion
        marker commit atomically. Return the completed job state, including FAILED
        for a permanent failure. ResultPending means recovery is still needed:
        retry the same job_id through recover_pending_results, honoring backoff.
        """
        async with self._result_locks.setdefault(job_id, asyncio.Lock()):
            job = self.jobs.get_job(job_id)
            if job is not None and job.result_applied_at is not None:
                return job.state
            return await self._apply(job_id)

    async def recover_pending_results(self, *, concurrency: int = 4, batch_size: int = 64) -> None:
        """Advance bounded pages past uncertain results, with a fixed ceiling and worker pool."""
        async with self._recovery_lock:
            if not self._recovery_ceiling:
                self._recovery_ceiling = self.jobs.unapplied_result_ceiling()
            jobs = self.jobs.list_unapplied_results(
                after_id=self._recovery_cursor, through_id=self._recovery_ceiling, limit=batch_size,
            )
            if not jobs and self._recovery_cursor:
                self._recovery_ceiling = self.jobs.unapplied_result_ceiling()
                jobs = self.jobs.list_unapplied_results(through_id=self._recovery_ceiling, limit=batch_size)
            self._recovery_cursor = jobs[-1].id if jobs else 0
            if len(jobs) < batch_size or self._recovery_cursor >= self._recovery_ceiling:
                self._recovery_cursor = self._recovery_ceiling = 0
            pending = iter(jobs)

            async def drain() -> None:
                # next() does not yield: each job belongs to exactly one pool task.
                for job in pending:
                    try:
                        await self.apply(job.id)
                    except ResultPending:
                        continue
                    except Exception:  # noqa: BLE001 -- one corrupt result must not stop the recovery pool
                        logger.exception("persisted result recovery failed for job {}", job.id)

            async with asyncio.TaskGroup() as group:
                for _ in range(min(concurrency, len(jobs))):
                    group.create_task(drain())

    async def _apply(self, job_id: int) -> JobState:
        """Resume result intent and commit completion while the caller holds the job lock."""
        job = self.jobs.get_job(job_id)
        if job is None:
            raise ValueError(f"job {job_id} disappeared")
        revision_error = await self._check_revision(job)
        with self.database.transaction() as tx:
            result_store_tx = self.result_store.using(tx)
            flow = tx.lock_workflow(job.flow_id)
            current = tx.lock_job(job_id)
            if current is None or flow is None:
                raise ValueError("result workflow disappeared")
            if current.result_applied_at is not None:
                return current.state
            if current.result_payload is None or current.result_digest is None:
                raise ValueError("result application requires a persisted terminal message")
            plan = result_store_tx.get_plan(job_id)
            if plan is None:
                plan = self._prepare(tx, flow, current, revision_error)
                result_store_tx.prepare(plan)
            if plan.digest != current.result_digest:
                raise ValueError("result plan digest conflicts with inbox")
        failure = await self.effects.run(plan)
        if failure and (plan.revision is not None or plan.archive is not None):
            # Code delivery or cleanup already happened. Preserve its receipts so
            # explicit retry repairs only GitHub without another worker run.
            with self.database.transaction() as tx:
                deliveries_tx = self.deliveries.using(tx)
                jobs_tx = self.jobs.using(tx)
                flow = tx.lock_workflow(plan.flow_id)
                current = tx.lock_job(job_id)
                pending = (
                    flow is not None and current is not None
                    and flow.state not in TERMINAL_STATES
                    and flow.current_attempt_id == plan.attempt_id
                    and current.result_applied_at is None
                    and not (
                        (delivery := deliveries_tx.get_delivery(plan.flow_id)) is not None
                        and delivery.supersedes(current.kind)
                    )
                )
                if pending:
                    jobs_tx.set_job_error(job_id, failure)
            if pending:
                raise ResultPending(failure)
        with self.database.transaction() as tx:
            deliveries_tx = self.deliveries.using(tx)
            jobs_tx = self.jobs.using(tx)
            result_store_tx = self.result_store.using(tx)
            workers_tx = self.workers.using(tx)
            flow = tx.lock_workflow(plan.flow_id)
            current = tx.lock_job(job_id)
            if flow is None or current is None:
                raise ValueError("result workflow disappeared before commit")
            if current.result_applied_at is not None:
                return current.state
            if current.result_digest != plan.digest:
                raise ValueError("result digest changed before commit")
            delivery = deliveries_tx.get_delivery(flow.id)
            retired = delivery is not None and delivery.supersedes(current.kind)
            if retired:
                # Preserve the frozen plan/receipts as history; only execution facts survive merge.
                plan = self._retired_plan(current, plan.attempt_id)
                failure = None
            state = JobState.FAILED if failure else plan.state
            if flow.state not in TERMINAL_STATES:
                if flow.current_attempt_id != plan.attempt_id:
                    failure = "result attempt is no longer current"
                    state = JobState.FAILED
                else:
                    self._commit(tx, flow, current, plan, failure)
            if failure or plan.error:
                jobs_tx.set_job_error(job_id, failure or plan.error or "result failed")
                result_store_tx.skip_remaining(job_id, failure or plan.error or "result failed")
            if not jobs_tx.complete_persisted_result(job_id, state):
                raise ResultPending("result has not reached its atomic completion boundary")
            if flow.state is FlowState.CANCELLED:
                workers_tx.release_flow_execution(flow.id)
        if not retired:
            self._after_commit(flow, current, plan, state)
        return state

    async def _check_revision(self, job: Job) -> str | None:
        """Perform network reads outside the transaction, once per frozen result plan."""
        if (job.kind is not JobKind.REVISION or job.result_applied_at is not None
                or job.result_payload is None or self.result_store.get_plan(job.id) is not None):
            return None
        flow = self.flows.get_flow(job.flow_id)
        if flow is None or flow.state in TERMINAL_STATES:
            return None
        delivery = self.deliveries.get_delivery(flow.id)
        if delivery is not None and delivery.supersedes(job.kind):
            return None
        message = parse_worker_message(json.dumps(job.result_payload))
        if not isinstance(message, Succeeded) or not isinstance(message.result, RevisionTaskResult):
            return None
        result = message.result
        task = frozen_revision(job)
        mismatch = revision_mismatch(task, result)
        if mismatch:
            return mismatch
        delivery = self.deliveries.get_delivery(flow.id)
        if delivery is None or delivery.head_sha != task.expected_head_sha:
            return "revision baseline no longer matches delivery"
        if job.verify_next_at is not None and job.verify_next_at > self.database.now():
            raise ResultPending(job.verify_last_error or "revision verification backoff")
        assert result.commit_sha is not None
        if flow.installation_id is None:
            return "revision has no open verified delivery"
        try:
            repo = self.github.installation(flow.installation_id).repository(flow.repo)
            return await verify_revision_remote(repo, flow, delivery, result.commit_sha)
        except GitHubError as error:
            if error.retryable:
                self.result_store.defer_verification(job, error)
                raise ResultPending(str(error)) from error
            return str(error)

    def _prepare(
        self, tx: Database, flow: Flow, job: Job, revision_error: str | None = None,
    ) -> ResultPlan:
        """Freeze state-dependent inputs once; later attempts use this same plan and effect order."""
        deliveries_tx = self.deliveries.using(tx)
        flows_tx = self.flows.using(tx)
        jobs_tx = self.jobs.using(tx)
        reviews_tx = self.reviews.using(tx)
        workers_tx = self.workers.using(tx)
        message = parse_worker_message(json.dumps(job.result_payload))
        assert job.result_digest is not None
        attempt = flows_tx.get_current_attempt(flow)
        base = ResultPlan(
            job_id=job.id,
            flow_id=flow.id,
            digest=job.result_digest,
            attempt_id=flow.current_attempt_id,
            state=JobState.DONE,
        )
        if flow.state in TERMINAL_STATES:
            return (
                base.model_copy(update={"state": JobState.FAILED})
                if isinstance(message, Failed)
                else base
            )
        batch = reviews_tx.for_job(job) if job.kind in {JobKind.REVISION} else None
        if batch is not None:
            base = base.model_copy(update={"attempt_id": batch.attempt_id, "batch_id": batch.id})
        delivery = deliveries_tx.get_delivery(flow.id)
        if delivery is not None and delivery.supersedes(job.kind):
            return self._retired_plan(job, base.attempt_id)
        if batch is not None and (batch.attempt_id != flow.current_attempt_id
                or batch.state not in {BatchState.READY, BatchState.PROCESSING}):
            return base.model_copy(update={"state": JobState.FAILED,
                "error": "review batch or attempt is no longer current"})
        if isinstance(message, Failed):
            return self._failure(tx, flow, base, message.error)
        assert isinstance(message, Succeeded)
        result = message.result
        if result.kind != job.kind.value:
            raise ValueError("result kind does not match persisted job")
        if attempt is None:
            return self._failure(tx, flow, base, "result has no current attempt")
        if isinstance(result, RevisionTaskResult):
            assert batch is not None
            expected = [(f.item_id, f.version) for f in batch.feedback]
            received = [(f.item_id, f.version) for f in result.feedback_refs]
            if result.batch_id != batch.id or received != expected:
                return base.model_copy(update={"state": JobState.FAILED, "flow_state": FlowState.FAILED,
                    "error": "result does not match its frozen review batch"})
        if isinstance(result, InitialTaskResult):
            base = base.model_copy(
                update={
                    "run": RunUpdate(
                        kind="initial",
                        session_id=result.session_id,
                        exit_code=result.exit_code,
                    )
                }
            )
            if result.exit_code != 0 or not result.session_id:
                reason = f"编码失败（codex exit={result.exit_code}），未开 PR。"
                if result.failure_stage and result.failure_detail:
                    reason = (
                        f"编码失败（codex exit={result.exit_code}）；VCS {result.failure_stage}: "
                        f"{result.failure_detail}。未开 PR。"
                    )
                return self._failure(tx, flow, base, reason)
            delivery = deliveries_tx.get_delivery(flow.id)
            call = deliveries_tx.get_tool_call(result.open_pr_call_id) if result.open_pr_call_id else None
            mismatch = initial_delivery_mismatch(job, attempt, result, delivery, call)
            if mismatch is not None:
                return self._failure(
                    tx,
                    flow,
                    base,
                    f"编码结果与已验证的 Flow delivery 不一致：{mismatch}。",
                )
            assert delivery is not None
            if (
                not delivery.base_branch
                or not delivery.head_sha
                or not delivery.title
                or delivery.body is None
            ):
                return self._failure(
                    tx, flow, base, "Flow delivery has no complete prepared PR intent"
                )
            effects: list[Effect] = [
                PullRequestEffect(
                    key="open-pr",
                    installation_id=self._installation(flow),
                    repo=flow.repo,
                    branch=delivery.branch,
                    base_branch=delivery.base_branch,
                    head_sha=delivery.head_sha,
                    title=delivery.title,
                    body=delivery.body,
                ),
                *self._review_effects(flow, "created_pr"),
            ]
            return base.model_copy(update={"delivery": delivery, "effects": effects})
        if isinstance(result, RevisionTaskResult):
            assert batch is not None
            base = base.model_copy(
                update={
                    "run": RunUpdate(
                        kind="revision",
                        session_id=result.session_id,
                        exit_code=result.exit_code,
                    )
                }
            )
            if result.exit_code != 0:
                return self._failure(
                    tx,
                    flow,
                    base,
                    f"修订失败（exit={result.exit_code}, stage={result.failure_stage}）："
                    f"{result.failure_detail or '执行结果不确定'}。反馈保留，需核对工作区和远端。",
                )
            task = frozen_revision(job)
            delivery = deliveries_tx.get_delivery(flow.id)
            execution = workers_tx.get_flow_execution(flow.id)
            mismatch = revision_error or revision_mismatch(task, result)
            if mismatch:
                return self._failure(tx, flow, base, mismatch)
            if (delivery is None or delivery.head_sha != task.expected_head_sha
                    or delivery.state is not DeliveryState.OPEN
                    or delivery.pr_number != task.pr_number or delivery.branch != task.branch
                    or delivery.base_branch != task.base_branch
                    or attempt.codex_session_id != task.session_id
                    or attempt.session_generation != task.session_generation
                    or execution is None or execution.released_at is not None
                    or execution.repo_identity != task.repo_identity):
                return self._failure(tx, flow, base, "revision assignment is no longer current")
            assert result.output is not None
            number = self._pr_number(flow, deliveries=deliveries_tx)
            members = {(f.item_id, f.version): f for f in batch.feedback}
            reported: list[AddressedComment | DeferredComment] = [
                *result.output.addressed, *result.output.deferred,
            ]
            keys = [(f.item_id, f.version) for f in reported]
            if (len(keys) != len(set(keys)) or any(
                key not in members or members[key].github_id != item.comment_id
                for key, item in zip(keys, reported, strict=True)
            )):
                return self._failure(tx, flow, base, "revision reported unknown or conflicting feedback")
            consumed = [FeedbackRef(item_id=f.item_id, version=f.version) for f in result.output.addressed]
            # Empty review summaries carry metadata, not an additional coding request.
            reported_keys = set(keys)
            consumed.extend(
                FeedbackRef(item_id=f.item_id, version=f.version) for f in batch.feedback
                if not f.body.strip()
                and (f.kind is ReviewItemKind.REVIEW_BODY or f.signal == "request_changes")
                and (f.item_id, f.version) not in reported_keys
            )
            unresolved = (len(consumed) != len(batch.feedback)
                          or bool(result.output.needs_clarification))
            notes = plan_resolution(flow, number, batch, result,
                                    reviews_tx.addressed(flow.id, attempt.id))
            blocked = {(f.item_id, f.version) for f in notes.blocked}
            consumed = [f for f in consumed if (f.item_id, f.version) not in blocked]
            unresolved |= bool(notes.problems)
            effects = notes.effects
            new_commit = result.commit_sha != task.expected_head_sha
            if result.output.summary:
                effects.append(
                    self._comment(
                        flow, number, "summary",
                        f"修订交付"
                        f"（{'已核验新提交' if new_commit else '无新提交'}）：{result.output.summary}"
                    )
                )
            if not unresolved and new_commit:
                effects.extend(self._review_effects(flow, number))
            return base.model_copy(
                update={
                    "effects": effects,
                    "delivery": delivery,
                    "revision": result,
                    "batch_state": BatchState.WAITING if unresolved else BatchState.COMPLETED,
                    "consumed": consumed,
                    "open_points": [*notes.problems, *result.output.needs_clarification,
                                    *(item.reason for item in result.output.deferred)],
                }
            )
        if isinstance(result, ArchiveResult):
            try:
                verify_archive(
                    flow, job, result, deliveries=deliveries_tx, jobs=jobs_tx, workers=workers_tx
                )
            except ValueError as error:
                return self._failure(tx, flow, base, str(error))
            delivery = deliveries_tx.get_delivery(flow.id)
            assert delivery is not None
            return base.model_copy(update={
                "flow_state": FlowState.SUCCEEDED, "archive": result,
                "effects": archive_effects(flow, delivery, done_label=self.settings.done_label,
                                           in_review_label=self.settings.in_review_label),
            })
        raise TypeError(f"unsupported result: {type(result).__name__}")

    @staticmethod
    def _retired_plan(job: Job, attempt_id: int | None) -> ResultPlan:
        """A merge supersedes the result's domain intent, but not its immutable execution record."""
        message = parse_worker_message(json.dumps(job.result_payload))
        run = None
        state = JobState.DONE
        reason = "PR closed or merged; late result retained without applying coding/review changes"
        if isinstance(message, Failed):
            state, reason = JobState.FAILED, f"{reason}: {message.error}"
        elif isinstance(message, Succeeded):
            result = message.result
            if result.kind != job.kind.value:
                raise ValueError("result kind does not match persisted job")
            if isinstance(result, (InitialTaskResult, RevisionTaskResult)):
                run = RunUpdate(kind=result.kind, session_id=result.session_id,
                                exit_code=result.exit_code)
                if result.exit_code != 0:
                    state = JobState.FAILED
        else:
            raise TypeError("persisted inbox message is not terminal")
        assert job.result_digest is not None
        return ResultPlan(job_id=job.id, flow_id=job.flow_id, digest=job.result_digest,
                          attempt_id=attempt_id, state=state, error=reason, run=run)

    def _commit(
        self,
        tx: Database,
        flow: Flow,
        job: Job,
        plan: ResultPlan,
        failure: str | None,
    ) -> None:
        """Commit local consequences under the same connection as the final result marker."""
        deliveries_tx = self.deliveries.using(tx)
        flows_tx = self.flows.using(tx)
        jobs_tx = self.jobs.using(tx)
        result_store_tx = self.result_store.using(tx)
        reviews_tx = self.reviews.using(tx)
        workers_tx = self.workers.using(tx)
        delivery = deliveries_tx.get_delivery(flow.id)
        retired = delivery is not None and delivery.supersedes(job.kind)
        if plan.run is not None and plan.attempt_id is not None:
            attempt = flows_tx.get_attempt(plan.attempt_id)
            if attempt is None:
                raise ValueError("result attempt disappeared")
            if not retired and (plan.run.kind == "initial" or (
                plan.run.session_id is not None and not failure and plan.state is JobState.DONE
            )):
                attempt.codex_session_id = plan.run.session_id
                if plan.revision is not None:
                    attempt.session_generation = plan.revision.session_generation
                    assert plan.revision.output is not None
                    attempt.last_revision_summary = plan.revision.output.summary
                flows_tx.update_attempt(attempt)
        if retired:
            return
        if job.kind is JobKind.ARCHIVE:
            if failure or plan.state is JobState.FAILED:
                return
            assert plan.archive is not None
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
        state = FlowState.FAILED if failure else plan.flow_state
        if state is not None:
            flow.state = state
            flows_tx.update_flow(flow)
            if job.kind is not JobKind.REVISION:
                workers_tx.release_flow_execution(flow.id)
        if failure or plan.state is JobState.FAILED:
            return
        if plan.delivery is not None:
            current = deliveries_tx.get_delivery(flow.id)
            if current is None or current.version != plan.delivery.version:
                raise ResultPending("delivery version changed before result commit")
            if plan.revision is not None:
                current.head_sha = plan.revision.commit_sha
                current.version += 1
                deliveries_tx.update_delivery(current)
            else:
                self._commit_initial_delivery(
                    job, current, flow, deliveries=deliveries_tx, result_store=result_store_tx
                )
        if plan.batch_id is not None and plan.batch_state is not None:
            consumed = plan.consumed
            batch_state, open_points = plan.batch_state, list(plan.open_points)
            if plan.revision is not None:
                records = result_store_tx.list_effects(plan.job_id)
                blocked = {(f.item_id, f.version) for r in records
                           if isinstance(r.payload, ThreadReplyEffect)
                           and r.receipt is not None and r.receipt.pending_reason
                           for f in r.payload.feedback_refs}
                consumed = [f for f in consumed if (f.item_id, f.version) not in blocked]
                open_points.extend(r.receipt.pending_reason for r in records
                                   if r.receipt is not None and r.receipt.pending_reason)
                if open_points:
                    batch_state = BatchState.WAITING
                assert plan.revision.commit_sha is not None
                reviews_tx.record_addressed(plan.batch_id, consumed, plan.revision.commit_sha, job.id)
            reviews_tx.complete(plan.batch_id, batch_state, consumed, list(dict.fromkeys(open_points)))

    @staticmethod
    def _commit_initial_delivery(
        job: Job,
        current: FlowDelivery,
        flow: Flow,
        *,
        deliveries: DeliveryStore,
        result_store: ResultStore,
    ) -> None:
        pull_request = unique_pull_request_receipt(result_store.list_effects(job.id))
        current.state = DeliveryState.OPEN
        current.pr_number = pull_request.number
        current.url = f"https://github.com/{flow.repo}/pull/{pull_request.number}"
        current.version += 1
        deliveries.update_delivery(current)

    def _failure(self, tx: Database, flow: Flow, base: ResultPlan, reason: str) -> ResultPlan:
        deliveries_tx = self.deliveries.using(tx)
        delivery = deliveries_tx.get_delivery(flow.id)
        if delivery is not None and delivery.state is DeliveryState.MERGED:
            return base.model_copy(update={"state": JobState.FAILED, "error": reason})
        number = delivery.pr_number if delivery and delivery.pr_number else flow.issue_number
        return base.model_copy(
            update={
                "state": JobState.FAILED,
                "flow_state": FlowState.FAILED,
                "error": reason,
                "effects": (
                    [self._comment(flow, number, "failure", f"⚠️ {reason}")]
                    if flow.installation_id is not None
                    else []
                ),
            }
        )

    def _review_effects(self, flow: Flow, number: int | Literal["created_pr"]) -> list[Effect]:
        effects: list[Effect] = [
            LabelEffect(
                key="labels",
                installation_id=self._installation(flow),
                repo=flow.repo,
                number=number,
                label=self.settings.in_review_label,
                present=True,
            )
        ]
        if self.settings.reviewers:
            effects.append(
                ReviewersEffect(
                    key="reviewers",
                    installation_id=self._installation(flow),
                    repo=flow.repo,
                    number=number,
                    reviewers=list(self.settings.reviewers),
                )
            )
        return effects

    @staticmethod
    def _installation(flow: Flow) -> int:
        if flow.installation_id is None:
            raise ValueError("Flow has no GitHub installation")
        return flow.installation_id

    def _comment(self, flow: Flow, number: int, key: str, body: str) -> CommentEffect:
        return CommentEffect(
            key=key,
            installation_id=self._installation(flow),
            repo=flow.repo,
            number=number,
            body=body,
        )

    @staticmethod
    def _pr_number(flow: Flow, *, deliveries: DeliveryStore) -> int:
        delivery = deliveries.get_delivery(flow.id)
        if delivery is None or delivery.pr_number is None:
            raise ValueError("Flow has no delivered PR")
        return delivery.pr_number

    def _after_commit(self, flow: Flow, job: Job, plan: ResultPlan, state: JobState) -> None:
        if state is not JobState.DONE or flow.state in {FlowState.FAILED, FlowState.CANCELLED}:
            return
        if flow.state is FlowState.SUCCEEDED and job.kind is not JobKind.ARCHIVE:
            return
        if job.kind is JobKind.ARCHIVE:
            emit_hook(self.plugins, "on_archived", flow=flow)
        elif job.kind is JobKind.INITIAL and plan.delivery is not None:
            emit_hook(
                self.plugins,
                "on_pr_opened",
                flow=flow,
                pr_number=self._pr_number(flow, deliveries=self.deliveries),
            )
