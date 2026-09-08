"""Translate persisted jobs into remote tasks and apply remote results."""

from __future__ import annotations

from typing import Protocol

from loguru import logger

from app.control.archive import archive_task
from app.control.results import ResultApplication
from app.control.revisions import verify_revision_remote
from app.control.tools import FlowToolService
from app.github_models import GitHubAdapterError, ReviewThreads
from app.models import (
    TERMINAL_STATES,
    Attempt,
    Flow,
    FlowDelivery,
    Job,
    JobKind,
    JobState,
    ReviewItemKind,
)
from app.plugins import get_plugin_manager
from app.ports import GitHubClient
from app.protocol import (
    ArchiveResult,
    AssignmentSecret,
    ExecutionResult,
    ExecutionTask,
    Failed,
    FlowToolRequest,
    FlowToolResponse,
    InitialResult,
    InitialTask,
    PushSecret,
    RevisionResult,
    RevisionTask,
    TriageResult,
    TriageTask,
    terminal_digest,
)
from app.review_context import build_review_context
from app.review_models import BatchState
from app.store import Store


class CoordinatorSettings(Protocol):
    allowlist: list[str]
    reviewers: list[str]
    in_review_label: str
    confidence_threshold: float
    max_revision_cycles: int


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
                issue = await self.github.get_issue(installation_id, flow.repo, flow.issue_number)
                repository = await self.github.get_repository(installation_id, flow.repo)
            except GitHubAdapterError as exc:
                await self.fail(job, str(exc))
                return JobState.FAILED
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

        if job.kind in {JobKind.TRIAGE, JobKind.REVISION}:
            batch = self.store.reviews.for_job(job)
            if batch.attempt_id != attempt.id or batch.state not in {
                BatchState.READY, BatchState.PROCESSING,
            }:
                raise ValueError("review batch is no longer current")

        if job.kind in {JobKind.TRIAGE, JobKind.REVISION} and batch.threads is None and any(
            f.kind is ReviewItemKind.INLINE for f in batch.feedback
        ):
            assert flow.installation_id is not None
            number = self._pr_number(flow, attempt)
            try:
                threads = await self.github.list_review_threads(flow.installation_id, flow.repo, number)
            except GitHubAdapterError as exc:
                threads = ReviewThreads(repo=flow.repo, pr_number=number, fetched_at=self.store.now(),
                                        limitations=[f"threads:{exc.kind.value}"])
            with self.store.transaction() as tx:
                fresh = tx.lock_workflow(flow.id)
                current = tx.lock_job(job.id)
                if (fresh is None or current is None or self._retired(tx, fresh, job)
                        or fresh.current_attempt_id != attempt.id):
                    return JobState.DONE
                batch.threads = tx.reviews.freeze_threads(current, threads)

        if job.kind is JobKind.TRIAGE:
            context = batch.context
            if context is None:
                context = await build_review_context(
                    self.github, flow, batch, self._pr_number(flow, attempt),
                )
                with self.store.transaction() as tx:
                    fresh = tx.lock_workflow(flow.id)
                    current = tx.lock_job(job.id)
                    if (fresh is None or current is None or self._retired(tx, fresh, job)
                            or fresh.current_attempt_id != attempt.id):
                        return JobState.DONE
                    if job.assignment_token is not None and (
                        current.state is not JobState.RUNNING
                        or current.assigned_worker_id != job.assigned_worker_id
                        or current.assignment_token != job.assignment_token
                        or current.lease_expires_at is None
                        or current.lease_expires_at <= tx.now()
                    ):
                        return JobState.QUEUED
                    context = tx.reviews.freeze_context(current, context)
            return TriageTask(
                batch_id=batch.id,
                feedback=list(batch.feedback),
                attempt_id=attempt.id,
                context=context,
            )
        if job.kind is JobKind.REVISION:
            delivery = self._delivery(flow, attempt)
            if attempt.revision_cycles + 1 > self.settings.max_revision_cycles:
                await self.fail(job, "已达最大修订轮数，暂停。")
                return JobState.FAILED
            execution = self.store.get_flow_execution(flow.id)
            if execution is None or execution.released_at is not None:
                await self.fail(job, "修订任务缺少有效 worker binding。")
                return JobState.FAILED
            try:
                mismatch = await verify_revision_remote(
                    self.github, flow, delivery, delivery.head_sha or "",
                )
                if not mismatch:
                    assert flow.installation_id is not None
                    issue = await self.github.get_issue(
                        flow.installation_id, flow.repo, flow.issue_number,
                    )
            except GitHubAdapterError as exc:
                # Preparation has not consumed a revision cycle. Persist the failure so
                # transient outages can be retried explicitly without reconnect loops.
                await self.fail(job, str(exc))
                return JobState.FAILED
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
        if job.kind in {JobKind.TRIAGE, JobKind.REVISION}:
            with self.store.transaction() as tx:
                fresh = tx.lock_workflow(flow.id)
                current = tx.lock_job(job.id)
                if fresh is None or current is None or self._retired(tx, fresh, job):
                    return
                tx.reviews.started(current)
        elif job.kind is JobKind.INITIAL:
            self._fire("on_flow_start", flow=flow)

    async def apply_result(self, job: Job, result: ExecutionResult) -> JobState:
        _validate_result_kind(job.kind, result)
        return await self.results.apply(job.id)

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

    def _fire(self, hook: str, **kwargs: object) -> None:
        try:
            getattr(self.plugins.hook, hook)(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugin hook {} failed: {}", hook, exc)

    def _delivery(self, flow: Flow, attempt: Attempt) -> FlowDelivery:
        delivery = self.store.get_delivery(flow.id)
        if delivery is None:
            raise ValueError(f"flow {flow.id} has no delivery")
        return delivery

    def _pr_number(self, flow: Flow, attempt: Attempt) -> int:
        number = self._delivery(flow, attempt).pr_number
        if number is None:
            raise ValueError(f"flow {flow.id} has no pull request")
        return number

def _validate_result_kind(kind: JobKind, result: ExecutionResult) -> None:
    expected = {
        JobKind.INITIAL: InitialResult,
        JobKind.TRIAGE: TriageResult,
        JobKind.REVISION: RevisionResult,
        JobKind.ARCHIVE: ArchiveResult,
    }[kind]
    if not isinstance(result, expected):
        raise TypeError(f"job kind {kind} cannot accept result kind {result.kind}")
