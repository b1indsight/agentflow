"""Ordinary Controller replacement preserves the Worker executor and journal."""

import asyncio

import pytest

from app.controler.workers.transport import serve_worker
from app.domain.models import JobState
from app.persistence.settings import SettingsStore, TextSetting
from tests.test_communication_session import Socket
from tests.test_remote_e2e import remote_flow as remote_flow  # noqa: PLC0414
from tests.test_remote_e2e import skill_repository as skill_repository  # noqa: PLC0414
from tests.test_workspace_dispatch import wait_until
from tests.worker_fixtures import run_worker


class DuplexSocket(Socket):
    def __init__(self):
        super().__init__()
        self.headers = {"x-flowtool-worker-id": "worker-1", "authorization": "Bearer worker-secret"}

    async def accept(self):
        pass

    async def close(self, code=1000):
        await super().close(code)
        await self.incoming.put(ConnectionError(f"worker closed: {code}"))
        await self.sent.put(ConnectionError(f"controller closed: {code}"))

    async def send(self, raw):
        await self.incoming.put(raw)

    async def recv(self):
        message = await self.sent.get()
        if isinstance(message, Exception):
            raise message
        return message


async def test_running_worker_reconnects_to_new_controller_and_finishes_once(
    store, remote_flow, server_runtime, skill_repository, monkeypatch,
):
    # Normal disconnect must not cancel the executor. Tools and terminal journal
    # replay on the new connection without a deployment pause or second execution.
    flow, original, github, client, agent = remote_flow
    SettingsStore(store.database).set_value(TextSetting.SKILL_REPOSITORY, "owner/skills", expected=None)
    job, = store.jobs.list_jobs(flow.id)
    started, proceed = asyncio.Event(), asyncio.Event()
    issuing, issue = asyncio.Event(), asyncio.Event()
    configure = client.workspaces.configure_git_identity
    read_token = github.repository_read_token

    async def paused(*args, **kwargs):
        await configure(*args, **kwargs)
        started.set()
        await proceed.wait()  # Disconnect during workspace preparation, before skill installation.

    async def delayed_token(repository):
        issuing.set()
        await issue.wait()
        return await read_token(repository)

    monkeypatch.setattr(client.workspaces, "configure_git_identity", paused)
    tasks = []
    try:
        old = DuplexSocket()
        server = asyncio.create_task(
            serve_worker(old, worker_tokens=original.worker_tokens, serve=original.serve_worker_session)
        )
        worker = asyncio.create_task(run_worker(client, old))
        tasks.extend([server, worker])
        await asyncio.wait_for(started.wait(), 5)
        owner = client.executions[flow.id]
        session = owner.state
        await old.close(code=1001)
        with pytest.raises(ConnectionError, match="1001"):
            await asyncio.wait_for(worker, 2)
        await asyncio.wait_for(server, 2)
        assert owner.lease() is not None
        assert github.opened == []
        assert store.jobs.get_job(job.id).state is JobState.RUNNING
        assert store.jobs.get_job(job.id).result_received_at is None

        replacement = server_runtime(github=github)
        replacement.workers.recover_abandoned_assignments()
        monkeypatch.setattr(github, "repository_read_token", delayed_token)
        new = DuplexSocket()
        tasks.extend(
            [
                asyncio.create_task(
                    serve_worker(new, worker_tokens=replacement.worker_tokens, serve=replacement.serve_worker_session)
                ),
                asyncio.create_task(run_worker(client, new)),
            ]
        )
        await asyncio.wait_for(issuing.wait(), 5)
        await wait_until(lambda: client._connected)
        proceed.set()
        await wait_until(lambda: bool(agent.started))
        (_, workspace), = agent.started
        installed = workspace / ".agents/skills/style/SKILL.md"
        assert installed.read_bytes() == (skill_repository / "style/SKILL.md").read_bytes()
        issue.set()
        await wait_until(lambda: session.load().acknowledged)
        assert len(agent.started) == 1
        assert owner.lease() is None
        assert store.jobs.get_job(job.id).state is JobState.DONE
        assert github.opened == ["feat/implement-task"]
        receipt = session.load().terminal.result.delivery_receipt
        assert receipt.status == "prepared"
        assert receipt.head_sha == store.deliveries.get_delivery(flow.id).head_sha
        assert store.flows.get_flow_execution(flow.id).released_at is None
    finally:
        proceed.set()
        issue.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(execution.shutdown() for execution in client.executions.values()))
