from pathlib import Path

import pytest

from app.models import AgentRun, CodexResult, TriageVerdict, VcsKind, VcsOutcome, Verdict
from app.protocol import (
    AnswerResult,
    AnswerTask,
    ArchiveResult,
    ArchiveTask,
    InitialResult,
    InitialTask,
    RevisionResult,
    RevisionTask,
    TriageResult,
    TriageTask,
)
from app.worker.executor import WorkerExecutor


class RecordingAgent:
    def __init__(self) -> None:
        self.started: list[tuple[str, Path]] = []
        self.resumed: list[tuple[str, str]] = []

    async def start(self, task: str, cwd: Path, *, output_schema=None) -> AgentRun:
        self.started.append((task, cwd))
        return AgentRun(
            session_id="session-1",
            last_message="initial done",
            exit_code=0,
            output=CodexResult(summary="initial done", vcs_backend_used=VcsKind.GIT),
        )

    async def resume(self, session_id: str, prompt: str, *, output_schema=None) -> AgentRun:
        self.resumed.append((session_id, prompt))
        return AgentRun(
            session_id=session_id,
            last_message="revision done",
            exit_code=0,
            output=CodexResult(summary="updated"),
        )


class RecordingJudge:
    async def triage(self, discussion: str, pr_context: str) -> TriageVerdict:
        return TriageVerdict(verdict=Verdict.REVISE, confidence=0.9, instruction="Fix it")

    async def answer(self, question: str, pr_context: str) -> str:
        return "answer"


class BackendlessAgent(RecordingAgent):
    async def start(self, task: str, cwd: Path, *, output_schema=None) -> AgentRun:
        return AgentRun(
            session_id="session-1",
            last_message="claimed success",
            exit_code=0,
            output=CodexResult(summary="claimed success"),
        )


@pytest.mark.asyncio
async def test_executor_runs_agent_and_judge_tasks_without_store(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("Run tests.")
    cleaned: list[Path] = []

    async def fake_workspace(repo_path: Path, flow_id: int) -> Path:
        assert flow_id == 3
        return workspace

    async def fake_context(repo_path: Path, branch: str):
        return object()

    async def fake_verify(context, backend):
        assert backend is VcsKind.GIT
        return VcsOutcome(
            declared_backend=VcsKind.GIT,
            verified_backend=VcsKind.GIT,
            branch="flow/issue-8",
            commit_sha="abc",
            remote_sha="abc",
        )

    async def fake_cleanup(repo_path: Path, flow_id: int) -> None:
        cleaned.append(repo_path / str(flow_id))

    monkeypatch.setattr("app.worker.executor.prepare_flow_repo", fake_workspace)
    monkeypatch.setattr("app.worker.executor.prepare_vcs_context", fake_context)
    monkeypatch.setattr("app.worker.executor.verify_vcs_outcome", fake_verify)
    monkeypatch.setattr("app.worker.executor.cleanup_flow_repo", fake_cleanup)
    agent = RecordingAgent()
    executor = WorkerExecutor(tmp_path, agent, RecordingJudge())

    initial = await executor.execute(
        InitialTask(
            flow_id=3,
            attempt_id=1,
            repo="owner/repo",
            issue_number=8,
            issue_title="Issue",
            issue_body="Body",
            branch="flow/issue-8",
        )
    )
    triage = await executor.execute(
        TriageTask(attempt_id=1, discussion="please revise", pr_context="PR #1")
    )
    revision = await executor.execute(
        RevisionTask(
            flow_id=3,
            attempt_id=1,
            repo="owner/repo",
            branch="flow/issue-8",
            session_id="session-1",
            instruction="Fix it",
            feedback=[],
        )
    )
    answer = await executor.execute(
        AnswerTask(attempt_id=1, question="Why?", pr_context="PR #1")
    )
    archived = await executor.execute(
        ArchiveTask(flow_id=3, attempt_id=1, repo="owner/repo")
    )

    assert isinstance(initial, InitialResult)
    assert isinstance(triage, TriageResult)
    assert isinstance(revision, RevisionResult)
    assert revision.output.summary == "updated"
    assert isinstance(answer, AnswerResult)
    assert answer.answer == "answer"
    assert isinstance(archived, ArchiveResult)
    assert agent.started and agent.resumed
    assert agent.started[0][1] == workspace
    assert cleaned


@pytest.mark.asyncio
async def test_initial_exit_zero_without_a_declared_backend_is_a_failure(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "flow-repo"
    workspace.mkdir()

    async def fake_workspace(repo_path: Path, flow_id: int) -> Path:
        return workspace

    async def fake_context(repo_path: Path, branch: str):
        return object()

    monkeypatch.setattr("app.worker.executor.prepare_flow_repo", fake_workspace)
    monkeypatch.setattr("app.worker.executor.prepare_vcs_context", fake_context)
    executor = WorkerExecutor(tmp_path, BackendlessAgent(), RecordingJudge())

    result = await executor.execute(
        InitialTask(
            flow_id=3,
            attempt_id=1,
            repo="owner/repo",
            issue_number=8,
            issue_title="Issue",
            issue_body="Body",
            branch="flow/issue-8",
        )
    )

    assert isinstance(result, InitialResult)
    assert result.exit_code == 65
    assert result.failure_stage == "result"
    assert result.failure_detail == "missing VCS backend"
    assert "missing VCS backend" in result.last_message
