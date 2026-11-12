import asyncio
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr

from app.contracts.protocol import (
    AssignmentSecret,
    InitialTask,
    InitialTaskResult,
    PushSecret,
    StopFence,
)
from app.domain.delivery_models import PreparedDelivery
from app.domain.models import (
    CodingExecutionResult,
    InitialCodingOutput,
    SessionUnavailable,
    TaskContent,
    VcsKind,
    VcsOutcome,
)
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.execution.workspaces import RepositoryWorkspaces
from app.worker.state.execution import ExecutionState
from tests.test_vcs import _git, _git_fixture
from tests.test_worker_session import _assignment


class RecordingAgent:
    def __init__(self) -> None:
        self.started: list[tuple[str, Path]] = []
        self.resumed: list[tuple[str, str]] = []

    async def start(self, task: str, cwd: Path, *, output_schema=None, context=None) -> CodingExecutionResult:
        self.started.append((task, cwd))
        return await self.finish(context)

    async def finish(self, context) -> CodingExecutionResult:
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


async def assigned_workspace(root: Path) -> tuple[ExecutionState, RepositoryWorkspaces]:
    pool = RepositoryWorkspaces(root, "worker-1")
    target = (await pool.provision("owner/repo"))[1]
    received = _assignment(workspace=target.workspace, generation=target.generation)
    received.task = received.task.model_copy(update={
        "repo_identity": "worker-1:owner/repo", "attempt_id": 1,
    })
    journal = ExecutionState(root, received.flow_id)
    journal.record_received(received)
    journal.record_started()
    return journal, pool


@pytest.fixture
async def initial_workspace(tmp_path):
    seed, _ = _git_fixture(tmp_path)
    target = tmp_path / "repos/owner/repo"
    target.parent.mkdir(parents=True)
    shutil.move(seed, target)
    (target / "AGENTS.md").write_text("Run tests.")
    _git("add", "AGENTS.md", cwd=target)
    _git("commit", "-m", "conventions", cwd=target)
    return await assigned_workspace(tmp_path)


@pytest.mark.asyncio
async def test_initial_exit_zero_without_a_declared_backend_is_a_failure(
    monkeypatch, tmp_path: Path, initial_workspace,
) -> None:
    journal, pool = initial_workspace
    executor = WorkerExecutor(tmp_path, BackendlessAgent(), worker_id="worker-1", state=journal,
                              workspaces=pool)

    result = await executor.execute(
        InitialTask(
            flow_id=3,
            attempt_id=1,
            repo="owner/repo", repo_identity="worker-1:owner/repo",
            issue_number=8,
            task_content=TaskContent(title="Issue", body="Body"),
            branch="flow/issue-8",
        ), assignment_secret(), tool_caller=RecordingToolCaller()
    )

    assert isinstance(result, InitialTaskResult)
    assert result.exit_code == 65
    assert result.failure_stage == "result"
    assert result.failure_detail == "missing VCS backend"
    assert "missing VCS backend" in result.last_message


@pytest.mark.parametrize("detail", ["Amp: no new stdout/stderr for 1200s; execution timed out", None])
async def test_initial_preserves_execution_failure_details(tmp_path, initial_workspace, detail):
    # INITIAL previously discarded the adapter's safe diagnostic before terminal persistence.
    agent = RecordingAgent()
    agent.start = AsyncMock(return_value=CodingExecutionResult("session-1", "", 124, detail))
    agent.resume = AsyncMock(return_value=CodingExecutionResult("session-1", "", 124, detail))
    journal, pool = initial_workspace
    task = journal.assignment.task
    result = await WorkerExecutor(
        tmp_path, agent, worker_id="worker-1", state=journal, workspaces=pool,
    ).execute(task, assignment_secret(), tool_caller=RecordingToolCaller())
    assert result.exit_code == 124
    assert result.failure_stage == "execution"
    assert result.failure_detail == (
        detail or "CodingAgent exited unsuccessfully; execution may have occurred"
    )


@pytest.mark.parametrize(("problem", "repair_base"), [
    ("amend", "captured"), ("amend", "amended"), ("amend", "parent"),
    ("schema-after-delivery", "captured"), ("missing-delivery", "captured"),
    ("dirty", "captured"), ("unpushed", "captured"),
    ("verification-timeout", "captured"), ("verification-command", "captured"),
    ("execution-timeout", "captured"), ("changed-after-delivery", "captured"),
])
async def test_initial_corrects_completion_in_original_session(
    tmp_path, initial_workspace, monkeypatch, problem, repair_base,
):
    # The incident amended the captured B into its sibling B′. Only a descendant of B is valid.
    from app.worker.execution import executor as executor_module
    from app.worker.execution.vcs import VcsVerificationError

    output = InitialCodingOutput(summary="implemented", vcs_backend_used=VcsKind.GIT).model_dump_json()
    journal, pool = initial_workspace

    class Agent(RecordingAgent):
        async def start(self, task, cwd, *, output_schema=None, context=None):
            self.started.append((task, cwd))
            self.context, self.schema = context, output_schema
            self.base = (await asyncio.to_thread(
                subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=cwd, text=True,
            )).strip()
            _git("checkout", "-b", "flow/issue-8", cwd=cwd)
            (cwd / "README.md").write_text("implemented\n")
            _git("add", "README.md", cwd=cwd)
            _git("commit", *(["--amend"] if problem == "amend" else []), "-m", "implement", cwd=cwd)
            if problem not in {"amend", "unpushed", "execution-timeout"}:
                _git("push", "origin", "flow/issue-8", cwd=cwd)
            if problem in {"schema-after-delivery", "changed-after-delivery"}:
                await self.finish(context)
                return CodingExecutionResult("session-1", "invalid output push-token", 0)
            if problem == "dirty":
                (cwd / "leftover").write_text("unfinished")
            if problem == "execution-timeout":
                return CodingExecutionResult("session-1", "", 124, "agent timed out", "timeout")
            return CodingExecutionResult("session-1", output, 0)

        async def resume(self, session_id, prompt, *, cwd, output_schema=None, context=None):
            self.resumed.append((session_id, prompt))
            assert session_id == "session-1" and cwd == self.started[0][1]
            assert context is self.context and output_schema == self.schema and output_schema.is_file()
            assert self.base in prompt and "flow/issue-8" in prompt and "push-token" not in prompt
            if problem == "amend":
                if repair_base != "amended" and len(self.resumed) == 1:
                    base = self.base if repair_base == "captured" else f"{self.base}^"
                    _git("reset", "--soft", base, cwd=cwd)
                _git("commit", "--allow-empty", "-m", "repair", cwd=cwd)
            if problem == "dirty":
                (cwd / "leftover").unlink()
            if problem == "changed-after-delivery":
                # An incorrect Agent must not replace an already confirmed delivery with a new head.
                _git("commit", "--allow-empty", "-m", "changed after receipt", cwd=cwd)
            _git("push", "origin", "flow/issue-8", cwd=cwd)
            if problem in {"schema-after-delivery", "changed-after-delivery"} or repair_base != "captured":
                return CodingExecutionResult(session_id, output, 0)
            return await self.finish(context)

    deliveries = []

    class Caller:
        async def call_tool(self, name, arguments, *, call_id=None, vcs_outcome):
            deliveries.append(vcs_outcome)
            return PreparedDelivery(head_sha=vcs_outcome.commit_sha, delivery_version=1)

    verify = executor_module.verify_vcs_outcome
    checks = 0

    async def initially_unavailable(context, backend):
        nonlocal checks
        checks += 1
        if checks == 1:
            detail = ("git command timed out after 30s" if problem == "verification-timeout"
                      else "git command failed with exit 128")
            raise VcsVerificationError("push", detail)
        return await verify(context, backend)

    if problem in {"verification-timeout", "verification-command"}:
        monkeypatch.setattr(executor_module, "verify_vcs_outcome", initially_unavailable)
    agent = Agent()
    result = await WorkerExecutor(tmp_path, agent, worker_id="worker-1", state=journal,
                                  workspaces=pool).execute(
        journal.assignment.task, assignment_secret(), tool_caller=Caller(),
    )
    assert len(agent.started) == 1
    assert not agent.schema.exists()
    if repair_base == "captured" and problem != "changed-after-delivery":
        assert result.exit_code == 0 and result.pushed and result.open_pr_call_id
        assert len(agent.resumed) == 1 and len(deliveries) == 1
        assert result.commit_sha == result.delivery_receipt.head_sha == deliveries[0].remote_sha
        repo = agent.started[0][1]
        assert (repo / "README.md").read_text() == "implemented\n"
        _git("merge-base", "--is-ancestor", agent.base, result.commit_sha, cwd=repo)
    else:
        assert result.exit_code == 65
        assert result.failure_stage == ("delivery" if problem == "changed-after-delivery" else "commit")
        assert len(agent.resumed) == 2
        assert len(deliveries) == (problem == "changed-after-delivery")
        assert "2/2" in result.last_message


@pytest.mark.parametrize("boundary", ["no-session", "session-changed", "session-rejected",
                                      "assignment-lost", "lost-during-verification",
                                      "capability-expired", "cancelled"])
async def test_initial_correction_does_not_cross_execution_authority(
    tmp_path, initial_workspace, monkeypatch, boundary,
):
    journal, pool = initial_workspace
    secret = assignment_secret()
    clock = Mock(wraps=datetime)
    monkeypatch.setattr("app.worker.execution.gh.datetime", clock)
    if boundary == "lost-during-verification":
        async def lost_assignment(context, backend):
            from app.worker.execution.vcs import VcsVerificationError
            journal.assignment = None
            raise VcsVerificationError("push", "verification failed after assignment loss")

        monkeypatch.setattr("app.worker.execution.executor.verify_vcs_outcome", lost_assignment)

    class Agent(BackendlessAgent):
        async def start(self, task, cwd, **kwargs):
            self.started.append((task, cwd))
            result = await super().start(task, cwd, **kwargs)
            if boundary == "no-session":
                result.session_id = None
            elif boundary == "lost-during-verification":
                result.last_message = InitialCodingOutput(
                    summary="implemented", vcs_backend_used=VcsKind.GIT,
                ).model_dump_json()
            elif boundary == "assignment-lost":
                journal.assignment = None
            elif boundary == "capability-expired":
                # The adapter call may consume the remaining lifetime of the frozen capability.
                clock.now.return_value = secret.push.expires_at + timedelta(seconds=1)
            return result

        async def resume(self, session_id, prompt, **kwargs):
            self.resumed.append((session_id, prompt))
            if boundary == "session-rejected":
                raise SessionUnavailable("private adapter diagnostic")
            if boundary == "cancelled":
                raise asyncio.CancelledError
            return CodingExecutionResult("another-session", "invalid output", 0)

    agent = Agent()
    execution = WorkerExecutor(tmp_path, agent, worker_id="worker-1", state=journal,
                               workspaces=pool).execute(
        journal.assignment.task, secret, tool_caller=RecordingToolCaller(),
    )
    if boundary == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await execution
    else:
        result = await execution
        assert result.exit_code != 0
        assert "push-token" not in result.model_dump_json()
    assert len(agent.started) == 1
    assert len(agent.resumed) == (boundary in {"session-changed", "session-rejected", "cancelled"})


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

        monkeypatch.setattr("app.worker.execution.executor.RepositoryWorkspaces.claim", failed_prepare)
    agent = RecordingAgent()
    journal, pool = await assigned_workspace(tmp_path)
    result = await WorkerExecutor(tmp_path, agent, worker_id="worker-1", state=journal,
                                   workspaces=pool).execute(
        journal.assignment.task, secret, tool_caller=RecordingToolCaller(),
    )
    assert result.exit_code != 0 and not agent.started
    assert "push-token" not in result.model_dump_json()


@pytest.mark.parametrize("issue_number", [8, None])
async def test_initial_bootstrap_push_mcp_and_final_verification_share_current_capability(
    tmp_path, monkeypatch, issue_number,
):
    from app.worker.execution import vcs
    seed, remote = _git_fixture(tmp_path)
    base_sha = (await asyncio.to_thread(
        subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=seed, text=True,
    )).strip()
    root = tmp_path / "new-worker"
    authenticated = []
    deliveries: list[VcsOutcome] = []
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
            assert name == "flow.open_pr" and call_id
            deliveries.append(vcs_outcome)
            return PreparedDelivery(head_sha=vcs_outcome.commit_sha, delivery_version=1)

    monkeypatch.setattr(vcs, "_run", offline)
    journal, pool = await assigned_workspace(root)
    journal.assignment.task = journal.assignment.task.model_copy(update={
        "issue_number": issue_number,
        "task_content": TaskContent(
            title="Implement supplied task", body="Direct requirements.\nPreserve all input.",
        ),
    })
    agent = Agent()
    result = await WorkerExecutor(root, agent, worker_id="worker-1", state=journal,
                                  workspaces=pool).execute(
        journal.assignment.task, assignment_secret(), tool_caller=Caller(),
    )
    assert isinstance(result, InitialTaskResult)
    assert len(agent.started) == 1 and not agent.resumed
    assert agent.started[0][1] == pool.path(pool.lookup("owner/repo", journal.flow_id))
    assert agent.started[0][0].startswith(
        "# Task\n## Implement supplied task\n\nDirect requirements.\nPreserve all input.\n",
    )
    assert "from GitHub issue" not in agent.started[0][0]
    assert result.exit_code == 0 and result.pushed and result.open_pr_call_id
    assert result.vcs_backend is VcsKind.GIT and result.branch == "flow/issue-8"
    assert result.delivery_receipt is not None and len(deliveries) == 1
    assert deliveries[0].backend is VcsKind.GIT and deliveries[0].branch == result.branch
    assert (result.commit_sha == result.remote_sha == result.delivery_receipt.head_sha
            == deliveries[0].commit_sha == deliveries[0].remote_sha)
    assert result.commit_sha != base_sha
    assert result.delivery_receipt.status == "prepared"
    assert authenticated == ["clone", "verify", "verify"]


async def test_stop_preserves_edits_until_next_initial_prepares_new_generation(
    tmp_path, initial_workspace,
):
    # A completed agent may leave edits; stop records quiescence without cleaning the repository.
    journal, pool = initial_workspace

    class Agent(RecordingAgent):
        async def start(self, task, cwd, **kwargs):
            (cwd / "README.md").write_text("stopped tracked edit")
            (cwd / "untracked").write_text("stopped untracked edit")
            return CodingExecutionResult("session", "stopped", 1)

    executor = WorkerExecutor(tmp_path, Agent(), worker_id="worker-1", state=journal,
                              workspaces=pool)
    received = journal.assignment
    result = await executor.execute(received.task, assignment_secret(), tool_caller=RecordingToolCaller())
    journal.complete(result)
    binding = journal.load().workspace
    repo = pool.path(binding)
    before = pool.manifest_path.read_bytes()
    owner = WorkerExecution(worker_id="worker-1", executor=executor, state=journal)
    fence = StopFence(request_id="stop", flow_id=received.flow_id, job_id=received.job_id,
                     assignment_token=received.assignment_token)
    receipt = await owner.stop(fence)
    assert journal.load().stopped == receipt
    owner.on_stop_accepted(fence)
    journal.reconcile_acknowledgement()
    assert pool.manifest_path.read_bytes() == before
    assert pool.lookup("owner/repo", received.flow_id) == binding
    assert (repo / "README.md").read_text() == "stopped tracked edit"
    assert (repo / "untracked").read_text() == "stopped untracked edit"

    newer = await pool.claim("owner/repo", received.flow_id, binding.slot_id, binding.generation + 1,
                             base_branch="main", remote="origin", push_token=None)
    assert newer.generation == binding.generation + 1 and pool.path(newer) == repo
    assert (repo / "README.md").read_text() == "base\n"
    assert not (repo / "untracked").exists()
