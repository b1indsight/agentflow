"""Translate persisted jobs into remote tasks and apply remote results."""

from __future__ import annotations

from typing import Protocol

from loguru import logger

from app.models import (
    TERMINAL_STATES,
    Attempt,
    Flow,
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
    ExecutionResult,
    ExecutionTask,
    InitialResult,
    InitialTask,
    ReviewFeedback,
    RevisionResult,
    RevisionTask,
    TriageResult,
    TriageTask,
)
from app.store import Store


class CoordinatorSettings(Protocol):
    allowlist: list[str]
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

    async def prepare(self, job: Job) -> ExecutionTask | JobState:
        flow = self.store.get_flow(job.flow_id)
        if flow is None:
            return JobState.FAILED
        if flow.state in TERMINAL_STATES:
            return JobState.DONE

        if job.kind is JobKind.INITIAL:
            attempt = self.store.get_current_attempt(flow)
            if attempt is None:
                attempt = self.store.create_attempt(flow.id)
                attempt.branch = f"flow/issue-{flow.issue_number}"
                self.store.update_attempt(attempt)
                flow.current_attempt_id = attempt.id
                self.store.update_flow(flow)
            return InitialTask(
                attempt_id=attempt.id,
                repo=flow.repo,
                issue_number=flow.issue_number,
                issue_title=f"issue #{flow.issue_number}",
                issue_body="",
                branch=_required_branch(attempt),
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
            if attempt.revision_cycles + 1 > self.settings.max_revision_cycles:
                await self._fail(flow, attempt.pr_number, "已达最大修订轮数，暂停。")
                return JobState.FAILED
            if not attempt.codex_session_id:
                await self._fail(flow, attempt.pr_number, "修订任务缺少 Codex session。")
                return JobState.FAILED
            return RevisionTask(
                attempt_id=attempt.id,
                repo=flow.repo,
                branch=_required_branch(attempt),
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
                attempt_id=attempt.id,
                repo=flow.repo,
                branches=[
                    current.branch
                    for current in self.store.list_attempts(flow.id)
                    if current.branch
                ],
            )
        raise ValueError(f"unsupported job kind: {job.kind}")

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None:
        flow = self.store.get_flow(job.flow_id)
        if flow is None:
            raise ValueError(f"flow {job.flow_id} disappeared")
        attempt = self.store.get_attempt(task.attempt_id)
        if attempt is None:
            raise ValueError(f"attempt {task.attempt_id} disappeared")
        if attempt.worker_id not in (None, worker_id):
            raise ValueError(
                f"attempt {attempt.id} is affined to {attempt.worker_id}, not {worker_id}"
            )
        attempt.worker_id = worker_id
        if job.kind is JobKind.REVISION:
            attempt.revision_cycles += 1
            flow.state = FlowState.REVISING
        elif job.kind is JobKind.INITIAL:
            flow.state = FlowState.CODING
            self._fire("on_flow_start", flow=flow)
        self.store.update_attempt(attempt)
        self.store.update_flow(flow)

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
            if result.exit_code != 0:
                await self._fail(
                    flow,
                    flow.issue_number,
                    f"编码失败（codex exit={result.exit_code}），未开 PR。",
                )
                return JobState.FAILED
            installation_id = _installation_id(flow)
            branch = _required_branch(attempt)
            pr_number = await self.github.open_pr(
                installation_id,
                flow.repo,
                head=branch,
                base="main",
                title=f"issue #{flow.issue_number} (closes #{flow.issue_number})",
                body=f"Closes #{flow.issue_number}\n\n{result.last_message}",
            )
            attempt.pr_number = pr_number
            self.store.update_attempt(attempt)
            flow.state = FlowState.PR_OPEN
            self.store.update_flow(flow)
            await self.github.request_reviewers(
                installation_id, flow.repo, pr_number, self.settings.allowlist
            )
            await self.github.set_labels(
                installation_id, flow.repo, pr_number, ["flow:in-review"]
            )
            self._fire("on_pr_opened", flow=flow, pr_number=pr_number)
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
                    _pr_number(attempt),
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
                    attempt.pr_number,
                    f"修订失败（codex exit={result.exit_code}），本轮未更新。",
                )
                return JobState.FAILED
            await self._apply_codex_result(flow, attempt, result)
            items = self.store.unrelayed_items(attempt.id)
            self.store.mark_relayed([item.id for item in items if item.id is not None])
            flow.state = FlowState.PR_OPEN
            self.store.update_flow(flow)
            installation_id = _installation_id(flow)
            pr_number = _pr_number(attempt)
            await self.github.request_reviewers(
                installation_id, flow.repo, pr_number, self.settings.allowlist
            )
            await self.github.set_labels(
                installation_id, flow.repo, pr_number, ["flow:in-review"]
            )
            return JobState.DONE

        if isinstance(result, AnswerResult):
            if attempt is None:
                return JobState.FAILED
            await self.github.comment(
                _installation_id(flow), flow.repo, _pr_number(attempt), result.answer
            )
            return JobState.DONE

        if isinstance(result, ArchiveResult):
            flow.state = FlowState.ARCHIVED
            self.store.update_flow(flow)
            self._fire("on_archived", flow=flow)
            logger.info("flow {} archived", flow.id)
            return JobState.DONE

        raise TypeError(f"unsupported execution result: {type(result).__name__}")

    async def fail(self, job: Job, reason: str) -> None:
        flow = self.store.get_flow(job.flow_id)
        if flow is None:
            return
        attempt = self.store.get_current_attempt(flow)
        number = attempt.pr_number if attempt and attempt.pr_number else flow.issue_number
        await self._fail(flow, number, reason)

    async def _apply_codex_result(
        self, flow: Flow, attempt: Attempt, result: RevisionResult
    ) -> None:
        installation_id = _installation_id(flow)
        pr_number = _pr_number(attempt)
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

    async def _fail(self, flow: Flow, number: int | None, reason: str) -> None:
        logger.warning("flow {} failed: {}", flow.id, reason)
        flow.state = FlowState.FAILED
        self.store.update_flow(flow)
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

    @staticmethod
    def _pr_context(flow: Flow, attempt: Attempt) -> str:
        return f"{flow.repo} PR #{attempt.pr_number} (issue #{flow.issue_number})"


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


def _required_branch(attempt: Attempt) -> str:
    if not attempt.branch:
        raise ValueError(f"attempt {attempt.id} has no branch")
    return attempt.branch


def _pr_number(attempt: Attempt) -> int:
    if attempt.pr_number is None:
        raise ValueError(f"attempt {attempt.id} has no pull request")
    return attempt.pr_number


def _installation_id(flow: Flow) -> int:
    if flow.installation_id is None:
        raise ValueError(f"flow {flow.id} has no GitHub installation")
    return flow.installation_id
