"""Translate persisted jobs into remote tasks and apply remote results."""

from __future__ import annotations

from typing import Protocol

from loguru import logger

from app.control.tools import FlowToolRejected, FlowToolService
from app.github_models import GitHubAdapterError
from app.models import (
    TERMINAL_STATES,
    Attempt,
    DeliveryState,
    Flow,
    FlowDelivery,
    FlowState,
    Job,
    JobKind,
    JobState,
    Verdict,
)
from app.plugins import get_plugin_manager
from app.ports import GitHubClient
from app.protocol import (
    AnswerResult,
    AnswerTask,
    ArchiveResult,
    ArchiveTask,
    AssignmentSecret,
    ExecutionResult,
    ExecutionTask,
    FlowToolRequest,
    FlowToolResponse,
    InitialResult,
    InitialTask,
    PushSecret,
    ReviewFeedback,
    RevisionResult,
    RevisionTask,
    TriageResult,
    TriageTask,
)
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
        self.tools = FlowToolService(store, github, settings, self.plugins)

    async def prepare(self, job: Job) -> ExecutionTask | JobState:
        flow = self.store.get_flow(job.flow_id)
        if flow is None:
            return JobState.FAILED
        if flow.state in TERMINAL_STATES:
            return JobState.DONE

        if job.kind is JobKind.INITIAL:
            if flow.installation_id is None:
                await self._fail(flow, flow.issue_number, "Flow 缺少 GitHub installation。")
                return JobState.FAILED
            execution = self.store.get_flow_execution(flow.id)
            if execution is None or execution.released_at is not None:
                await self._fail(flow, flow.issue_number, "Flow 缺少 active execution binding。")
                return JobState.FAILED
            if job.assigned_worker_id and execution.worker_id != job.assigned_worker_id:
                await self._fail(flow, flow.issue_number, "Flow execution binding 与 assignment 不匹配。")
                return JobState.FAILED
            attempt = self.store.get_current_attempt(flow)
            if attempt is None:
                attempt = self.store.create_attempt(flow.id)
                flow.current_attempt_id = attempt.id
                self.store.update_flow(flow)
            delivery = self.store.get_or_create_delivery(flow.id, f"flow/{flow.id}")
            try:
                issue = await self.github.get_issue(
                    flow.installation_id, flow.repo, flow.issue_number
                )
                repository = await self.github.get_repository(flow.installation_id, flow.repo)
            except GitHubAdapterError as exc:
                await self._fail(flow, flow.issue_number, str(exc))
                return JobState.FAILED
            if repository.full_name != flow.repo:
                await self._fail(flow, flow.issue_number, "GitHub repository identity 不匹配。")
                return JobState.FAILED
            delivery.base_branch = repository.default_branch
            self.store.update_delivery(delivery)
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
            await self._fail(flow, flow.issue_number, f"{job.kind} job has no current attempt")
            return JobState.FAILED

        if job.kind is JobKind.TRIAGE:
            items = self.store.unrelayed_items(attempt.id)
            if not items:
                return JobState.DONE
            discussion = "\n".join(f"{item.author}: {item.body}" for item in items)
            return TriageTask(
                attempt_id=attempt.id,
                discussion=discussion,
                pr_context=self._pr_context(flow, attempt),
            )
        if job.kind is JobKind.REVISION:
            delivery = self._delivery(flow, attempt)
            if attempt.revision_cycles + 1 > self.settings.max_revision_cycles:
                await self._fail(flow, delivery.pr_number, "已达最大修订轮数，暂停。")
                return JobState.FAILED
            if not attempt.codex_session_id:
                await self._fail(flow, delivery.pr_number, "修订任务缺少 Codex session。")
                return JobState.FAILED
            return RevisionTask(
                flow_id=flow.id,
                attempt_id=attempt.id,
                repo=flow.repo,
                branch=delivery.branch,
                session_id=attempt.codex_session_id,
                instruction=(job.payload or {}).get(
                    "instruction", "Address the review feedback."
                ),
                feedback=[
                    ReviewFeedback(
                        kind=item.kind,
                        github_id=item.github_id,
                        author=item.author,
                        body=item.body,
                        path=item.path,
                        original_commit_id=item.original_commit_id,
                        diff_hunk=item.diff_hunk,
                    )
                    for item in self.store.unrelayed_items(attempt.id)
                ],
            )
        if job.kind is JobKind.ANSWER:
            return AnswerTask(
                attempt_id=attempt.id,
                question=(job.payload or {}).get("question", ""),
                pr_context=self._pr_context(flow, attempt),
            )
        if job.kind is JobKind.ARCHIVE:
            return ArchiveTask(
                flow_id=flow.id,
                attempt_id=attempt.id,
                repo=flow.repo,
            )
        raise ValueError(f"unsupported job kind: {job.kind}")

    async def assignment_secret(
        self, job: Job, task: ExecutionTask
    ) -> AssignmentSecret | None:
        if not isinstance(task, InitialTask):
            return None
        flow = self.store.get_flow(job.flow_id)
        if flow is None or flow.installation_id is None:
            raise ValueError("INITIAL assignment has no GitHub installation")
        capability = await self.github.create_push_capability(
            flow.installation_id, flow.repo
        )
        return AssignmentSecret(
            push=PushSecret(
                token=capability.token,
                repository=capability.repository,
                expires_at=capability.expires_at,
            )
        )

    async def execute_tool(
        self, request: FlowToolRequest, *, worker_id: str
    ) -> FlowToolResponse:
        return await self.tools.execute(request, worker_id=worker_id)

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None:
        flow = self.store.get_flow(job.flow_id)
        if flow is None:
            raise ValueError(f"flow {job.flow_id} disappeared")
        attempt = self.store.get_attempt(task.attempt_id)
        if attempt is None:
            raise ValueError(f"attempt {task.attempt_id} disappeared")
        execution = self.store.get_flow_execution(flow.id)
        if execution is None or execution.worker_id != worker_id:
            raise ValueError(
                f"flow {flow.id} is not bound to worker {worker_id}"
            )
        if job.kind is JobKind.REVISION:
            attempt.revision_cycles += 1
        elif job.kind is JobKind.INITIAL:
            self._fire("on_flow_start", flow=flow)
        self.store.update_attempt(attempt)

    async def apply_result(self, job: Job, result: ExecutionResult) -> JobState:
        _validate_result_kind(job.kind, result)
        flow = self.store.get_flow(job.flow_id)
        if flow is None:
            return JobState.FAILED
        if flow.state in TERMINAL_STATES:
            return JobState.DONE
        attempt = self.store.get_current_attempt(flow)

        if isinstance(result, InitialResult):
            if attempt is None:
                return JobState.FAILED
            attempt.codex_session_id = result.session_id
            self.store.update_attempt(attempt)
            self.store.record_codex_run(attempt.id, "initial", "", "", result.exit_code)
            if result.exit_code != 0 or not result.session_id:
                reason = f"编码失败（codex exit={result.exit_code}），未开 PR。"
                if result.failure_stage and result.failure_detail:
                    reason = (
                        f"编码失败（codex exit={result.exit_code}）；VCS {result.failure_stage}: "
                        f"{result.failure_detail}。未开 PR。"
                    )
                await self._fail(
                    flow,
                    flow.issue_number,
                    reason,
                )
                return JobState.FAILED
            delivery = self.store.get_delivery(flow.id)
            mismatch = self._initial_delivery_mismatch(job, attempt, result, delivery)
            if mismatch is not None:
                await self._fail(
                    flow,
                    (
                        delivery.pr_number
                        if delivery is not None and delivery.pr_number is not None
                        else flow.issue_number
                    ),
                    f"编码结果与已验证的 Flow delivery 不一致：{mismatch}。",
                )
                return JobState.FAILED
            assert result.open_pr_call_id is not None
            try:
                await self.tools.finalize_open_pr(flow.id, result.open_pr_call_id)
            except (FlowToolRejected, GitHubAdapterError) as exc:
                await self._fail(
                    flow,
                    flow.issue_number,
                    f"PR delivery failed after terminal validation: {exc}",
                )
                return JobState.FAILED
            return JobState.DONE

        if isinstance(result, TriageResult):
            if attempt is None:
                return JobState.FAILED
            self._fire("on_review", flow=flow)
            verdict = result.verdict
            if (
                verdict.verdict is Verdict.REVISE
                and verdict.confidence >= self.settings.confidence_threshold
            ):
                await self.github.comment(
                    _installation_id(flow),
                    flow.repo,
                    self._pr_number(flow, attempt),
                    f"按此修改中：{verdict.instruction}",
                )
                self.store.enqueue_job(
                    flow.id,
                    JobKind.REVISION,
                    payload={"instruction": verdict.instruction},
                )
            elif verdict.verdict is Verdict.ANSWER:
                discussion = "\n".join(
                    f"{item.author}: {item.body}"
                    for item in self.store.unrelayed_items(attempt.id)
                )
                self.store.enqueue_job(
                    flow.id, JobKind.ANSWER, payload={"question": discussion}
                )
            return JobState.DONE

        if isinstance(result, RevisionResult):
            if attempt is None:
                return JobState.FAILED
            if result.session_id:
                attempt.codex_session_id = result.session_id
                self.store.update_attempt(attempt)
            self.store.record_codex_run(attempt.id, "revision", "", "", result.exit_code)
            if result.exit_code != 0:
                await self._fail(
                    flow,
                    self._pr_number(flow, attempt),
                    f"修订失败（codex exit={result.exit_code}），本轮未更新。",
                )
                return JobState.FAILED
            await self._apply_codex_result(flow, attempt, result)
            items = self.store.unrelayed_items(attempt.id)
            self.store.mark_relayed([item.id for item in items if item.id is not None])
            installation_id = _installation_id(flow)
            pr_number = self._pr_number(flow, attempt)
            await self.github.request_reviewers(
                installation_id, flow.repo, pr_number, self.settings.reviewers
            )
            await self.github.set_labels(
                installation_id, flow.repo, pr_number, [self.settings.in_review_label]
            )
            return JobState.DONE

        if isinstance(result, AnswerResult):
            if attempt is None:
                return JobState.FAILED
            await self.github.comment(
                _installation_id(flow), flow.repo, self._pr_number(flow, attempt), result.answer
            )
            return JobState.DONE

        if isinstance(result, ArchiveResult):
            flow.state = FlowState.SUCCEEDED
            self.store.update_flow(flow)
            self.store.release_flow_execution(flow.id)
            self._fire("on_archived", flow=flow)
            logger.info("flow {} archived", flow.id)
            return JobState.DONE

        raise TypeError(f"unsupported execution result: {type(result).__name__}")

    async def fail(self, job: Job, reason: str) -> None:
        flow = self.store.get_flow(job.flow_id)
        if flow is None:
            return
        delivery = self.store.get_delivery(flow.id)
        number = delivery.pr_number if delivery and delivery.pr_number else flow.issue_number
        await self._fail(flow, number, reason)

    async def _apply_codex_result(
        self, flow: Flow, attempt: Attempt, result: RevisionResult
    ) -> None:
        installation_id = _installation_id(flow)
        pr_number = self._pr_number(flow, attempt)
        for addressed in result.output.addressed:
            thread_id = self._thread_id_for(attempt, addressed.comment_id)
            if thread_id:
                await self.github.resolve_review_thread(installation_id, thread_id)
            self.store.mark_resolved(attempt.id, addressed.comment_id)
        for deferred in result.output.deferred:
            await self.github.comment(
                installation_id,
                flow.repo,
                pr_number,
                f"@ {deferred.comment_id}: {deferred.reason}",
            )
        for question in result.output.needs_clarification:
            await self.github.comment(
                installation_id, flow.repo, pr_number, f"❓ {question}"
            )
        if result.output.summary:
            await self.github.comment(
                installation_id,
                flow.repo,
                pr_number,
                f"已按 review 更新：{result.output.summary}",
            )

    def _initial_delivery_mismatch(
        self,
        job: Job,
        attempt: Attempt,
        result: InitialResult,
        delivery: FlowDelivery | None,
    ) -> str | None:
        """Return the first violated invariant in a successful INITIAL delivery result."""

        if delivery is None:
            return "delivery is missing"
        if delivery.state not in {DeliveryState.OPENING, DeliveryState.OPEN}:
            return f"delivery state is {delivery.state.value}, expected opening or open"
        if result.vcs_backend is None:
            return "verified VCS backend is missing"
        if not result.pushed:
            return "worker did not report a verified push"
        if result.branch != delivery.branch:
            return "result branch does not match delivery branch"
        if not result.commit_sha:
            return "verified commit SHA is missing"
        if result.commit_sha != result.remote_sha:
            return "local and remote commit SHAs differ"
        if result.commit_sha != delivery.head_sha:
            return "result commit SHA does not match delivery head"
        if not result.open_pr_call_id:
            return "flow.open_pr call id is missing"

        tool_call = self.store.get_tool_call(result.open_pr_call_id)
        if tool_call is None:
            return "flow.open_pr call is not persisted"
        if (tool_call.flow_id, tool_call.attempt_id, tool_call.job_id) != (
            job.flow_id,
            attempt.id,
            job.id,
        ):
            return "flow.open_pr call fence does not match the result"
        if result.pull_request is None:
            return "pull request handle is missing"
        if tool_call.response != result.pull_request:
            return "pull request handle does not match the persisted tool response"
        if result.pull_request.get("head_sha") != delivery.head_sha:
            return "prepared delivery head SHA does not match delivery"
        return None

    async def _fail(self, flow: Flow, number: int | None, reason: str) -> None:
        logger.warning("flow {} failed: {}", flow.id, reason)
        flow.state = FlowState.FAILED
        self.store.update_flow(flow)
        self.store.release_flow_execution(flow.id)
        if number is None or flow.installation_id is None:
            return
        try:
            await self.github.comment(flow.installation_id, flow.repo, number, f"⚠️ {reason}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to post failure comment on {}: {}", number, exc)

    def _fire(self, hook: str, **kwargs: object) -> None:
        try:
            getattr(self.plugins.hook, hook)(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugin hook {} failed: {}", hook, exc)

    @staticmethod
    def _thread_id_for(attempt: Attempt, comment_id: int) -> str | None:
        return None

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

    def _pr_context(self, flow: Flow, attempt: Attempt) -> str:
        return f"{flow.repo} PR #{self._delivery(flow, attempt).pr_number} " \
            f"(issue #{flow.issue_number})"


def _validate_result_kind(kind: JobKind, result: ExecutionResult) -> None:
    expected = {
        JobKind.INITIAL: InitialResult,
        JobKind.TRIAGE: TriageResult,
        JobKind.REVISION: RevisionResult,
        JobKind.ANSWER: AnswerResult,
        JobKind.ARCHIVE: ArchiveResult,
    }[kind]
    if not isinstance(result, expected):
        raise TypeError(f"job kind {kind} cannot accept result kind {result.kind}")


def _installation_id(flow: Flow) -> int:
    if flow.installation_id is None:
        raise ValueError(f"flow {flow.id} has no GitHub installation")
    return flow.installation_id
