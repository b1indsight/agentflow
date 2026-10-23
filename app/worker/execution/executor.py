"""Execute protocol tasks against worker-local agents and workspaces."""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from loguru import logger
from pydantic import ValidationError

from app.contracts.protocol import (
    ArchiveResult,
    ArchiveTask,
    AssignmentSecret,
    ExecutionResult,
    ExecutionTask,
    InitialTask,
    InitialTaskResult,
    RevisionExecutionRecord,
    RevisionTask,
    RevisionTaskResult,
)
from app.domain.delivery_models import DeliveryReceipt
from app.domain.models import (
    CodingExecutionContext,
    GitHubCapability,
    InitialCodingOutput,
    RevisionCodingOutput,
    SessionUnavailable,
)
from app.domain.ports import CodingAgent
from app.worker.execution.gh import require_gh
from app.worker.execution.prompts import build_initial_prompt, build_revision_prompt
from app.worker.execution.vcs import (
    VcsVerificationError,
    agent_guidance,
    prepare_revision_context,
    prepare_vcs_context,
    verify_revision_outcome,
    verify_vcs_outcome,
)
from app.worker.execution.workspaces import RepositoryWorkspaces
from app.worker.state.session import WorkerSession
from app.worker.transport.mcp import FlowToolCaller, WorkerMCP

VCS_VERIFICATION_EXIT_CODE = 65


class WorkerExecutor:
    """Run one assignment using only worker-local resources.

    The executor deliberately has no Store or GitHub client. Persistent state transitions and
    GitHub side effects remain owned by the control plane.
    """

    def __init__(
        self,
        worker_root: Path,
        code_agent: CodingAgent,
        *,
        worker_id: str,
        session: WorkerSession,
        revision_timeout: float = 1200.0,
        workspace_limit: int = 2,
    ) -> None:
        self.worker_root = worker_root
        self.code_agent = code_agent
        self.worker_id = worker_id
        self.session = session
        self.revision_timeout = revision_timeout
        self.workspaces = RepositoryWorkspaces(worker_root, worker_id, limit=workspace_limit)

    async def execute(
        self, task: ExecutionTask, secret: AssignmentSecret | None = None,
        *, tool_caller: FlowToolCaller
    ) -> ExecutionResult:
        """Use the active assignment caller without retaining a client/executor reference cycle."""
        if isinstance(task, InitialTask):
            return await self._initial(task, secret, tool_caller)
        if isinstance(task, RevisionTask):
            try:
                async with asyncio.timeout(self.revision_timeout):
                    return await self._revision(task, secret)
            except TimeoutError as error:
                raise TimeoutError("revision deadline expired; edits or a push may have occurred") from error
        if isinstance(task, ArchiveTask):
            if (self.worker_id != task.worker_id
                    or task.repo_identity != f"{self.worker_id}:{task.repo}"):
                raise ValueError("archive repository identity does not match worker")
            binding = self.session.verify_cleanup(task)
            cleanup = await self.workspaces.release(binding)
            return ArchiveResult(**task.model_dump(), cleanup=cleanup)
        raise TypeError(f"unsupported execution task: {type(task).__name__}")

    async def reclaim_workspace(self) -> None:
        """Clean and recycle the stopped session's allocation before reporting it stopped."""
        record = self.session.load()
        if record is None or record.stopped is None:
            raise VcsVerificationError("workspace", "stopped worker session is missing")
        if record.workspace is None:
            self.workspaces.assert_unallocated(record.flow_id)
            return
        if record.workspace.flow_id != record.flow_id:
            raise VcsVerificationError("workspace", "cancelled workspace belongs to another Flow")
        await self.workspaces.release(record.workspace, recycle=True)

    async def _initial(
        self, task: InitialTask, secret: AssignmentSecret | None, tool_caller: FlowToolCaller
    ) -> InitialTaskResult:
        if task.repo_identity != f"{self.worker_id}:{task.repo}":
            return _vcs_failure(None, "", "binding", "repo identity does not match worker")
        try:
            capability = _github_capability(secret, task.repo)
            binding = await self.workspaces.acquire(
                task.repo, task.flow_id, base_branch=task.base_branch, remote=task.remote,
                push_token=capability.token,
            )
            self.session.bind_workspace(binding)
            workspace = self.workspaces.path(binding)
            vcs_context = replace(
                await prepare_vcs_context(workspace, task.branch, remote=task.remote),
                push_token=capability.token,
            )
        except VcsVerificationError as error:
            return _vcs_failure(None, "", error.stage, str(error))
        except OSError:
            return _vcs_failure(None, "", "bootstrap", "repository filesystem preparation failed")
        prompt = build_initial_prompt(
            task.issue_title,
            task.issue_body,
            self._repo_conventions(workspace),
            agent_guidance(task.branch, remote=task.remote),
        )
        schema_path = _write_output_schema(InitialCodingOutput)
        tool_call_id: str | None = None
        tool_result: DeliveryReceipt | None = None
        tool_vcs_outcome = None
        try:
            async with WorkerMCP(tool_caller, vcs_context) as mcp:
                run = await self.code_agent.start(
                    prompt,
                    workspace,
                    output_schema=schema_path,
                    context=CodingExecutionContext(
                        github=capability,
                        mcp_url=mcp.url,
                        mcp_token=mcp.token,
                    ),
                )
                tool_call_id = mcp.last_call_id
                tool_result = mcp.last_result
                tool_vcs_outcome = mcp.last_vcs_outcome
        except VcsVerificationError as error:
            return _vcs_failure(None, "", error.stage, str(error))
        finally:
            schema_path.unlink(missing_ok=True)
        if run.exit_code != 0:
            return InitialTaskResult(
                session_id=run.session_id,
                last_message=run.last_message,
                exit_code=run.exit_code,
            )
        try:
            output = InitialCodingOutput.model_validate_json(run.last_message)
        except ValidationError:
            # Validation errors include raw input, which may contain credentials.
            return _vcs_failure(run.session_id, run.last_message, "result", "missing VCS backend")
        try:
            outcome = await verify_vcs_outcome(vcs_context, output.vcs_backend_used)
        except VcsVerificationError as error:
            return _vcs_failure(run.session_id, run.last_message, error.stage, str(error))
        if (tool_call_id is None or tool_result is None):
            return _vcs_failure(
                run.session_id,
                run.last_message,
                "delivery",
                "CodingAgent did not complete flow.open_pr",
            )
        if tool_vcs_outcome is not None and tool_vcs_outcome != outcome:
            return _vcs_failure(
                run.session_id,
                run.last_message,
                "delivery",
                "VCS outcome changed after flow.open_pr",
            )
        logger.info(
            "verified CodingAgent VCS outcome: backend={} branch={} commit={}",
            outcome.backend,
            outcome.branch,
            outcome.commit_sha,
        )
        return InitialTaskResult(
            session_id=run.session_id,
            last_message=run.last_message,
            exit_code=run.exit_code,
            vcs_backend=outcome.backend,
            branch=outcome.branch,
            commit_sha=outcome.commit_sha,
            remote_sha=outcome.remote_sha,
            pushed=outcome.commit_sha == outcome.remote_sha,
            open_pr_call_id=tool_call_id,
            delivery_receipt=tool_result,
        )

    async def _revision(
        self, task: RevisionTask, secret: AssignmentSecret | None,
    ) -> RevisionTaskResult:
        """Resume with fresh credentials, falling back once only after proven non-execution."""
        result = RevisionTaskResult(
            batch_id=task.batch_id, feedback_refs=list(task.feedback), flow_id=task.flow_id,
            attempt_id=task.attempt_id, previous_session_id=task.session_id,
            session_id=task.session_id, session_generation=task.session_generation,
            mode="resume" if task.session_id else "fallback", last_message="", exit_code=65,
        )
        schema_path: Path | None = None
        try:
            if task.repo_identity != f"{self.worker_id}:{task.repo}":
                raise VcsVerificationError("binding", "repo identity does not match worker")
            capability = _github_capability(secret, task.repo)
            binding = self.workspaces.lookup(task.repo, task.flow_id)
            self.session.bind_workspace(binding)
            workspace = self.workspaces.path(binding)
            context = CodingExecutionContext(github=capability)
            vcs_context = await prepare_revision_context(
                workspace, task.branch, task.expected_head_sha, capability.token,
            )
            prompt = build_revision_prompt(task, self._repo_conventions(workspace))
            schema_path = _write_output_schema(RevisionCodingOutput)
            run = None
            if task.session_id:
                self.session.begin_revision("resume")
                try:
                    run = await self.code_agent.resume(
                        task.session_id, prompt, cwd=workspace, output_schema=schema_path, context=context,
                    )
                except SessionUnavailable:
                    invocation = RevisionExecutionRecord(mode="resume", session_id=task.session_id,
                                                    exit_code=1, failure="session_unavailable")
                    result.invocations = self.session.finish_invocation(invocation)
                    # A rejection is insufficient if anything changed while resume was attempted.
                    fallback_context = await prepare_revision_context(
                        workspace, task.branch, task.expected_head_sha, capability.token,
                    )
                    if ((fallback_context.git_remote_url, fallback_context.jj_remote_url)
                            != (vcs_context.git_remote_url, vcs_context.jj_remote_url)):
                        raise VcsVerificationError("push", "remote changed after session rejection")
            if run is None:
                result.mode = "fallback"
                result.session_generation += 1
                self.session.begin_revision("fallback")
                run = await self.code_agent.start(
                    prompt, workspace, output_schema=schema_path, context=context,
                )
            invocation = RevisionExecutionRecord(mode=result.mode, session_id=run.session_id,
                                            exit_code=run.exit_code)
            result.invocations = self.session.finish_invocation(invocation)
            result.session_id = run.session_id
            result.last_message = run.last_message
            if run.exit_code != 0:
                result.exit_code = run.exit_code
                result.failure_stage = "execution"
                result.failure_detail = (run.failure_detail
                                         or "CodingAgent exited unsuccessfully; execution may have occurred")
                return result
            try:
                output = RevisionCodingOutput.model_validate_json(run.last_message)
            except ValidationError:
                raise VcsVerificationError("result", "missing strict revision output or session") from None
            if not run.session_id:
                raise VcsVerificationError("result", "missing strict revision output or session")
            if result.mode == "resume" and run.session_id != task.session_id:
                raise VcsVerificationError("session", "resume returned a different session")
            result.output = output
            outcome = await verify_revision_outcome(vcs_context, output.vcs_backend_used)
            result.vcs_backend = outcome.backend
            result.branch = outcome.branch
            result.commit_sha = outcome.commit_sha
            result.remote_sha = outcome.remote_sha
            result.clean_worktree = True
            result.exit_code = 0
        except VcsVerificationError as error:
            result.failure_stage = error.stage
            result.failure_detail = str(error)
        finally:
            if schema_path is not None:
                schema_path.unlink(missing_ok=True)
        return result

    @staticmethod
    def _repo_conventions(workspace: Path) -> str:
        for name in ("AGENTS.md", "CLAUDE.md"):
            path = workspace / name
            if path.exists():
                return path.read_text()
        return ""


def _github_capability(secret: AssignmentSecret | None, repository: str) -> GitHubCapability:
    if secret is None:
        raise VcsVerificationError("capability", "push capability is missing")
    capability = GitHubCapability(secret.push.token, secret.push.repository, secret.push.expires_at)
    require_gh(capability, repository)
    return capability


def _write_output_schema(model: type[RevisionCodingOutput | InitialCodingOutput]) -> Path:
    """Write the caller's schema to a private temporary file for its CodingAgent."""

    with tempfile.NamedTemporaryFile(
        mode="w", prefix="coding-schema-", suffix=".json", delete=False
    ) as output:
        json.dump(model.model_json_schema(), output)
        return Path(output.name)


def _vcs_failure(
    session_id: str | None, last_message: str, stage: str, detail: str
) -> InitialTaskResult:
    message = f"VCS verification failed [{stage}]: {detail}"
    if last_message:
        message = f"{last_message.rstrip()}\n\n{message}"
    return InitialTaskResult(
        session_id=session_id,
        last_message=message,
        exit_code=VCS_VERIFICATION_EXIT_CODE,
        failure_stage=stage,
        failure_detail=detail,
    )
