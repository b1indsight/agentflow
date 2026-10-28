import shutil
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
from app.domain.delivery_models import PreparedDelivery
from app.domain.models import (
    CodingExecutionResult,
    InitialCodingOutput,
    VcsKind,
    VcsOutcome,
)
from app.worker.execution.executor import WorkerExecutor
from app.worker.state.execution import ExecutionState
from tests.test_vcs import _git, _git_fixture
from tests.test_worker_session import _assignment


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
                assert response.json()["result"]["structuredContent"]["status"] == "prepared"
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
        return PreparedDelivery(head_sha="abc", delivery_version=1)


def assignment_secret() -> AssignmentSecret:
    return AssignmentSecret(
        push=PushSecret(
            token=SecretStr("push-token"),
            repository="owner/repo",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )


@pytest.fixture
def initial_workspace(tmp_path):
    seed, _ = _git_fixture(tmp_path)
    target = tmp_path / "repos/owner/repo"
    target.parent.mkdir(parents=True)
    shutil.move(seed, target)
    (target / "AGENTS.md").write_text("Run tests.")
    _git("add", "AGENTS.md", cwd=target)
    _git("commit", "-m", "conventions", cwd=target)
    journal = ExecutionState(tmp_path)
    journal.record_received(_assignment())
    journal.record_started()
    return journal


@pytest.mark.asyncio
async def test_executor_runs_agent_without_store(
    monkeypatch, tmp_path: Path, initial_workspace,
) -> None:
    async def fake_verify(context, backend):
        assert context.push_token.get_secret_value() == "push-token"
        assert backend is VcsKind.GIT
        return VcsOutcome(
            backend=VcsKind.GIT,
            branch="flow/issue-8",
            commit_sha="abc",
            remote_sha="abc",
        )

    monkeypatch.setattr("app.worker.execution.executor.verify_vcs_outcome", fake_verify)
    monkeypatch.setattr(
        "app.worker.transport.mcp.verify_pushed_outcome",
        lambda context: fake_verify(context, VcsKind.GIT),
    )
    agent = RecordingAgent()
    executor = WorkerExecutor(
        tmp_path, agent, worker_id="worker-1", state=ExecutionState(tmp_path)
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
    workspace = executor.workspaces.path(executor.workspaces.lookup("owner/repo", 3))
    assert agent.started[0][1] == workspace


@pytest.mark.asyncio
async def test_initial_exit_zero_without_a_declared_backend_is_a_failure(
    monkeypatch, tmp_path: Path, initial_workspace,
) -> None:
    executor = WorkerExecutor(tmp_path, BackendlessAgent(), worker_id="worker-1", state=ExecutionState(tmp_path))

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


@pytest.mark.parametrize("problem", ["missing-gh", "expired", "mismatch", "clone-failure", "capacity"])
async def test_initial_preparation_failure_does_not_start_agent_or_expose_token(
    tmp_path, monkeypatch, problem,
):
    secret = assignment_secret()
    if problem == "expired":
        secret.push.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif problem == "mismatch":
        secret.push.repository = "another/repo"
    elif problem == "missing-gh":
        monkeypatch.setattr("app.worker.execution.gh.shutil.which", lambda *_args, **_kwargs: None)
    else:
        async def failed_prepare(*args, **kwargs):
            from app.worker.execution.vcs import VcsVerificationError
            raise VcsVerificationError("bootstrap", problem)

        monkeypatch.setattr("app.worker.execution.executor.RepositoryWorkspaces.acquire", failed_prepare)
    agent = RecordingAgent()
    assignment = _assignment()
    task = assignment.task.model_copy(update={"repo_identity": "worker-1:owner/repo"})
    journal = ExecutionState(tmp_path)
    journal.record_received(assignment)
    journal.record_started()
    result = await WorkerExecutor(tmp_path, agent, worker_id="worker-1", state=journal).execute(
        task, secret, tool_caller=RecordingToolCaller(),
    )
    assert result.exit_code != 0 and not agent.started
    assert "push-token" not in result.model_dump_json()


async def test_initial_bootstrap_push_mcp_and_final_verification_share_current_capability(
    tmp_path, monkeypatch,
):
    from app.worker.execution import vcs
    _, remote = _git_fixture(tmp_path)
    root = tmp_path / "new-worker"
    authenticated = []
    run = vcs._run

    async def offline(*args, **kwargs):
        if "clone" in args or "ls-remote" in args:
            assert kwargs["environment"]["FLOWTOOL_PUSH_TOKEN"] == "push-token"
            authenticated.append("clone" if "clone" in args else "verify")
            args = tuple(str(remote) if value == "https://github.com/owner/repo.git" else value
                         for value in args)
        return await run(*args, **kwargs)

    class Agent(RecordingAgent):
        async def start(self, task, cwd, *, output_schema=None, context=None):
            assert context.github.token.get_secret_value() == "push-token"
            _git("config", "user.name", "Test Bot", cwd=cwd)
            _git("config", "user.email", "bot@example.test", cwd=cwd)
            _git("checkout", "-b", "flow/issue-8", cwd=cwd)
            (cwd / "README.md").write_text("implemented\n")
            _git("add", ".", cwd=cwd)
            _git("commit", "-m", "implement", cwd=cwd)
            _git("push", "origin", "flow/issue-8", cwd=cwd)
            return await super().start(task, cwd, output_schema=output_schema, context=context)

    class Caller:
        async def call_tool(self, name, arguments, *, call_id=None, vcs_outcome):
            assert name == "flow.open_pr" and vcs_outcome.commit_sha == vcs_outcome.remote_sha
            return PreparedDelivery(head_sha=vcs_outcome.commit_sha, delivery_version=1)

    monkeypatch.setattr(vcs, "_run", offline)
    assignment = _assignment()
    task = assignment.task.model_copy(update={"repo_identity": "worker-1:owner/repo"})
    journal = ExecutionState(root)
    journal.record_received(assignment)
    journal.record_started()
    result = await WorkerExecutor(root, Agent(), worker_id="worker-1", state=journal).execute(
        task, assignment_secret(), tool_caller=Caller(),
    )
    assert result.exit_code == 0 and result.pushed and result.open_pr_call_id
    assert result.delivery_receipt.status == "prepared"
    assert authenticated == ["clone", "verify", "verify"]
