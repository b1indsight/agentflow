"""Execute protocol tasks against worker-local agents and workspaces."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from app.context_builder import build_initial_prompt, build_revision_prompt
from app.models import CodeAgentContext, CodexResult, InitialCodexResult
from app.ports import CodeAgent, Judge
from app.protocol import (
    AnswerResult,
    AnswerTask,
    ArchiveResult,
    ArchiveTask,
    AssignmentSecret,
    ExecutionResult,
    ExecutionTask,
    InitialResult,
    InitialTask,
    RevisionResult,
    RevisionTask,
    TriageResult,
    TriageTask,
)
from app.vcs import (
    VcsVerificationError,
    agent_guidance,
    cleanup_flow_repo,
    prepare_flow_repo,
    prepare_vcs_context,
    verify_vcs_outcome,
)
from app.worker.tools import FlowToolCaller, flow_tool_broker

VCS_VERIFICATION_EXIT_CODE = 65


class WorkerExecutor:
    """Run one assignment using only worker-local resources.

    The executor deliberately has no Store or GitHub client. Persistent state transitions and
    GitHub side effects remain owned by the control plane.
    """

    def __init__(
        self,
        worker_root: Path,
        code_agent: CodeAgent,
        judge: Judge,
        *,
        worker_id: str | None = None,
        tool_caller: FlowToolCaller | None = None,
    ) -> None:
        self.worker_root = worker_root
        self.code_agent = code_agent
        self.judge = judge
        self.worker_id = worker_id
        self.tool_caller = tool_caller

    async def execute(
        self, task: ExecutionTask, secret: AssignmentSecret | None = None
    ) -> ExecutionResult:
        if isinstance(task, InitialTask):
            return await self._initial(task, secret)
        if isinstance(task, TriageTask):
            verdict = await self.judge.triage(task.discussion, task.pr_context)
            return TriageResult(batch_id=task.batch_id, feedback_refs=list(task.feedback), verdict=verdict)
        if isinstance(task, RevisionTask):
            return await self._revision(task)
        if isinstance(task, AnswerTask):
            answer = await self.judge.answer(task.question, task.pr_context)
            return AnswerResult(batch_id=task.batch_id, feedback_refs=list(task.feedback), answer=answer)
        if isinstance(task, ArchiveTask):
            repo_path = self._repo_path(task.repo)
            await cleanup_flow_repo(repo_path, task.flow_id)
            return ArchiveResult()
        raise TypeError(f"unsupported execution task: {type(task).__name__}")

    async def _initial(
        self, task: InitialTask, secret: AssignmentSecret | None
    ) -> InitialResult:
        if self.worker_id is not None and task.repo_identity != f"{self.worker_id}:{task.repo}":
            return _vcs_failure(None, "", "binding", "repo identity does not match worker")
        if self.tool_caller is not None and secret is None:
            return _vcs_failure(None, "", "capability", "push capability is missing")
        if secret is not None:
            if secret.push.repository != task.repo:
                return _vcs_failure(None, "", "capability", "push capability repository mismatch")
            if secret.push.expires_at <= datetime.now(UTC):
                return _vcs_failure(None, "", "capability", "push capability expired")
        repo_path = self._repo_path(task.repo)
        workspace = await prepare_flow_repo(repo_path, task.flow_id)
        vcs_context = await prepare_vcs_context(workspace, task.branch)
        prompt = build_initial_prompt(
            task.issue_title,
            task.issue_body,
            self._repo_conventions(workspace),
            agent_guidance(task.branch),
        )
        schema_path = _write_output_schema(InitialCodexResult)
        context = CodeAgentContext(
            push_token=secret.push.token if secret else None,
            push_repository=secret.push.repository if secret else None,
        )
        tool_call_id: str | None = None
        tool_result: dict[str, object] | None = None
        tool_vcs_outcome = None
        try:
            if self.tool_caller is None:
                run = await self.code_agent.start(
                    prompt, workspace, output_schema=schema_path, context=context
                )
            else:
                async with flow_tool_broker(self.tool_caller, vcs_context) as broker:
                    run = await self.code_agent.start(
                        prompt,
                        workspace,
                        output_schema=schema_path,
                        context=CodeAgentContext(
                            push_token=context.push_token,
                            push_repository=context.push_repository,
                            mcp_socket=broker.socket_path,
                        ),
                    )
                    tool_call_id = broker.last_call_id
                    tool_result = broker.last_result
                    tool_vcs_outcome = broker.last_vcs_outcome
        finally:
            schema_path.unlink(missing_ok=True)
        if run.exit_code != 0:
            return InitialResult(
                session_id=run.session_id,
                last_message=run.last_message,
                exit_code=run.exit_code,
            )
        if run.output is None or run.output.vcs_backend_used is None:
            return _vcs_failure(run.session_id, run.last_message, "result", "missing VCS backend")
        try:
            outcome = await verify_vcs_outcome(vcs_context, run.output.vcs_backend_used)
        except VcsVerificationError as error:
            return _vcs_failure(run.session_id, run.last_message, error.stage, str(error))
        if self.tool_caller is not None and (tool_call_id is None or tool_result is None):
            return _vcs_failure(
                run.session_id,
                run.last_message,
                "delivery",
                "CodeAgent did not complete flow.open_pr",
            )
        if tool_vcs_outcome is not None and tool_vcs_outcome != outcome:
            return _vcs_failure(
                run.session_id,
                run.last_message,
                "delivery",
                "VCS outcome changed after flow.open_pr",
            )
        logger.info(
            "verified CodeAgent VCS outcome: backend={} branch={} commit={}",
            outcome.verified_backend,
            outcome.branch,
            outcome.commit_sha,
        )
        return InitialResult(
            session_id=run.session_id,
            last_message=run.last_message,
            exit_code=run.exit_code,
            vcs_backend=outcome.verified_backend,
            branch=outcome.branch,
            commit_sha=outcome.commit_sha,
            remote_sha=outcome.remote_sha,
            pushed=outcome.commit_sha == outcome.remote_sha,
            open_pr_call_id=tool_call_id,
            pull_request=tool_result,
        )

    async def _revision(self, task: RevisionTask) -> RevisionResult:
        prompt = build_revision_prompt(
            task.instruction,
            task.feedback,
            agent_guidance(task.branch),
        )
        schema_path = _write_output_schema(CodexResult)
        try:
            run = await self.code_agent.resume(
                task.session_id,
                prompt,
                output_schema=schema_path,
            )
        finally:
            schema_path.unlink(missing_ok=True)
        return RevisionResult(
            batch_id=task.batch_id,
            feedback_refs=list(task.feedback),
            session_id=run.session_id,
            last_message=run.last_message,
            exit_code=run.exit_code,
            output=run.output or CodexResult(),
        )

    def _repo_path(self, repo: str) -> Path:
        return self.worker_root / "repos" / repo

    @staticmethod
    def _repo_conventions(workspace: Path) -> str:
        for name in ("AGENTS.md", "CLAUDE.md"):
            path = workspace / name
            if path.exists():
                return path.read_text()
        return ""


def _write_output_schema(model: type[CodexResult | InitialCodexResult]) -> Path:
    """Write CodexResult's schema to a private temporary file for the Codex CLI."""

    with tempfile.NamedTemporaryFile(
        mode="w", prefix="codex-schema-", suffix=".json", delete=False
    ) as output:
        json.dump(model.model_json_schema(), output)
        return Path(output.name)


def _vcs_failure(
    session_id: str | None, last_message: str, stage: str, detail: str
) -> InitialResult:
    message = f"VCS verification failed [{stage}]: {detail}"
    if last_message:
        message = f"{last_message.rstrip()}\n\n{message}"
    return InitialResult(
        session_id=session_id,
        last_message=message,
        exit_code=VCS_VERIFICATION_EXIT_CODE,
        failure_stage=stage,
        failure_detail=detail,
    )
