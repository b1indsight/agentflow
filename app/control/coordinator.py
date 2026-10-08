"""Translate persisted jobs into remote tasks and apply remote results."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Protocol

from app.contracts.protocol import (
    AssignmentSecret,
    ExecutionTask,
    Failed,
    FlowToolRequest,
    FlowToolResponse,
    InitialTask,
    PushSecret,
    RevisionTask,
    terminal_digest,
)
from app.control.archive import archive_task
from app.control.preparation import PreparationReadFailure, PreparationStale
from app.control.resolution import feedback_link
from app.control.results import ResultApplication, ResultSettings
from app.control.review_context import build_review_context
from app.control.revisions import verify_revision_remote
from app.control.tools import FlowToolService
from app.domain.github_models import GitHubAdapterError, GitHubErrorKind
from app.domain.models import (
    TERMINAL_STATES,
    Flow,
    FlowDelivery,
    Job,
    JobKind,
    JobState,
    ReviewItemKind,
    TriageVerdict,
    Verdict,
)
from app.domain.ports import GitHubClient, Judge
from app.domain.review_models import BatchState, ReviewBatch
from app.persistence.store import Store
from app.plugins import emit_hook, get_plugin_manager


class CoordinatorSettings(ResultSettings, Protocol):
    max_revision_cycles: int
    prepare_max_failures: int
    prepare_retry_window_seconds: float


class JobCoordinator:
    """The sole owner of flow state transitions and GitHub side effects."""

    def __init__(
        self,
        store: Store,
        github: GitHubClient,
        settings: CoordinatorSettings,
        plugins=None,
    ) -> None:
        self.store = store
        self.github = github
        self.settings = settings
        self.plugins = plugins or get_plugin_manager()
        self.tools = FlowToolService(store, github)
        self.results = ResultApplication(store, github, settings, self.plugins)

    @staticmethod
    def _retired(store: Store, flow: Flow, job: Job) -> bool:
        delivery = store.get_delivery(flow.id)
        return (flow.state in TERMINAL_STATES
                or (delivery is not None and delivery.supersedes(job.kind)))

    async def triage(self, batch: ReviewBatch, judge: Judge) -> None:
        """Resolve a feedback prerequisite, enqueueing a worker job only for revision."""
        with self.store.transaction() as tx:
            flow = tx.lock_workflow(batch.flow_id)
            if not tx.reviews.current(batch):
                return
        assert flow is not None and flow.installation_id is not None
        context = batch.context or await build_review_context(
            self.github, flow, batch, self._pr_number(flow),
        )
        with self.store.transaction() as tx:
            tx.lock_workflow(flow.id)
            if not tx.reviews.current(batch):
                return
            context = tx.reviews.freeze_context(batch, context)
            tx.reviews.relayed(batch)
        decision = batch.decision or await judge.triage(context)
        if decision.context_digest != context.digest:
            raise ValueError("Judge decision does not match its frozen context")
        verdict = decision.verdict
        if context.limitations or decision.failure:
            verdict = TriageVerdict(verdict=Verdict.WAIT, reason=decision.failure
                or "incomplete-context", open_points=verdict.open_points)
        elif verdict.verdict is Verdict.REVISE and verdict.confidence < self.settings.confidence_threshold:
            verdict = TriageVerdict(verdict=Verdict.WAIT, confidence=verdict.confidence,
                reason=f"low-confidence: {verdict.reason}"[:2000], open_points=verdict.open_points)
        marker = f"<!-- agentflow:batch:{batch.id}:answer -->"
        if verdict.verdict is Verdict.ANSWER:
            found = await self.github.find_comment(flow.installation_id, flow.repo, context.pr_number, marker)
            with self.store.transaction() as tx:
                tx.lock_workflow(flow.id)
                if not tx.reviews.current(batch):
                    return
                if found is not None:
                    tx.reviews.complete(batch.id, BatchState.COMPLETED, list(batch.feedback), [])
                    return
                if tx.reviews.get_batch(batch.id).answer_dispatched:
                    return  # An uncertain POST is reconciled by marker, never blindly repeated.
        # Remote reads can outlive close/merge or a changed PR baseline.
        remote = (await self.github.get_pull_request(flow.installation_id, flow.repo, context.pr_number)
                  if verdict.verdict in {Verdict.REVISE, Verdict.ANSWER} else None)
        with self.store.transaction() as tx:
            tx.lock_workflow(flow.id)
            if not tx.reviews.current(batch):
                return
            if remote is not None and (context.pull_request is None or remote.state != "open" or remote.merged
                    or remote.head_sha != context.pull_request.head_sha
                    or remote.base_sha != context.pull_request.base_sha):
                with tx.connection() as connection:
                    connection.execute("UPDATE review_batches SET context=NULL,decision=NULL WHERE id=%s", (batch.id,))
                return
            tx.reviews.record_decision(batch, decision.model_copy(update={"verdict": verdict}))
            if verdict.verdict is Verdict.REVISE:
                job = tx.enqueue_job(flow.id, JobKind.REVISION,
                    {"batch_id": batch.id, "instruction": verdict.instruction})
                tx.reviews.advance(batch.id, job)
            elif verdict.verdict is not Verdict.ANSWER:
                tx.reviews.complete(batch.id,
                    BatchState.COMPLETED if verdict.verdict is Verdict.IGNORE else BatchState.WAITING,
                    list(batch.feedback) if verdict.verdict is Verdict.IGNORE else [],
                    verdict.open_points or ([verdict.reason or "Awaiting more discussion."]
                        if verdict.verdict is Verdict.WAIT else []))
        if verdict.verdict is Verdict.ANSWER:
            with self.store.transaction() as tx:
                tx.lock_workflow(flow.id)
                if not tx.reviews.current(batch):
                    return
                with tx.connection() as connection:
                    connection.execute("UPDATE review_batches SET answer_dispatched=TRUE WHERE id=%s", (batch.id,))
            sources = ", ".join(f"[{f.kind.value} {f.github_id}]({feedback_link(flow.repo,
                context.pr_number, f.kind, f.github_id)})" for f in batch.feedback)
            try:
                await self.github.comment(flow.installation_id, flow.repo, context.pr_number,
                    f"{verdict.answer}\n\n来源：{sources}\n\n{marker}")
            except GitHubAdapterError as exc:
                # Same definite-rejection boundary as ResultEffects.run; uncertain writes stay marked.
                if exc.definitely_unsent or (exc.status_code is not None and 400 <= exc.status_code < 500
                                            and exc.status_code != 408):
                    with self.store.connection() as connection:
                        connection.execute("UPDATE review_batches SET answer_dispatched=FALSE WHERE id=%s", (batch.id,))
                raise
            with self.store.transaction() as tx:
                tx.lock_workflow(flow.id)
                if not tx.reviews.current(batch):
                    return
                tx.reviews.complete(batch.id, BatchState.COMPLETED, list(batch.feedback), [])
        emit_hook(self.plugins, "on_review", flow=flow)

    async def prepare_for_assignment(
        self, job: Job, worker: str, token: str, epoch: str,
    ) -> ExecutionTask | JobState:
        """Bound read preparation for remote assignments."""
        error = None
        expired = (job.prepare_first_failed_at is not None and self.store.now()
                   >= job.prepare_first_failed_at + timedelta(seconds=self.settings.prepare_retry_window_seconds))
        if not expired:
            try:
                prepared = await self.prepare(job)
            except PreparationReadFailure as failure:
                error = failure.error
            else:
                if not isinstance(prepared, JobState) and not self.store.clear_preparation(job.id, worker, token, epoch):
                    raise PreparationStale("preparation changed before dispatch")
                return prepared
        state = self.store.defer_preparation(
            job.id, worker, token, epoch, error, max_failures=self.settings.prepare_max_failures,
            window_seconds=self.settings.prepare_retry_window_seconds,
        )
        if state is None:
            raise PreparationStale("preparation changed before retry")
        if state is JobState.FAILED and worker is not None:
            await self.results.apply(job.id)
        return state

    async def _read[T](self, job: Job, operation: Callable[[], Awaitable[T]]) -> T:
        """Bound only GitHub reads by the remaining retry window, never failure effects or signing."""
        remaining = None if job.prepare_first_failed_at is None else max(0, (
            job.prepare_first_failed_at + timedelta(seconds=self.settings.prepare_retry_window_seconds)
            - self.store.now()
        ).total_seconds())
        try:
            async with asyncio.timeout(remaining):
                return await operation()
        except TimeoutError as error:
            raise PreparationReadFailure(GitHubAdapterError(
                GitHubErrorKind.TRANSIENT, "preparation read window expired",
            )) from error

    async def prepare(self, job: Job) -> ExecutionTask | JobState:
        flow = self.store.get_flow(job.flow_id)
        if flow is None:
            return JobState.FAILED
        if self._retired(self.store, flow, job):
            return JobState.DONE

        if job.kind is JobKind.INITIAL:
            if flow.installation_id is None:
                await self.fail(job, "Flow 缺少 GitHub installation。")
                return JobState.FAILED
            installation_id = flow.installation_id
            execution = self.store.get_flow_execution(flow.id)
            if execution is None or execution.released_at is not None:
                await self.fail(job, "Flow 缺少 active execution binding。")
                return JobState.FAILED
            if job.assigned_worker_id and execution.worker_id != job.assigned_worker_id:
                await self.fail(job, "Flow execution binding 与 assignment 不匹配。")
                return JobState.FAILED
            with self.store.transaction() as tx:
                fresh = tx.lock_workflow(flow.id)
                if fresh is None or self._retired(tx, fresh, job):
                    return JobState.DONE
                attempt = tx.get_current_attempt(fresh)
                if attempt is None:
                    attempt = tx.create_attempt(fresh.id)
                    fresh.current_attempt_id = attempt.id
                    tx.update_flow(fresh)
                delivery = tx.get_or_create_delivery(fresh.id, f"flow/{fresh.id}")
                flow = fresh
            try:
                issue = await self._read(job, lambda: self.github.get_issue(installation_id, flow.repo, flow.issue_number))
                repository = await self._read(job, lambda: self.github.get_repository(installation_id, flow.repo))
            except GitHubAdapterError as exc:
                raise PreparationReadFailure(exc) from exc
            if repository.full_name != flow.repo:
                await self.fail(job, "GitHub repository identity 不匹配。")
                return JobState.FAILED
            with self.store.transaction() as tx:
                fresh = tx.lock_workflow(flow.id)
                if fresh is None or self._retired(tx, fresh, job):
                    return JobState.DONE
                current_delivery = tx.get_delivery(flow.id)
                if current_delivery is None:
                    raise ValueError("prepared delivery disappeared")
                current_delivery.base_branch = repository.default_branch
                tx.update_delivery(current_delivery)
                delivery = current_delivery
            return InitialTask(
                flow_id=flow.id,
                attempt_id=attempt.id,
                repo=flow.repo,
                issue_number=flow.issue_number,
                issue_title=issue.title,
                issue_body=issue.body,
                branch=delivery.branch,
                base_branch=repository.default_branch,
                remote="origin",
                repo_identity=execution.repo_identity,
            )

        attempt = self.store.get_current_attempt(flow)
        if attempt is None:
            await self.fail(job, f"{job.kind} job has no current attempt")
            return JobState.FAILED

        if job.kind is JobKind.REVISION:
            batch = self.store.reviews.for_job(job)
            if batch.attempt_id != attempt.id or batch.state not in {
                BatchState.READY, BatchState.PROCESSING,
            }:
                raise ValueError("review batch is no longer current")

        threads = None
        if job.kind is JobKind.REVISION and batch.threads is None and any(
            f.kind is ReviewItemKind.INLINE for f in batch.feedback
        ):
            assert flow.installation_id is not None
            number = self._pr_number(flow)
            try:
                installation = flow.installation_id
                threads = await self._read(job, lambda: self.github.list_review_threads(installation, flow.repo, number))
            except GitHubAdapterError as exc:
                raise PreparationReadFailure(exc) from exc

        if job.kind is JobKind.REVISION:
            delivery = self._delivery(flow)
            if attempt.revision_cycles + 1 > self.settings.max_revision_cycles:
                await self.fail(job, "已达最大修订轮数，暂停。")
                return JobState.FAILED
            execution = self.store.get_flow_execution(flow.id)
            if execution is None or execution.released_at is not None:
                await self.fail(job, "修订任务缺少有效 worker binding。")
                return JobState.FAILED
            try:
                mismatch = await self._read(job, lambda: verify_revision_remote(
                    self.github, flow, delivery, delivery.head_sha or "",
                ))
                if not mismatch:
                    assert flow.installation_id is not None
                    installation = flow.installation_id
                    issue = await self._read(job, lambda: self.github.get_issue(
                        installation, flow.repo, flow.issue_number,
                    ))
            except GitHubAdapterError as exc:
                raise PreparationReadFailure(exc) from exc
            if mismatch:
                await self.fail(job, mismatch)
                return JobState.FAILED
            assert flow.installation_id is not None
            assert delivery.head_sha and delivery.base_branch and delivery.pr_number
            task = RevisionTask(
                batch_id=batch.id,
                flow_id=flow.id,
                attempt_id=attempt.id,
                repo=flow.repo,
                branch=delivery.branch,
                repo_identity=execution.repo_identity,
                pr_number=delivery.pr_number,
                base_branch=delivery.base_branch,
                expected_head_sha=delivery.head_sha,
                session_id=attempt.codex_session_id,
                session_generation=attempt.session_generation,
                issue_title=issue.title,
                issue_body=issue.body,
                previous_summary=attempt.last_revision_summary,
                open_points=batch.open_points,
                context=batch.context,
                instruction=(job.payload or {}).get("instruction", "Address the review feedback."),
                feedback=batch.feedback,
            )
            with self.store.transaction() as tx:
                fresh = tx.lock_workflow(flow.id)
                current = tx.lock_job(job.id)
                binding = tx.get_flow_execution(flow.id)
                latest = tx.get_delivery(flow.id)
                if (fresh is None or current is None or self._retired(tx, fresh, job)
                        or fresh.current_attempt_id != attempt.id):
                    return JobState.DONE
                if (binding != execution or latest != delivery
                        or current.assignment_token != job.assignment_token
                        or current.assigned_worker_id != job.assigned_worker_id
                        or current.state is not JobState.RUNNING
                        or (job.assignment_token is not None and (
                            current.lease_expires_at is None
                            or current.lease_expires_at <= tx.now()))):
                    return JobState.QUEUED
                if threads is not None:
                    tx.reviews.freeze_threads(current, threads)
                return tx.reviews.freeze_revision(current, task)
        if job.kind is JobKind.ARCHIVE:
            try:
                with self.store.transaction() as tx:
                    fresh = tx.lock_workflow(flow.id)
                    current = tx.lock_job(job.id)
                    if fresh is None or current is None:
                        raise ValueError("archive workflow disappeared")
                    archive = archive_task(tx, fresh, current)
                    tx.freeze_archive(current, archive)
                    return archive
            except ValueError as error:
                await self.fail(job, str(error))
                return JobState.FAILED
        raise ValueError(f"unsupported job kind: {job.kind}")

    async def assignment_secret(self, job: Job, task: ExecutionTask) -> AssignmentSecret | None:
        if not isinstance(task, (InitialTask, RevisionTask)):
            return None
        flow = self.store.get_flow(job.flow_id)
        if flow is None or flow.installation_id is None:
            raise ValueError("coding assignment has no GitHub installation")
        capability = await self.github.create_push_capability(flow.installation_id, flow.repo)
        return AssignmentSecret(
            push=PushSecret(
                token=capability.token,
                repository=capability.repository,
                expires_at=capability.expires_at,
            )
        )

    async def execute_tool(self, request: FlowToolRequest, *, worker_id: str) -> FlowToolResponse:
        return await self.tools.execute(request, worker_id=worker_id)

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None:
        flow = self.store.get_flow(job.flow_id)
        if flow is None:
            raise ValueError(f"flow {job.flow_id} disappeared")
        if self._retired(self.store, flow, job):
            return
        attempt = self.store.get_attempt(task.attempt_id)
        if attempt is None:
            raise ValueError(f"attempt {task.attempt_id} disappeared")
        execution = self.store.get_flow_execution(flow.id)
        if execution is None or execution.worker_id != worker_id:
            raise ValueError(f"flow {flow.id} is not bound to worker {worker_id}")
        if job.kind is JobKind.REVISION:
            with self.store.transaction() as tx:
                fresh = tx.lock_workflow(flow.id)
                current = tx.lock_job(job.id)
                if fresh is None or current is None or self._retired(tx, fresh, job):
                    return
                tx.reviews.started(current)
        elif job.kind is JobKind.INITIAL:
            emit_hook(self.plugins, "on_flow_start", flow=flow)

    async def apply_result(self, job_id: int) -> JobState:
        """Apply the accepted result from the durable inbox, including recovery replays."""
        return await self.results.apply(job_id)

    async def fail(self, job: Job, reason: str) -> None:
        """Persist control failures too, so recovery and live failures use the same atomic finish."""
        with self.store.transaction() as tx:
            tx.lock_workflow(job.flow_id)
            current = tx.lock_job(job.id)
            if current is None or current.state is not JobState.RUNNING:
                return
            if current.result_received_at is None:
                message = Failed(
                    job_id=job.id,
                    assignment_token=current.assignment_token or "control",
                    connection_epoch="control",
                    error=reason,
                )
                tx.record_control_result(
                    job.id,
                    message.model_dump(mode="json"),
                    terminal_digest(message),
                    reason,
                )
        await self.results.apply(job.id)

    def _delivery(self, flow: Flow) -> FlowDelivery:
        delivery = self.store.get_delivery(flow.id)
        if delivery is None:
            raise ValueError(f"flow {flow.id} has no delivery")
        return delivery

    def _pr_number(self, flow: Flow) -> int:
        number = self._delivery(flow).pr_number
        if number is None:
            raise ValueError(f"flow {flow.id} has no pull request")
        return number
