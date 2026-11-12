"""Duplex worker/server runtime coverage with PostgreSQL and a scripted external agent."""

import asyncio
import subprocess
from collections.abc import Mapping
from contextlib import asynccontextmanager

import pytest
from pydantic import SecretStr

from app.communication.gateway import Gateway
from app.contracts.receipts import decode_result_inbox
from app.controler.workers.transport import serve_worker
from app.controler.workflow.service import ControlService
from app.domain.models import DeliveryState, FlowState, JobState
from app.github import RemoteRef, Repository
from app.persistence.records import required
from app.worker.config import WorkerSettings
from app.worker.runtime import WorkerRuntime
from tests.github import RecordingGitHub, RecordingRepository
from tests.test_coordinator import coordinator
from tests.test_worker_executor import RecordingAgent
from tests.test_worker_workspaces import managed_seed
from tests.test_workspace_dispatch import wait_until
from tests.worker_fakes import run_worker, server_runtime


class QueueSocket:
    def __init__(self, worker_to_serve: asyncio.Queue[str], serve_to_worker: asyncio.Queue[str]):
        self._incoming = worker_to_serve
        self._outgoing = serve_to_worker
        self._headers = {
            "x-flowtool-worker-id": "worker-1",
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
def remote_flow(store, tmp_path, monkeypatch, recording_resources):
    """Use the normal coordinator/executor; only GitHub and the coding agent are scripted."""
    from tests.test_vcs import _git

    flow = required(ControlService(database=store.database).on_trigger("owner/repo", 8, 42))
    github = RecordingGitHub()
    subject = coordinator(store, github)
    root, remote = managed_seed(tmp_path)
    seed = root / "repos/owner/repo"
    _git("branch", "-m", "trunk", cwd=seed)
    _git("push", "-u", "origin", "trunk", cwd=seed)

    def remote_sha(branch):
        return subprocess.check_output(
            ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{branch}"], text=True,
        ).strip()

    async def fetch_ref(repo, branch, *, retry_transient=False):
        return RemoteRef(branch=branch, sha=await asyncio.to_thread(remote_sha, branch))

    async def open_pull_request(repo, head, base, title, body):
        pull = await RecordingRepository.open_pull_request(repo, head, base, title, body)
        github.pull_request = pull.model_copy(update={"head_sha": remote_sha(head)})
        return github.pull_request

    monkeypatch.setattr(Repository, "fetch_ref", fetch_ref)
    monkeypatch.setattr(Repository, "open_pull_request", open_pull_request)

    class Agent(RecordingAgent):
        async def start(self, task, cwd, *, output_schema=None, context=None):
            self.started.append((task, cwd))
            _git("checkout", "-b", flow.initial_branch, cwd=cwd)
            (cwd / "README.md").write_text("implemented\n")
            _git("add", "README.md", cwd=cwd)
            _git("commit", "-m", "implement", cwd=cwd)
            _git("push", "origin", flow.initial_branch, cwd=cwd)
            return await self.finish(context)

    agent = Agent()
    monkeypatch.setattr("app.worker.runtime.CodexAgent", lambda **kwargs: agent)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    worker = WorkerRuntime(WorkerSettings(
        worker_id="worker-1", worker_token=SecretStr("worker-secret"),
        serve_ws_url="wss://unused", worker_root=root,
    ))
    worker.root_lock.acquire()
    worker.initialize()
    try:
        yield flow, subject, github, worker, agent
    finally:
        worker.root_lock.release()


@asynccontextmanager
async def remote_connection(subject, worker, *, peers=None):
    """Keep the existing duplex wire while sharing runtime setup with HTTP history tests."""
    handler = server_runtime(
        subject, database=subject.database, peers=peers if peers is not None else Gateway(),
        poll_interval=0.01, tools=subject.tools, results=subject.results,
    )
    to_serve, to_worker = asyncio.Queue(), asyncio.Queue()
    tasks = [
        asyncio.create_task(serve_worker(
            QueueSocket(to_serve, to_worker), worker_tokens={"worker-1": "worker-secret"},
            serve=handler.serve_worker_session,
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


async def test_control_gateway_and_worker_client_complete_one_remote_job(store, remote_flow):
    flow, subject, github, worker, agent = remote_flow
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
    assert github.opened == [flow.initial_branch] and len(agent.started) == 1
