"""Prepare worker assignments and interpret frozen review decisions."""

from __future__ import annotations

from datetime import timedelta

from pluggy import PluginManager

from app.contracts.protocol import (
    AssignmentArguments,
    AssignmentSecret,
    ExecutionTask,
    Failed,
    InitialTask,
    PushSecret,
    RevisionTask,
)
from app.controler.config import ServerSettings
from app.controler.review.context import build_review_context
from app.controler.workflow.archive import prepare_archive
from app.controler.workflow.initial import InitialPreparation
from app.controler.workflow.preparation import (
    PreparationReadFailure,
    PreparationReads,
    PreparationStale,
    PreparationState,
    preparation_retired,
    required_pr_number,
)
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.revisions import prepare_revision
from app.domain.models import (
    DeliveryState,
    Job,
    JobKind,
    JobState,
    TriageVerdict,
    Verdict,
)
from app.domain.ports import Judge
from app.domain.review_models import BatchState, ReviewBatch
from app.github import GitHubApp
from app.persistence.assignments import AssignmentStore
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.reviews import ReviewStore
from app.persistence.settings import SettingsStore
from app.persistence.workers import WorkerStore
from app.plugins import emit_hook, get_plugin_manager


class JobCoordinator:
    """Build worker tasks and interpret review decisions, delegating durable result application."""

    def __init__(
        self,
        github: GitHubApp,
        settings: ServerSettings,
        plugins: PluginManager | None = None,
        *,
        database: Database,
        results: ResultApplication,
    ) -> None:
        self.database = database
        self.deliveries = DeliveryStore(database)
        self.flows = FlowStore(database)
        self.jobs = JobStore(database)
        self.reviews = ReviewStore(database)
        self.workers = WorkerStore(database)
        self.assignments = AssignmentStore(database)
        self.reads = PreparationReads(database, settings)
        self.github = github
        self.settings = settings
        self.plugins = plugins or get_plugin_manager()
        self.results = results

    async def triage(self, batch: ReviewBatch, judge: Judge) -> None:
        """Resolve feedback into an answer/revise worker task or a wait/ignore decision."""
        with self.flows.locked(batch.flow_id) as locked:
            tx = locked.database
            reviews_tx = self.reviews.using(tx)
            flow = locked.flow
            if not reviews_tx.current(batch):
                return
        assert flow is not None and flow.installation_id is not None
        repo = self.github.installation(flow.installation_id).repository(flow.repo)
        context = batch.context or await build_review_context(
            repo, flow, batch, required_pr_number(flow, deliveries=self.deliveries),
        )
        with self.flows.locked(flow.id) as locked:
            tx = locked.database
            reviews_tx = self.reviews.using(tx)
            if not reviews_tx.current(batch):
                return
            context = reviews_tx.freeze_context(batch, context)
            reviews_tx.relayed(batch)
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
        # Remote reads can outlive close/merge or a changed PR baseline.
        remote = (await repo.fetch_pull_request(context.pr_number)
                  if verdict.verdict in {Verdict.ANSWER, Verdict.REVISE} else None)
        with self.flows.locked(flow.id) as locked:
            tx = locked.database
            jobs_tx = self.jobs.using(tx)
            reviews_tx = self.reviews.using(tx)
            if not reviews_tx.current(batch):
                return
            assert locked.flow is not None
            if remote is not None and (context.pull_request is None or remote.state != "open" or remote.merged
                    or remote.head_sha != context.pull_request.head_sha
                    or remote.base_sha != context.pull_request.base_sha):
                reviews_tx.invalidate_context(batch, context)
                return
            reviews_tx.record_decision(batch, decision.model_copy(update={"verdict": verdict}))
            if verdict.verdict in {Verdict.ANSWER, Verdict.REVISE}:
                job = jobs_tx.enqueue_job(locked.flow, JobKind.REVISION,
                    {"batch_id": batch.id, "instruction": verdict.instruction,
                     "purpose": verdict.verdict.value})
                reviews_tx.advance(batch.id, job)
            else:
                reviews_tx.complete(batch.id,
                    BatchState.COMPLETED if verdict.verdict is Verdict.IGNORE else BatchState.WAITING,
                    list(batch.feedback) if verdict.verdict is Verdict.IGNORE else [],
                    verdict.open_points or ([verdict.reason or "Awaiting more discussion."]
                        if verdict.verdict is Verdict.WAIT else []))
        emit_hook(self.plugins, "on_review", flow=flow)

    async def prepare_for_assignment(
        self,
        job: Job,
        worker: str,
        token: str,
        epoch: str,
    ) -> AssignmentArguments | PreparationState:
        """Build an unstarted assignment, keeping read retries separate from signing failures."""
        prepared = await self._prepare_task(job, worker, token, epoch)
        if isinstance(prepared, JobState):
            return prepared
        model = None
        effort = None
        if job.kind is not JobKind.ARCHIVE:
            owner = next((item for item in self.workers.list_workspaces(worker)
                          if item.repository is None and item.connection_epoch == epoch), None)
            if owner is None or owner.coding_agent is None:
                raise PreparationStale("coding worker identity changed during preparation")
            defaults = SettingsStore(self.database).get()
            model = defaults.codex_model if owner.coding_agent == "codex" else defaults.amp_model
            effort = defaults.codex_effort if owner.coding_agent == "codex" else defaults.amp_effort
        try:
            secret = await self._assignment_secret(job, prepared)
        except Exception as exc:  # noqa: BLE001 - signing errors become durable control failures
            if not self.assignments.clear_preparation(job.id, worker, token, epoch):
                raise PreparationStale("assignment expired during capability preparation") from None
            await self.fail(job, f"assignment capability failed: {type(exc).__name__}")
            return JobState.FAILED
        # Signing may outlive the lease or race a webhook; validate even without a gateway.
        if not self.assignments.clear_preparation(job.id, worker, token, epoch):
            raise PreparationStale("assignment changed before dispatch")
        delivery = self.deliveries.get_delivery(job.required_flow_id)
        if delivery is not None and delivery.supersedes(job.kind):
            if delivery.state is DeliveryState.CLOSED:
                return JobState.DONE
            await self.fail(job, "PR merged before task dispatch")
            return JobState.FAILED
        if job.workspace is None or job.generation is None:
            raise PreparationStale("assignment has no claimed workspace")
        return AssignmentArguments(
            job_id=job.id, flow_id=job.required_flow_id, assignment_token=token,
            workspace=job.workspace, generation=job.generation, task=prepared, secret=secret,
            model=model, effort=effort,
        )

    async def _prepare_task(
        self, job: Job, worker: str, token: str, epoch: str,
    ) -> ExecutionTask | PreparationState:
        """Bound read preparation for remote assignments."""
        error = None
        expired = (job.prepare_first_failed_at is not None and self.database.now()
                   >= job.prepare_first_failed_at + timedelta(seconds=self.settings.prepare_retry_window_seconds))
        if not expired:
            try:
                prepared = await self.prepare(job)
            except PreparationReadFailure as failure:
                error = failure.error
            else:
                if isinstance(prepared, JobState):
                    assert prepared in (JobState.QUEUED, JobState.DONE, JobState.FAILED)
                if not isinstance(prepared, JobState) and not self.assignments.clear_preparation(job.id, worker, token, epoch):
                    raise PreparationStale("preparation changed before dispatch")
                return prepared
        state = self.assignments.defer_preparation(
            job.id, worker, token, epoch, error, max_failures=self.settings.prepare_max_failures,
            window_seconds=self.settings.prepare_retry_window_seconds,
        )
        if state is None:
            raise PreparationStale("preparation changed before retry")
        assert state in (JobState.QUEUED, JobState.DONE, JobState.FAILED)
        if state is JobState.FAILED and worker is not None:
            await self.results.apply(job.id)
        return state

    async def prepare(self, job: Job) -> ExecutionTask | JobState:
        """Build a task from a persisted job and its current workflow state.

        Remote dispatch callers should use ``prepare_for_assignment`` after claiming
        the job; that boundary also handles read retries, signing, and final fences.
        This method may create INITIAL intent or freeze REVISION/ARCHIVE inputs.
        GitHub reads run outside transactions, with state rechecked before freezing.

        Return an execution task when ready, DONE when the work is retired, FAILED
        when prerequisites fail, or QUEUED when revision assignment checks change.
        PreparationReadFailure leaves retry scheduling to the assignment boundary;
        invalid internal state raises ValueError. Failure handling may itself raise
        ResultPending while durable failure effects await recovery.
        """
        flow = self.flows.get_flow(job.required_flow_id)
        if flow is None:
            return JobState.FAILED
        if preparation_retired(flow, job, deliveries=self.deliveries):
            return JobState.DONE

        if job.kind is JobKind.INITIAL:
            return await InitialPreparation(
                self.github, database=self.database, reads=self.reads, fail=self.fail,
                apply_result=self.results.apply,
            ).prepare(job, flow)

        attempt = self.flows.get_current_attempt(flow)
        if attempt is None:
            await self.fail(job, f"{job.kind} job has no current attempt")
            return JobState.FAILED

        if job.kind is JobKind.REVISION:
            return await prepare_revision(
                job, flow, attempt, github=self.github, reads=self.reads,
                flows=self.flows, deliveries=self.deliveries, reviews=self.reviews,
                settings=self.settings, fail=self.fail,
            )
        if job.kind is JobKind.ARCHIVE:
            return await prepare_archive(
                job, flow, flows=self.flows, deliveries=self.deliveries,
                jobs=self.jobs, fail=self.fail,
            )
        raise ValueError(f"unsupported job kind: {job.kind}")

    async def _assignment_secret(self, job: Job, task: ExecutionTask) -> AssignmentSecret | None:
        if not isinstance(task, (InitialTask, RevisionTask)):
            return None
        flow = self.flows.get_flow(job.required_flow_id)
        if flow is None or flow.installation_id is None:
            raise ValueError("coding assignment has no GitHub installation")
        capability = await self.github.installation(flow.installation_id).create_push_capability(flow.repo)
        return AssignmentSecret(
            push=PushSecret(
                token=capability.token,
                repository=capability.repository,
                expires_at=capability.expires_at,
                bot_login=capability.bot_login,
                bot_user_id=capability.bot_user_id,
            )
        )

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None:
        flow = self.flows.get_flow(job.required_flow_id)
        if flow is None:
            raise ValueError(f"flow {job.flow_id} disappeared")
        if preparation_retired(flow, job, deliveries=self.deliveries):
            return
        attempt = self.flows.get_attempt(task.attempt_id)
        if attempt is None:
            raise ValueError(f"attempt {task.attempt_id} disappeared")
        execution = self.flows.get_flow_execution(flow.id)
        if execution is None or execution.worker_id != worker_id:
            raise ValueError(f"flow {flow.id} is not bound to worker {worker_id}")
        if job.kind is JobKind.REVISION:
            with self.flows.locked(flow.id) as locked:
                tx = locked.database
                deliveries_tx = self.deliveries.using(tx)
                reviews_tx = self.reviews.using(tx)
                fresh = locked.flow
                current = tx.lock_job(job.id)
                if (
                    fresh is None
                    or current is None
                    or preparation_retired(fresh, job, deliveries=deliveries_tx)
                ):
                    return
                reviews_tx.started(current)
        elif job.kind is JobKind.INITIAL:
            emit_hook(self.plugins, "on_flow_start", flow=flow)

    async def fail(self, job: Job, reason: str) -> None:
        """Persist control failures too, so recovery and live failures use the same atomic finish."""
        with self.flows.locked(job.required_flow_id) as locked:
            tx = locked.database
            jobs_tx = self.jobs.using(tx)
            current = tx.lock_job(job.id)
            if current is None or current.state is not JobState.RUNNING:
                return
            if current.result_received_at is None:
                message = Failed(
                    job_id=job.id,
                    assignment_token=current.assignment_token or "control",
                    error=reason,
                )
                jobs_tx.record_control_result(job.id, message, reason)
        await self.results.apply(job.id)
