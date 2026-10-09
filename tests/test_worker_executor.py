from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.contracts.protocol import (
    AssignmentSecret,
    InitialTask,
    InitialTaskResult,
    PushSecret,
)
from app.domain.delivery_models import OpenedDelivery
from app.domain.models import (
    CodingExecutionResult,
    InitialCodingOutput,
    TriageVerdict,
    VcsKind,
    VcsOutcome,
    Verdict,
)
from app.worker.execution.executor import WorkerExecutor
from app.worker.state.session import WorkerSession
from tests.review_memory import judge_decision


class RecordingAgent:
    def __init__(self) -> None:
        self.started: list[tuple[str, Path]] = []
        self.resumed: list[tuple[str, str]] = []

    async def start(self, task: str, cwd: Path, *, output_schema=None, context=None) -> CodingExecutionResult:
        self.started.append((task, cwd))
        if context is not None and context.mcp_url is not None:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(context.mcp_url, headers={
                    "Authorization": f"Bearer {context.mcp_token.get_secret_value()}",
                }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": "open_pr", "arguments": {"title": "Implement", "body": "Done"},
                }})
                assert response.json()["result"]["structuredContent"]["pr_number"] == 17
        return CodingExecutionResult(
            session_id="session-1",
            last_message=InitialCodingOutput(
                summary="initial done", vcs_backend_used=VcsKind.GIT,
            ).model_dump_json(),
            exit_code=0,
        )

    async def resume(self, session_id: str, prompt: str, *, cwd: Path, output_schema=None, context=None) -> CodingExecutionResult:
        self.resumed.append((session_id, prompt))
        return CodingExecutionResult(
            session_id=session_id,
            last_message="revision done",
            exit_code=0,
        )


class RecordingJudge:
    async def triage(self, context):
        return judge_decision(
            TriageVerdict(verdict=Verdict.REVISE, confidence=0.9, instruction="Fix it"), context,
        )


class BackendlessAgent(RecordingAgent):
    async def start(self, task: str, cwd: Path, *, output_schema=None, context=None) -> CodingExecutionResult:
        return CodingExecutionResult(
            session_id="session-1",
            last_message="claimed success",
            exit_code=0,
        )


class RecordingToolCaller:
    async def call_tool(self, name, arguments, *, call_id=None, vcs_outcome):
        assert name == "flow.open_pr"
        assert call_id
        assert vcs_outcome.commit_sha == "abc"
        return OpenedDelivery(pr_number=17, url="https://github.com/owner/repo/pull/17", head_sha="abc", delivery_version=1)


def assignment_secret() -> AssignmentSecret:
    return AssignmentSecret(
        push=PushSecret(
            token=SecretStr("push-token"),
            repository="owner/repo",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )


@pytest.mark.asyncio
async def test_initial_with_delivery_channel_requires_push_capability(tmp_path: Path) -> None:
    executor = WorkerExecutor(
        tmp_path, RecordingAgent(), worker_id="worker-1", session=WorkerSession(tmp_path)
    )

    result = await executor.execute(
        InitialTask(
            flow_id=3,
            attempt_id=1,
            repo="owner/repo", repo_identity="worker-1:owner/repo",
            issue_number=8,
            issue_title="Issue",
            issue_body="Body",
            branch="flow/3",
        ), tool_caller=RecordingToolCaller()
    )

    assert isinstance(result, InitialTaskResult)
    assert result.failure_stage == "capability"
    assert result.failure_detail == "push capability is missing"


@pytest.mark.asyncio
async def test_executor_runs_agent_without_store(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("Run tests.")

    async def fake_workspace(repo_path: Path, flow_id: int) -> Path:
        assert flow_id == 3
        return workspace

    async def fake_context(repo_path: Path, branch: str):
        return object()

    async def fake_verify(context, backend):
        assert backend is VcsKind.GIT
        return VcsOutcome(
            backend=VcsKind.GIT,
            branch="flow/issue-8",
            commit_sha="abc",
            remote_sha="abc",
        )

    monkeypatch.setattr("app.worker.execution.executor.prepare_flow_repo", fake_workspace)
    monkeypatch.setattr("app.worker.execution.executor.prepare_vcs_context", fake_context)
    monkeypatch.setattr("app.worker.execution.executor.verify_vcs_outcome", fake_verify)
    monkeypatch.setattr(
        "app.worker.transport.mcp.verify_pushed_outcome",
        lambda context: fake_verify(context, VcsKind.GIT),
    )
    agent = RecordingAgent()
    executor = WorkerExecutor(
        tmp_path, agent, worker_id="worker-1", session=WorkerSession(tmp_path)
    )

    initial = await executor.execute(
        InitialTask(
            flow_id=3,
            attempt_id=1,
            repo="owner/repo", repo_identity="worker-1:owner/repo",
            issue_number=8,
            issue_title="Issue",
            issue_body="Body",
            branch="flow/issue-8",
        ),
        assignment_secret(), tool_caller=RecordingToolCaller(),
    )
    assert isinstance(initial, InitialTaskResult)
    assert initial.commit_sha == "abc"
    assert initial.open_pr_call_id
    assert initial.delivery_receipt is not None
    assert agent.started
    assert agent.started[0][1] == workspace


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

    monkeypatch.setattr("app.worker.execution.executor.prepare_flow_repo", fake_workspace)
    monkeypatch.setattr("app.worker.execution.executor.prepare_vcs_context", fake_context)
    executor = WorkerExecutor(tmp_path, BackendlessAgent(), worker_id="worker-1", session=WorkerSession(tmp_path))

    result = await executor.execute(
        InitialTask(
            flow_id=3,
            attempt_id=1,
            repo="owner/repo", repo_identity="worker-1:owner/repo",
            issue_number=8,
            issue_title="Issue",
            issue_body="Body",
            branch="flow/issue-8",
        ), assignment_secret(), tool_caller=RecordingToolCaller()
    )

    assert isinstance(result, InitialTaskResult)
    assert result.exit_code == 65
    assert result.failure_stage == "result"
    assert result.failure_detail == "missing VCS backend"
    assert "missing VCS backend" in result.last_message
