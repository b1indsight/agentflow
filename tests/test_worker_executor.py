from pathlib import Path

import pytest

from app.models import AgentRun, CodexResult, TriageVerdict, Verdict
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
        return AgentRun(session_id="session-1", last_message="initial done", exit_code=0)

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


@pytest.mark.asyncio
async def test_executor_runs_agent_and_judge_tasks_without_store(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("Run tests.")
    cleaned: list[Path] = []

    async def fake_workspace(repo_path: Path, branch: str) -> Path:
        return workspace

    async def fake_cleanup(path: Path) -> None:
        cleaned.append(path)

    monkeypatch.setattr("app.worker.executor.make_workspace", fake_workspace)
    monkeypatch.setattr("app.worker.executor.cleanup_workspace", fake_cleanup)
    agent = RecordingAgent()
    executor = WorkerExecutor(tmp_path, agent, RecordingJudge())

    initial = await executor.execute(
        InitialTask(
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
        ArchiveTask(attempt_id=1, repo="owner/repo", branches=["flow/issue-8"])
    )

    assert isinstance(initial, InitialResult)
    assert isinstance(triage, TriageResult)
    assert isinstance(revision, RevisionResult)
    assert revision.output.summary == "updated"
    assert isinstance(answer, AnswerResult)
    assert answer.answer == "answer"
    assert isinstance(archived, ArchiveResult)
    assert agent.started and agent.resumed
    assert cleaned
