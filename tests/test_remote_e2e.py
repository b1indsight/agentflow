"""Duplex worker/server runtime coverage with PostgreSQL and a scripted external agent."""

import asyncio
import subprocess
from collections.abc import Mapping
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.contracts.protocol import SkillRepositorySource
from app.contracts.receipts import decode_result_inbox
from app.controler.api import create_app
from app.controler.workers.transport import serve_worker
from app.controler.workflow.service import ControlService
from app.domain.models import DeliveryState, FlowState, JobState
from app.github import GitHubError, GitHubErrorKind, RemoteRef
from app.github import pull_requests as pr_api
from app.github import repositories as repo_api
from app.persistence.records import required
from app.persistence.settings import SettingsStore, TextSetting
from app.worker.config import WorkerSettings
from app.worker.execution import skills
from app.worker.execution.vcs import VcsVerificationError, _required_stdout
from app.worker.runtime import WorkerRuntime
from tests.access import AccessIssuer
from tests.github import RecordingGitHub, record_open_pull_request
from tests.test_vcs import _git, _git_fixture
from tests.test_worker_executor import RecordingAgent
from tests.test_worker_workspaces import managed_seed
from tests.test_workspace_dispatch import wait_until
from tests.worker_fixtures import NamingInference, run_worker


class QueueSocket:
    def __init__(self, worker_to_serve: asyncio.Queue[str], serve_to_worker: asyncio.Queue[str],
                 worker_id: str = "worker-1"):
        self._incoming = worker_to_serve
        self._outgoing = serve_to_worker
        self._headers = {
            "x-flowtool-worker-id": worker_id,
            "authorization": "Bearer worker-secret",
        }

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers

    async def accept(self) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        pass

    async def send_text(self, data: str) -> None:
        await self._outgoing.put(data)

    async def receive_text(self) -> str:
        return await self._incoming.get()


class QueueConnection:
    def __init__(self, worker_to_serve: asyncio.Queue[str], serve_to_worker: asyncio.Queue[str]):
        self._outgoing = worker_to_serve
        self._incoming = serve_to_worker

    async def send(self, data: str) -> None:
        await self._outgoing.put(data)

    async def recv(self) -> str:
        return await self._incoming.get()

    async def close(self, code: int = 1000) -> None:
        pass


@pytest.fixture
def remote_flow(store, tmp_path, monkeypatch, recording_resources, server_runtime, request):
    """Use the normal coordinator/executor; only GitHub and the coding agent are scripted."""
    flow = required(ControlService(database=store.database).create_flow("owner/repo", 8, 42))
    github = RecordingGitHub()
    server = server_runtime(github=github)
    root, remote = managed_seed(tmp_path)
    seed = root / "repos/owner/repo"
    _git("branch", "-m", "trunk", cwd=seed)
    _git("push", "-u", "origin", "trunk", cwd=seed)

    def remote_sha(branch):
        return subprocess.check_output(
            ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{branch}"], text=True,
        ).strip()

    async def fetch_ref(installation, full_name, branch, *, retry_transient=False):
        try:
            return RemoteRef(branch=branch, sha=await asyncio.to_thread(remote_sha, branch))
        except subprocess.CalledProcessError as error:
            raise GitHubError(GitHubErrorKind.NOT_FOUND, "get_ref", status_code=404) from error

    async def open_pull_request(repo, head, base, title, body):
        pull = await record_open_pull_request(repo, head, base, title, body)
        github.pull_request = pull.model_copy(update={"head_sha": remote_sha(head)})
        return github.pull_request

    monkeypatch.setattr(repo_api, "fetch_ref", fetch_ref)
    monkeypatch.setattr(pr_api, "open_pull_request", open_pull_request)

    class Agent(RecordingAgent):
        def __init__(self):
            super().__init__()
            self.models: list[str | None] = []
            self.efforts: list[str | None] = []

        async def start(self, task, cwd, *, output_schema=None, context=None, model=None,
                        effort=None, max_output_bytes=None):
            self.models.append(model)
            self.efforts.append(effort)
            self.started.append((task, cwd))
            branch = store.deliveries.get_delivery(flow.id).branch
            _git("checkout", "-b", branch, cwd=cwd)
            (cwd / "README.md").write_text("implemented\n")
            _git("add", "README.md", cwd=cwd)
            _git("commit", "-m", "implement", cwd=cwd)
            _git("push", "origin", branch, cwd=cwd)
            return await self.finish(context)

    agent = Agent()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    worker = WorkerRuntime(WorkerSettings(
        worker_id="worker-1", worker_token=SecretStr("worker-secret"),
        serve_ws_url="wss://unused", worker_root=root,
        coding_agent=getattr(request, "param", "codex"),
    ))
    # Script coding on the shared backend instance while keeping native history reads real.
    monkeypatch.setattr(agent, "read_history", worker._agents[worker.settings.coding_agent].read_history)
    monkeypatch.setitem(worker._agents, worker.settings.coding_agent, agent)
    inference = NamingInference()
    monkeypatch.setattr(worker, "infer", inference.infer)
    monkeypatch.setattr(worker, "inference_capability", inference.inference_capability)
    worker.root_lock.acquire()
    worker.initialize()
    try:
        yield flow, server, github, worker, agent
    finally:
        worker.root_lock.release()


@asynccontextmanager
async def remote_connection(server, worker):
    """Keep the existing duplex wire while sharing runtime setup with HTTP history tests."""
    to_serve, to_worker = asyncio.Queue(), asyncio.Queue()
    worker_id = worker.settings.worker_id
    tasks = [
        asyncio.create_task(serve_worker(
            QueueSocket(to_serve, to_worker, worker_id), worker_tokens={worker_id: "worker-secret"},
            serve=server.serve_worker_session,
        )),
        asyncio.create_task(run_worker(worker, QueueConnection(to_serve, to_worker))),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(execution.shutdown() for execution in worker.executions.values()))
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                raise outcome


async def test_console_detail_keeps_the_completed_workspace_snapshot(store, remote_flow):
    # Later local edits and disconnects must not change the last completed execution's counts.
    flow, server, _, worker, agent = remote_flow
    issuer = AccessIssuer()
    await server.access.close()
    server.access = issuer.auth
    app = create_app(server)
    app.state.runtime = server
    endpoint = f"/api/flows/{flow.id}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers=issuer.headers()) as http:
        assert (await http.get(endpoint)).json()["workspace_diff"] is None
        async with remote_connection(server, worker):
            await wait_until(lambda: store.flows.get_flow(flow.id).state is FlowState.REVIEW)
            (_, repo), = agent.started
            (repo / "draft").write_text("one\ntwo\n")
        assert (await http.get(endpoint)).json()["workspace_diff"] == {"added": 0, "deleted": 0, "modified": 1}


@pytest.mark.parametrize("remote_flow", ["codex", "amp"], indirect=True)
async def test_control_gateway_and_worker_client_complete_one_remote_job(store, remote_flow, skill_repository, monkeypatch):
    settings = SettingsStore(store.database)
    settings.update(
        "codex-wire-model", "anthropic/claude-opus-5-5", codex_effort="ultra", amp_effort="low", expected_revision=0,
    )
    settings.set_value(TextSetting.SKILL_REPOSITORY, "owner/skills", expected=None)
    published = skill_repository / "style/SKILL.md"
    original = published.read_text()
    flow, subject, github, worker, agent = remote_flow
    # Search before editing the checkout: no pushed branch or PR receipt exists yet.
    store.flows.create_flow("other/repo", 8, 42)
    start = agent.start

    async def search_then_start(task, cwd, *, context, **kwargs):
        link = cwd / ".agents/skills/style"
        assert link.is_symlink() and link.resolve().is_relative_to(worker.workspaces.inference_path)
        assert (link / "SKILL.md").read_text() == original
        async with httpx.AsyncClient(trust_env=False) as client:
            reply = await client.post(context.mcp_url, headers={
                "Authorization": f"Bearer {context.mcp_token.get_secret_value()}",
            }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "search_flows", "arguments": {"q": "issue:8"},
            }})
        page = reply.json()["result"]["structuredContent"]
        assert [row["id"] for row in page["items"]] == [flow.id] and sum(page["counts"].values()) == 1
        return await start(task, cwd, context=context, **kwargs)

    monkeypatch.setattr(agent, "start", search_then_start)
    job, = store.jobs.list_jobs(flow.id)
    async with remote_connection(subject, worker):
        await wait_until(lambda: required(store.jobs.get_job(job.id)).result_applied_at is not None)
        # A server commit alone is not completion: the correlated business ACK must
        # reach the worker and retire its durable journal, independently of inventory.
        await wait_until(lambda: worker.executions[flow.id].state.load().acknowledged)
        state = worker.executions[flow.id].state
        assert state.assignment is None and state.path.is_file()
        journal = state.load()
        assert journal.terminal.result.delivery_receipt.status == "prepared"

        # A skill push refreshes future workspaces without retargeting an initialized Flow.
        (_, workspace), = agent.started
        linked = workspace / ".agents/skills/style/SKILL.md"
        published.write_text(original + "Updated\n")
        _git("commit", "-am", "refresh skill", cwd=skill_repository)
        snapshots = linked.resolve().parents[2]
        refreshed = snapshots / await _required_stdout("bootstrap", "git", "rev-parse", "HEAD", cwd=skill_repository)
        await subject.receive_webhook("push", "skill-push", {
            "repository": {"full_name": "Owner/Skills", "default_branch": "main"},
            "ref": "refs/heads/main", "sender": {"type": "Bot"},
        }, "digest")
        assert await asyncio.to_thread(subject.webhooks.sweep, asyncio.get_running_loop()) == 1
        await wait_until(refreshed.is_dir)
        assert (refreshed / "style/SKILL.md").read_text() == published.read_text()
        # A later sync can reclaim the unused intermediate version, but not the live Flow's.
        published.write_text(original + "Newest\n")
        _git("commit", "-am", "publish newest", cwd=skill_repository)
        newest = snapshots / await _required_stdout("bootstrap", "git", "rev-parse", "HEAD", cwd=skill_repository)
        subject.request_skill_sync()
        await wait_until(newest.is_dir)
        assert not refreshed.exists()
        settings.set_value(TextSetting.SKILL_REPOSITORY, None, expected="owner/skills")
        subject.request_skill_sync()
        await wait_until(lambda: worker.workspaces.skills.repository is None)
        await required(worker.workspaces.skills.pending)
        assert linked.read_text() == original
        assert (newest / "style/SKILL.md").read_text() == published.read_text()

    persisted = required(store.jobs.get_job(job.id))
    assert persisted.state is JobState.DONE and persisted.started_at is not None
    terminal = decode_result_inbox(
        persisted.result_payload, job_id=job.id, assignment_token=persisted.assignment_token,
        result_digest=persisted.result_digest,
    ).terminal
    assert terminal == journal.terminal and terminal.result.session_id == "session-1"
    assert required(store.flows.get_flow(flow.id)).state is FlowState.REVIEW
    delivery = required(store.deliveries.get_delivery(flow.id))
    assert delivery.state is DeliveryState.OPEN and delivery.pr_number == 17
    assert delivery.head_sha == terminal.result.commit_sha
    call = required(store.deliveries.get_tool_call(terminal.result.open_pr_call_id))
    assert call.response == terminal.result.delivery_receipt
    assert github.opened == ["feat/implement-task"] and len(agent.started) == 1
    assert agent.models == ["codex-wire-model" if worker.settings.coding_agent == "codex"
                            else "anthropic/claude-opus-5-5"]
    assert agent.efforts == ["ultra" if worker.settings.coding_agent == "codex" else "low"]
    named = store.flows.get_flow(flow.id)
    assert named.title == "Implement task" and delivery.branch == "feat/implement-task"
    assert len([j for j in store.jobs.list_jobs(flow.id) if j.kind.value == "inference"]) == 1


@pytest.mark.parametrize("next_action", ["continue", "dirty", "unpublished", "early_input", "succeeded", "abandoned"])
async def test_initial_yields_until_input_or_archive(store, remote_flow, monkeypatch, next_action):
    from uuid import uuid4

    from app.controler.api import create_app
    from app.persistence.activity import ActivityStore
    from app.persistence.console import ConsoleStore
    from tests.access import AccessIssuer

    flow, server, github, worker, agent = remote_flow
    deliver = agent.finish
    issuer = AccessIssuer()
    monkeypatch.setattr(server, "access", issuer.auth)
    app = create_app(server)
    app.state.runtime, app.state.metrics = server, server.metrics
    request_id = str(uuid4())

    async def submit_input():
        response = await console.post(f"/api/flows/{flow.id}/agent-tasks", headers=issuer.headers(),
                                      json={"instruction": "Verification passed", "request_id": request_id})
        response.raise_for_status()

    async def pause(context):
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(context.mcp_url, headers={
                "Authorization": f"Bearer {context.mcp_token.get_secret_value()}",
            }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "request_user_input", "arguments": {"message": "Waiting for verification"},
            }})
            assert "error" not in response.json() and not response.json()["result"].get("isError")
        if next_action == "early_input":
            # A delegated result can arrive through the input queue before the worker's terminal.
            await submit_input()
        return await deliver(None)

    async def resume(session_id, prompt, *, cwd, output_schema, context):
        assert session_id == "session-1" and "Verification passed" in prompt
        if next_action in {"dirty", "unpublished"}:
            assert (cwd / "README.md").read_text() == "unfinished work\n"
            if next_action == "dirty":
                _git("commit", "-am", "finish pending work", cwd=cwd)
            _git("push", "origin", "HEAD", cwd=cwd)
        return await deliver(context)

    monkeypatch.setattr(agent, "finish", pause)
    monkeypatch.setattr(agent, "resume", resume)
    job, = store.jobs.list_jobs(flow.id)
    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as console,
                remote_connection(server, worker)):
        await wait_until(lambda: store.jobs.get_job(job.id).result_applied_at is not None)
        assert store.jobs.get_job(job.id).state is JobState.DONE
        assert store.flows.get_flow(flow.id).state is FlowState.REVIEW and not github.opened
        assert ConsoleStore(store.database).detail(flow.id).action == "awaiting_input"
        assert any(item.body == "Waiting for verification" for item in ActivityStore(store.database).page(flow.id).items)
        if next_action not in {"succeeded", "abandoned"}:
            if next_action != "early_input":
                # A failed continuation can leave these changes in the original workspace.
                if next_action in {"dirty", "unpublished"}:
                    cwd = agent.started[0][1]
                    (cwd / "README.md").write_text("unfinished work\n")
                    if next_action == "unpublished":
                        _git("commit", "-am", "pending work", cwd=cwd)
                for _ in range(2):
                    await submit_input()
            else:
                server.service.dispatch_pending_inputs()
            await wait_until(lambda: store.deliveries.get_delivery(flow.id).pr_number is not None)
            assert len([j for j in store.jobs.list_jobs(flow.id) if j.kind.value == "initial"]) == 2
            assert len(agent.started) == 1 and len(github.opened) == 1
        else:
            response = await console.post(f"/api/flows/{flow.id}/archive", headers=issuer.headers(), json={
                "reason": next_action, "summary": "Reviewed", "expected_attempt_id": flow.current_attempt_id,
            })
            response.raise_for_status()
            await wait_until(lambda: store.flows.get_flow(flow.id).state is FlowState.ARCHIVED)
            assert not github.opened
    await issuer.auth.close()


async def test_existing_remote_branch_is_not_taken_over(store, remote_flow):
    # Even a valid model answer cannot authorize coding on an unrelated existing ref.
    flow, server, github, worker, agent = remote_flow
    seed = worker.settings.worker_root / "repos/owner/repo"
    branch = "feat/implement-task"
    _git("branch", branch, cwd=seed)
    _git("push", "origin", branch, cwd=seed)
    before = _git("ls-remote", "origin", f"refs/heads/{branch}", cwd=seed)
    coding, = store.jobs.list_jobs(flow.id)
    async with remote_connection(server, worker):
        await wait_until(lambda: store.jobs.get_job(coding.id).result_applied_at is not None)
    failed = store.jobs.get_job(coding.id)
    assert failed.state is JobState.FAILED and failed.started_at is None
    assert "already exists" in failed.last_error
    assert store.deliveries.get_delivery(flow.id) is None
    assert github.opened == agent.started == []
    assert not any(call["operation"] == "create_push_capability" for call in github.resource_calls)
    assert _git("ls-remote", "origin", f"refs/heads/{branch}", cwd=seed) == before


@pytest.fixture
def skill_repository(tmp_path, monkeypatch):
    from app.worker.execution.vcs import _vcs_environment

    source = tmp_path / "skill-source"
    source.mkdir()
    publisher, _ = _git_fixture(source)
    skill = publisher / "style"
    skill.mkdir()
    (skill / "SKILL.md").write_text('---\nname: style\ndescription: Style rules\nmetadata:\n  always_on: "true"\n---\n')
    _git("add", "style", cwd=publisher)
    _git("commit", "-m", "publish skill", cwd=publisher)
    config = source / "gitconfig"
    config.write_text(f'[url "{publisher.as_uri()}"]\n insteadOf = https://github.com/owner/skills.git\n')
    monkeypatch.setattr("app.worker.execution.skills._vcs_environment",
                        lambda: _vcs_environment() | {"GIT_CONFIG_GLOBAL": str(config)})
    monkeypatch.setattr(RecordingGitHub, "repository_read_token", AsyncMock(return_value=SecretStr("read")))
    return publisher


async def test_install_uses_local_skills_and_sync_retains_the_last_snapshot(tmp_path, skill_repository, monkeypatch):
    workspace = tmp_path / "project"
    links = workspace / ".agents/skills"
    links.mkdir(parents=True)
    cache = skills.SkillSnapshots(lambda: tmp_path, lambda: [workspace])
    source = SkillRepositorySource(repository="owner/skills", token=SecretStr("read"))
    await cache.sync(source)
    unrelated = tmp_path / "skill-repositories" / ("0" * 40)
    unrelated.mkdir()  # A SHA-shaped name alone is not permission to delete.
    original = (skill_repository / "style/SKILL.md").read_text()
    (skill_repository / "style/SKILL.md").write_text(original + "B\n")
    _git("commit", "-am", "B", cwd=skill_repository)

    # A cold worker with a warm disk can start selection while the publisher is unreachable.
    cache = skills.SkillSnapshots(lambda: tmp_path, lambda: [workspace])
    resume = asyncio.Event()
    clone = skills.clone_skills

    async def blocked(*args):
        await resume.wait()
        return await clone(*args)

    monkeypatch.setattr(skills, "clone_skills", blocked)
    refresh = cache.sync(source)
    async with asyncio.timeout(5), cache.acquire() as a:
        assert (a / "style/SKILL.md").read_text() == original
        resume.set()
        await refresh  # No symlink yet: the selector alone keeps A alive.
        assert (a / "style/SKILL.md").read_text() == original
        (links / "style").symlink_to(a / "style")

    cache = skills.SkillSnapshots(lambda: tmp_path, lambda: [workspace])
    await cache.sync(source)
    with pytest.raises(VcsVerificationError, match="read credential unavailable"):
        await cache.sync(source.model_copy(update={"token": None}))
    async with cache.acquire() as b:
        assert (b / "style/SKILL.md").read_text() == original + "B\n"
        assert (links / "style/SKILL.md").read_text() == original
    (links / "style").unlink()
    await cache.sync(None)  # No installs, no workspace links: prune A but keep B on disk.
    assert not a.exists() and b.is_dir() and unrelated.is_dir()
    async with cache.acquire() as disabled:
        assert disabled is None
    inaccessible = SkillRepositorySource(repository="other/skills", token=None)
    with pytest.raises(VcsVerificationError, match="read credential unavailable"):
        await cache.sync(inaccessible)
    with pytest.raises(VcsVerificationError, match="read credential unavailable"):
        async with cache.acquire():
            pytest.fail("an inaccessible source must not borrow the retained snapshot")
