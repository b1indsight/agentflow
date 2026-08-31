"""Execute protocol tasks against worker-local agents and workspaces."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.context_builder import build_initial_prompt, build_revision_prompt
from app.models import CodexResult
from app.ports import CodeAgent, Judge
from app.protocol import (
    AnswerResult,
    AnswerTask,
    ArchiveResult,
    ArchiveTask,
    ExecutionResult,
    ExecutionTask,
    InitialResult,
    InitialTask,
    RevisionResult,
    RevisionTask,
    TriageResult,
    TriageTask,
)
from app.vcs import agent_guidance, cleanup_workspace, make_workspace, workspace_path


class WorkerExecutor:
    """Run one assignment using only worker-local resources.

    The executor deliberately has no Store or GitHub client. Persistent state transitions and
    GitHub side effects remain owned by the control plane.
    """

    def __init__(self, worker_root: Path, code_agent: CodeAgent, judge: Judge) -> None:
        self.worker_root = worker_root
        self.code_agent = code_agent
        self.judge = judge

    async def execute(self, task: ExecutionTask) -> ExecutionResult:
        if isinstance(task, InitialTask):
            return await self._initial(task)
        if isinstance(task, TriageTask):
            verdict = await self.judge.triage(task.discussion, task.pr_context)
            return TriageResult(verdict=verdict)
        if isinstance(task, RevisionTask):
            return await self._revision(task)
        if isinstance(task, AnswerTask):
            answer = await self.judge.answer(task.question, task.pr_context)
            return AnswerResult(answer=answer)
        if isinstance(task, ArchiveTask):
            repo_path = self._repo_path(task.repo)
            for branch in task.branches:
                await cleanup_workspace(workspace_path(repo_path, branch))
            return ArchiveResult()
        raise TypeError(f"unsupported execution task: {type(task).__name__}")

    async def _initial(self, task: InitialTask) -> InitialResult:
        repo_path = self._repo_path(task.repo)
        workspace = await make_workspace(repo_path, task.branch)
        prompt = build_initial_prompt(
            task.issue_title,
            task.issue_body,
            self._repo_conventions(workspace),
            agent_guidance(repo_path, task.branch),
        )
        run = await self.code_agent.start(prompt, workspace)
        return InitialResult(
            session_id=run.session_id,
            last_message=run.last_message,
            exit_code=run.exit_code,
        )

    async def _revision(self, task: RevisionTask) -> RevisionResult:
        repo_path = self._repo_path(task.repo)
        prompt = build_revision_prompt(
            task.instruction,
            task.feedback,
            agent_guidance(repo_path, task.branch),
        )
        schema_path = _write_output_schema()
        try:
            run = await self.code_agent.resume(
                task.session_id,
                prompt,
                output_schema=schema_path,
            )
        finally:
            schema_path.unlink(missing_ok=True)
        return RevisionResult(
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


def _write_output_schema() -> Path:
    """Write CodexResult's schema to a private temporary file for the Codex CLI."""

    with tempfile.NamedTemporaryFile(
        mode="w", prefix="codex-schema-", suffix=".json", delete=False
    ) as output:
        json.dump(CodexResult.model_json_schema(), output)
        return Path(output.name)
