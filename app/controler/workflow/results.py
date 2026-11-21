"""Prepare immutable result intent, recover remote effects, then commit local consequences once."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from loguru import logger
from pluggy import PluginManager

from app.contracts.protocol import (
    ArchiveResult,
    Failed,
    InferenceResult,
    InitialTaskResult,
    RevisionTaskResult,
    Succeeded,
)
from app.contracts.receipts import MergeArchiveNotice, decode_result_inbox
from app.contracts.result_models import (
    ResultPending,
    ResultPlan,
    RunUpdate,
    ThreadReplyEffect,
    unique_pull_request_receipt,
)
from app.controler.config import ServerSettings
from app.controler.workflow.archive import archive_plan, merge_notice_plan, verify_archive
from app.controler.workflow.effects import ResultEffects
from app.controler.workflow.initial import BRANCH_SLUG_PATTERN, initial_plan
from app.controler.workflow.plans import coding_failure_status, delivered_pr_number, failed_plan
from app.controler.workflow.revisions import frozen_revision, revision_plan, verify_revision_result
from app.domain.flow_policy import FlowEvent
from app.domain.inference_models import InferenceError, InferenceFailure
from app.domain.json import strict_json
from app.domain.models import (
    TERMINAL_STATES,
    DeliveryState,
    Flow,
    FlowDelivery,
    FlowState,
    Job,
    JobKind,
    JobState,
    TriageVerdict,
)
from app.domain.review_models import BatchState
from app.github import GitHubApp
from app.github.console import Observation
from app.persistence.activity import ActivityStore
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.inference import InferenceStore
from app.persistence.jobs import JobStore
from app.persistence.results import ResultStore
from app.persistence.reviews import ReviewStore
from app.plugins import emit_hook


def _coding_failure(job: Job, now: datetime) -> Observation:
    """Expose a safe diagnostic from the verified inbox even without a native Agent transcript."""
    assert job.result_payload is not None and job.result_digest is not None
    inbox = decode_result_inbox(job.result_payload, job_id=job.id, assignment_token=job.assignment_token,
                                result_digest=job.result_digest)
    author, body = "FlowTool", "控制端处理任务结果失败。"
    if isinstance(inbox.terminal, Failed):
        if inbox.source == "worker":
            author, body = "Worker", "Worker 报告任务失败。"
    elif isinstance(inbox.terminal, Succeeded):
        result = inbox.terminal.result
        assert isinstance(result, (InitialTaskResult, RevisionTaskResult))
        if result.exit_code:
            author, body = "Worker", coding_failure_status(result)
        else:
            body = "Worker 返回退出码 0，但控制端处理任务结果失败。"
    return Observation(
        key=f"workflow_result:{job.id}", source="workflow_result", kind="failure",
        attempt_id=job.attempt_id, author=author, occurred_at=now, updated_at=now,
        title=f"任务 #{job.id} 失败", body=body, metadata={"job_id": job.id, "kind": job.kind.value},
    )


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
        self.inference = InferenceStore(database)
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
            if job is not None and job.kind is JobKind.INFERENCE:
                return self._apply_inference(job)
            return await self._apply(job_id)

    def _apply_inference(self, job: Job) -> JobState:
        """Validate saved inference results and publish only structured Flow names."""
        inference = job.inference
        if inference is None or inference.current_task is None or job.result_payload is None:
            raise ValueError("inference application requires a persisted assignment and result")
        failure = None
        title = None
        try:
            result = InferenceResult.model_validate(job.result_payload)
            if result.output is not None:
                result.output.validate_for(inference.current_task)
                if inference.validation == "review":
                    TriageVerdict.model_validate(strict_json(result.output.text), context={"judge_response": True})
                elif inference.validation == "flow_naming":
                    output = strict_json(result.output.text)
                    if not isinstance(output, dict) or set(output) != {"title", "branch_slug"}:
                        raise ValueError("expected title and branch_slug")
                    title, branch_slug = output["title"], output["branch_slug"]
                    # Keep these bounds aligned with the schema in _ensure_naming.
                    if (not isinstance(title, str) or not title.strip() or len(title) > 256
                            or not title.isprintable() or not isinstance(branch_slug, str)
                            or not 3 <= len(branch_slug) <= 48
                            or re.fullmatch(BRANCH_SLUG_PATTERN, branch_slug) is None):
                        raise ValueError("invalid title or branch_slug")
        except InferenceError as error:
            failure = error.failure
        except ValueError:
            failure = InferenceFailure(code="invalid_response")
        with self.database.transaction() as tx:
            self.inference.using(tx).complete_inference_result(job.id, failure=failure)
            current = self.jobs.using(tx).get_job(job.id)
            assert current is not None
            if (current.state is JobState.DONE and job.flow_id is not None
                    and inference.validation == "flow_naming"):
                flow = self.flows.using(tx).get_flow(job.flow_id)
                if (flow is not None and flow.title_job_id == job.id
                        and flow.current_attempt_id == job.attempt_id
                        and flow.current_attempt_id == inference.source_attempt_id):
                    assert isinstance(title, str)
                    self.flows.using(tx).set_title(flow.id, job.id, title.strip())
        return current.state

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
        revision_error = await verify_revision_result(
            job, github=self.github, database=self.database, flows=self.flows,
            deliveries=self.deliveries, result_store=self.result_store,
        )
        with self.flows.locked(job.required_flow_id) as context:
            tx = context.database
            result_store_tx = self.result_store.using(tx)
            flow = context.flow
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
        if failure and plan.archive_notice is not None:
            # A permission failure is retryable notification work, never a reversal
            # of the immutable merge conclusion or a reason to retain a Worker.
            self.jobs.set_job_error(job_id, failure)
            raise ResultPending(failure)
        if failure and (plan.revision is not None or plan.archive is not None):
            # Code delivery or cleanup already happened. Preserve its receipts so
            # explicit retry repairs only GitHub without another worker run.
            with self.flows.locked(plan.flow_id) as context:
                tx = context.database
                deliveries_tx = self.deliveries.using(tx)
                jobs_tx = self.jobs.using(tx)
                flow = context.flow
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
                    assert flow is not None
                    self.flows.using(tx).transition(flow, FlowEvent.RESULT_FAILED)
            if pending:
                raise ResultPending(failure)
        with self.flows.locked(plan.flow_id) as context:
            tx = context.database
            deliveries_tx = self.deliveries.using(tx)
            jobs_tx = self.jobs.using(tx)
            result_store_tx = self.result_store.using(tx)
            flow = context.flow
            current = tx.lock_job(job_id)
            if flow is None or current is None:
                raise ValueError("result workflow disappeared before commit")
            if current.result_applied_at is not None:
                return current.state
            if current.result_digest != plan.digest:
                raise ValueError("result digest changed before commit")
            delivery = deliveries_tx.get_delivery(flow.id)
            retired = flow.retires(current, delivery)
            if retired:
                # Preserve the frozen plan/receipts as history; only execution facts survive merge.
                if plan.archive_notice is None:
                    plan = self._retired_plan(current, plan.attempt_id)
                failure = None
            state = JobState.FAILED if failure else plan.state
            if flow.state not in TERMINAL_STATES and not retired:
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
            if state is JobState.FAILED and current.kind in {JobKind.INITIAL, JobKind.REVISION}:
                ActivityStore(tx).append(flow.id, _coding_failure(current, tx.now()))
            if current.attempt_id == flow.current_attempt_id:
                # Even a failed INITIAL retains its checkout. Explicit finalization
                # must confirm stop/reclamation before releasing the binding.
                self.flows.using(tx).try_archive(flow.id)
            flow = self.flows.using(tx).get_flow(flow.id) or flow
        if not retired:
            self._after_commit(flow, current, plan, state)
        return state

    def _prepare(
        self, tx: Database, flow: Flow, job: Job, revision_error: str | None = None,
    ) -> ResultPlan:
        """Freeze state-dependent inputs once; later attempts use this same plan and effect order."""
        deliveries_tx = self.deliveries.using(tx)
        flows_tx = self.flows.using(tx)
        jobs_tx = self.jobs.using(tx)
        reviews_tx = self.reviews.using(tx)
        if job.result_payload is None or job.result_digest is None:
            raise ValueError("result application requires a persisted terminal and digest")
        message = decode_result_inbox(
            job.result_payload,
            job_id=job.id,
            assignment_token=job.assignment_token,
            result_digest=job.result_digest,
        ).terminal
        assert job.attempt_id is not None  # Non-inference jobs always belong to an Attempt.
        attempt = flows_tx.get_attempt(job.attempt_id)
        base = ResultPlan(
            job_id=job.id,
            flow_id=flow.id,
            digest=job.result_digest,
            attempt_id=job.attempt_id,
            state=JobState.DONE,
        )
        if isinstance(message, MergeArchiveNotice):
            return merge_notice_plan(
                base, flow, job, message, activity=ActivityStore(tx),
                done_label=self.settings.done_label, in_review_label=self.settings.in_review_label,
            )
        if flow.state in TERMINAL_STATES or job.attempt_id != flow.current_attempt_id:
            return self._retired_plan(job, base.attempt_id)
        # Bind failed results to their review batch too, before handling the terminal kind.
        batch = reviews_tx.for_job(job) if job.kind in {JobKind.REVISION} else None
        if batch is not None:
            base = base.model_copy(update={"batch_id": batch.id})
        delivery = deliveries_tx.get_delivery(flow.id)
        if flow.retires(job, delivery):
            return self._retired_plan(job, base.attempt_id)
        if batch is not None and (batch.attempt_id != flow.current_attempt_id
                or batch.state not in {BatchState.READY, BatchState.PROCESSING}):
            return base.model_copy(update={"state": JobState.FAILED,
                "error": "review batch or attempt is no longer current"})
        if isinstance(message, Failed):
            return failed_plan(base, message.error)
        assert isinstance(message, Succeeded)
        result = message.result
        if result.kind != job.kind.value:
            raise ValueError("result kind does not match persisted job")
        if attempt is None:
            return failed_plan(base, "result has no current attempt")
        if isinstance(result, InitialTaskResult):
            return initial_plan(
                base, flow, job, attempt, result, deliveries=deliveries_tx,
                in_review_label=self.settings.in_review_label, reviewers=self.settings.reviewers,
            )
        if isinstance(result, RevisionTaskResult):
            assert batch is not None
            return revision_plan(
                base, flow, job, attempt, batch, result, revision_error,
                deliveries=deliveries_tx, flows=flows_tx, reviews=reviews_tx,
                in_review_label=self.settings.in_review_label, reviewers=self.settings.reviewers,
            )
        if isinstance(result, ArchiveResult):
            return archive_plan(
                base, flow, job, result, deliveries=deliveries_tx, flows=flows_tx, jobs=jobs_tx,
                done_label=self.settings.done_label, in_review_label=self.settings.in_review_label,
            )
        raise TypeError(f"unsupported result: {type(result).__name__}")

    @staticmethod
    def _retired_plan(job: Job, attempt_id: int | None) -> ResultPlan:
        """A merge supersedes the result's domain intent, but not its immutable execution record."""
        if job.result_payload is None or job.result_digest is None:
            raise ValueError("retired result requires a persisted terminal and digest")
        message = decode_result_inbox(
            job.result_payload,
            job_id=job.id,
            assignment_token=job.assignment_token,
            result_digest=job.result_digest,
        ).terminal
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
        return ResultPlan(job_id=job.id, flow_id=job.required_flow_id, digest=job.result_digest,
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
                flows_tx.transition(flow, FlowEvent.RESULT_FAILED)
                return
            assert plan.archive is not None
            try:
                verify_archive(
                    flow,
                    job,
                    plan.archive,
                    deliveries=deliveries_tx,
                    jobs=jobs_tx,
                    flows=flows_tx,
                )
            except ValueError as error:
                raise ResultPending(str(error)) from error
        state = FlowState.BLOCKED if failure else plan.flow_state
        if state is FlowState.ARCHIVED:
            # Archive and ownership release happen after this result's completion
            # marker, in the same transaction, once the Attempt's resources settle.
            flows_tx.transition(flow, FlowEvent.CLEANUP_APPLIED)
        elif state is FlowState.BLOCKED:
            flows_tx.transition(flow, FlowEvent.RESULT_FAILED)
        elif state is not None:
            raise ValueError(f"unsupported result Flow outcome: {state}")
        if failure or plan.state is JobState.FAILED:
            return
        if plan.delivery is not None:
            current = deliveries_tx.get_delivery(flow.id)
            if current is None or current.version != plan.delivery.version:
                raise ResultPending("delivery version changed before result commit")
            if plan.revision is not None:
                if plan.revision_purpose == "revise":
                    current.head_sha = plan.revision.commit_sha
                    current.base_branch = frozen_revision(job).base_branch
                    current.version += 1
                    deliveries_tx.update_delivery(current)
            else:
                self._commit_initial_delivery(
                    job, current, flow, deliveries=deliveries_tx, result_store=result_store_tx
                )
            flows_tx.transition(flow, FlowEvent.DELIVERY_APPLIED)
        if plan.batch_id is not None and plan.batch_state is not None:
            consumed = plan.consumed
            batch_state, open_points = plan.batch_state, list(plan.open_points)
            addressed_by = None
            if plan.revision is not None:
                records = result_store_tx.list_effects(plan.job_id)
                blocked = {(f.item_id, f.version) for r in records
                           if isinstance(r.payload, ThreadReplyEffect)
                           and r.receipt is not None and r.receipt.pending_reason
                           for f in r.payload.feedback_refs}
                consumed = [f for f in consumed if (f.item_id, f.version) not in blocked]
                if plan.revision_purpose == "answer" and any(
                    r.receipt is not None and r.receipt.pending_reason for r in records
                ):
                    consumed = []
                open_points.extend(r.receipt.pending_reason for r in records
                                   if r.receipt is not None and r.receipt.pending_reason)
                if open_points:
                    batch_state = BatchState.WAITING
                assert plan.revision.commit_sha is not None
                assert plan.delivery is not None and plan.delivery.head_sha is not None
                head = (frozen_revision(job).expected_head_sha if plan.revision_purpose == "answer"
                        else plan.revision.commit_sha)
                addressed_by = (head, job.id)
            reviews_tx.complete(plan.batch_id, batch_state, consumed, list(dict.fromkeys(open_points)),
                                addressed_by=addressed_by)

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

    def _after_commit(self, flow: Flow, job: Job, plan: ResultPlan, state: JobState) -> None:
        if state is not JobState.DONE or flow.state is FlowState.BLOCKED:
            return
        if flow.state is FlowState.ARCHIVED and job.kind is not JobKind.ARCHIVE:
            return
        if job.kind is JobKind.INITIAL and plan.delivery is not None:
            emit_hook(
                self.plugins,
                "on_pr_opened",
                flow=flow,
                pr_number=delivered_pr_number(flow, deliveries=self.deliveries),
            )
