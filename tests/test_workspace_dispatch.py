"""Real PostgreSQL, Session endpoints and local Git slots; no external coding service."""

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.communication.gateway import Gateway
from app.communication.inprocess import in_process_transport_pair
from app.communication.session import Session
from app.contracts.protocol import (
    Inventory,
    ProvisionResult,
    Register,
    ServerMessageUnion,
    WorkerMessageUnion,
    parse_worker_message,
)
from app.contracts.validation import parse_worker_input
from app.controler.workers.callbacks import ServerCallbacks
from app.controler.workers.transport import control_ack
from app.controler.workflow.service import ControlService
from app.domain.models import ArchiveReason, JobState
from app.persistence.records import required
from app.worker.config import WorkerSettings
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.runtime import WorkerRuntime
from app.worker.state.execution import ExecutionState
from tests.github import RecordingGitHub
from tests.test_coordinator import coordinator
from tests.test_worker_workspaces import managed_seed
from tests.worker_fixtures import NamingInference


class ControlledWorker(WorkerRuntime):
    def __init__(self, root: Path, limit: int) -> None:
        super().__init__(WorkerSettings(
            worker_id="worker-a", worker_token=SecretStr("test"), serve_ws_url="wss://unused",
            worker_root=root, worker_repo_workspace_limit=limit,
        ))
        self.running: dict[int, Path] = {}
        self.finish: dict[int, asyncio.Event] = {}
        self.inference = NamingInference()  # type: ignore[assignment]

    def _create_execution(self, flow_id: int) -> WorkerExecution:
        state = ExecutionState(self.settings.worker_root, flow_id)
        worker = self

        class Executor:
            async def execute(self, task, secret=None, *, tool_caller):
                assignment = state.assignment
                assert assignment is not None
                binding = await worker.workspaces.claim(
                    task.repo, flow_id, assignment.workspace, assignment.generation,
                    base_branch="main", remote="origin", push_token=None,
                )
                state.bind_workspace(binding)
                path = worker.workspaces.path(binding)
                worker.running[assignment.job_id] = path
                finish = worker.finish.setdefault(assignment.job_id, asyncio.Event())
                try:
                    await finish.wait()
                finally:
                    worker.running.pop(assignment.job_id)
                raise RuntimeError("controlled coding outcome")

        return WorkerExecution(worker_id="worker-a", executor=Executor(), state=state,
                               on_change=self.notify)


async def wait_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(10):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("limit", [2, 3, 17])
async def test_demand_concurrent_dispatch_stop_and_result_isolate_workspaces(
    store, tmp_path, limit, recording_resources,
):
    root, _ = managed_seed(tmp_path)
    shutil.copytree(root / "repos/owner/repo", root / "repos/owner/other")
    control = ControlService(database=store.database)
    flows = [required(control.create_flow(repo, issue, 42)) for repo in ("owner/repo", "owner/other")
             for issue in range(1, limit + 2)]
    jobs = [job for flow in flows for job in store.jobs.list_jobs(flow.id)]
    worker = ControlledWorker(root, limit)
    worker.root_lock.acquire()
    worker.initialize()
    assert len(worker.workspaces.inventory()) == 1  # Only the real inference directory initially.
    assert worker.workspaces.inference_path.is_dir()
    peers = Gateway[WorkerMessageUnion, ServerMessageUnion]()
    host_transport, worker_transport = in_process_transport_pair()
    github = RecordingGitHub()
    github.refs = {}
    subject = coordinator(store, github)
    callbacks = None

    async def host():
        nonlocal callbacks
        register = parse_worker_message(await host_transport.receive_text())
        assert isinstance(register, Register)
        session = Session(host_transport, parse=parse_worker_input, make_ack=control_ack, epoch=uuid4().hex)
        session.register_control(register)
        session.start()
        callbacks = ServerCallbacks(
            subject, worker_id="worker-a", register=register, session=session,
            database=store.database, tools=subject.tools, results=subject.results, notify_jobs=peers.wake_all,
            heartbeat_interval=0.05, heartbeat_timeout=10, poll_interval=0.02,
        )
        try:
            await peers.serve("worker-a", session, callbacks, replace=True)
        finally:
            await callbacks.aclose()

    host_task = asyncio.create_task(host())
    worker_task = asyncio.create_task(worker._serve_connection(worker_transport))
    try:
        # Naming shares one inference lane; require progress, not a fixed total startup time.
        for expected in range(1, 2 * limit + 1):
            await wait_until(lambda expected=expected: len(worker.running) >= expected)
        assert not host_task.done() and not worker_task.done()
        assert len(set(worker.running.values())) == 2 * limit
        assert len(worker.workspaces.inventory()) == 1 + 2 * limit
        assert sum(required(store.jobs.get_job(j.id)).state is JobState.QUEUED for j in jobs) == 2
        initial = set(worker.running)
        before = {flow: execution.state.path.read_bytes() for flow, execution in worker.executions.items()}
        await host_transport.close()
        await asyncio.gather(host_task, worker_task, return_exceptions=True)
        assert set(worker.running) == initial  # Connection cleanup does not own agents.
        host_transport, worker_transport = in_process_transport_pair()
        host_task = asyncio.create_task(host())
        worker_task = asyncio.create_task(worker._serve_connection(worker_transport))
        await wait_until(lambda: worker._connected)
        assert callbacks is not None and set(callbacks.current) == initial
        assert all(worker.executions[flow].state.path.read_bytes() == raw for flow, raw in before.items())
        for execution in worker.executions.values():
            assert worker.receive(execution.state.assignment) is execution
        finished, stopped, *siblings = sorted(initial)
        worker.finish[finished].set()
        await wait_until(lambda: all(w.current_job_id != finished for w in store.workers.list_workspaces("worker-a")))
        finished_job = required(store.jobs.get_job(finished))
        binding = required(store.workers.get_flow_execution(finished_job.required_flow_id))
        assert binding.released_at is None  # Completion retains review affinity.
        assert set(worker.running) == initial - {finished}

        # Stopping one Flow must not stop its siblings; writer exit permits higher-generation reuse.
        stopped_job = required(store.jobs.get_job(stopped))
        flow = required(store.flows.get_flow(stopped_job.required_flow_id))
        # Completed naming is retained when a running coding task is stopped.
        control.finish(flow, reason=ArchiveReason.ABANDONED, summary="",
                       expected_round_no=flow.current_round_no)
        title_job = required(store.jobs.get_job(required(store.flows.get_flow(flow.id)).title_job_id))
        assert title_job.state is JobState.DONE
        peers.wake_all()
        await wait_until(lambda: bool(set(worker.running) - initial))
        assert set(siblings) <= set(worker.running)
        replacement = required(store.jobs.get_job(next(iter(set(worker.running) - initial))))
        assert replacement.workspace == stopped_job.workspace
        assert replacement.generation == stopped_job.generation + 1
        assert worker.executions[finished_job.required_flow_id].state.load().acknowledged
        assert callbacks is not None and len(callbacks.current) == 2 * limit - 1
    finally:
        await host_transport.close()
        outcomes = await asyncio.gather(host_task, worker_task, return_exceptions=True)
        await asyncio.gather(*(execution.shutdown() for execution in worker.executions.values()))
        worker.root_lock.release()
        # Include either endpoint's exception with a failed scenario rather than losing background evidence.
        for outcome in outcomes:
            if isinstance(outcome, Exception) and not isinstance(outcome, ConnectionError):
                raise outcome


@pytest.mark.parametrize("failure", ["invalid_repository", "workspace_conflict", "filesystem_error"])
async def test_provision_failure_does_not_disconnect_other_workspaces(
    store, tmp_path, monkeypatch, failure, recording_resources,
):
    # Capacity requests share the job connection. One bad repository must not interrupt
    # healthy jobs, and a partially created pool must still publish its actual inventory.
    root, _ = managed_seed(tmp_path)
    bad_repo = "owner/bad--flow-name" if failure == "invalid_repository" else "owner/bad"
    control = ControlService(database=store.database)
    bad_flow = required(control.create_flow(bad_repo, 1, 42))
    good_flow = required(control.create_flow("owner/repo", 2, 42))
    bad_job, = store.jobs.list_jobs(bad_flow.id)
    good_job, = store.jobs.list_jobs(good_flow.id)
    if failure == "filesystem_error":
        mkdir = Path.mkdir
        attempts = 0

        def fail_second_directory(path, *args, **kwargs):
            nonlocal attempts
            if path.parent == root / "workspaces" / bad_repo and len(path.name) == 32:
                attempts += 1
                if attempts == 2:
                    raise OSError("private local path must not reach the controller")
            return mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_second_directory)
        # Keep this Flow queued so only inventory, not an executor, exercises the partial pool.
        with store.database.connection() as conn:
            conn.execute("UPDATE jobs SET prepare_next_at=now()+interval '1 hour' WHERE id=%s", (bad_job.id,))

    worker = ControlledWorker(root, 3)
    worker.root_lock.acquire()
    worker.initialize()
    if failure == "workspace_conflict":
        (root / "workspaces" / bad_repo / "unregistered").mkdir(parents=True)
    elif failure == "filesystem_error":
        (root / "workspaces" / bad_repo).mkdir(parents=True)
    peers = Gateway[WorkerMessageUnion, ServerMessageUnion]()
    host_transport, worker_transport = in_process_transport_pair()
    messages: list[WorkerMessageUnion] = []
    send = worker_transport.send_text

    async def record_message(raw):
        messages.append(parse_worker_message(raw))
        await send(raw)

    monkeypatch.setattr(worker_transport, "send_text", record_message)
    github = RecordingGitHub()
    github.refs = {}
    subject = coordinator(store, github)
    callbacks = None

    async def host():
        nonlocal callbacks
        register = parse_worker_message(await host_transport.receive_text())
        assert isinstance(register, Register)
        session = Session(host_transport, parse=parse_worker_input, make_ack=control_ack, epoch=uuid4().hex)
        session.register_control(register)
        session.start()
        callbacks = ServerCallbacks(
            subject, worker_id="worker-a", register=register, session=session,
            database=store.database, tools=subject.tools, results=subject.results, notify_jobs=peers.wake_all,
            heartbeat_interval=0.05, heartbeat_timeout=10, poll_interval=0.02,
        )
        if failure == "filesystem_error":
            demand = callbacks.workers.workspace_demand
            monkeypatch.setattr(callbacks.workers, "workspace_demand",
                                lambda *args: [bad_repo, *demand(*args)])
        try:
            await peers.serve("worker-a", session, callbacks, replace=True)
        finally:
            await callbacks.aclose()

    host_task = asyncio.create_task(host())
    worker_task = asyncio.create_task(worker._serve_connection(worker_transport))
    try:
        await wait_until(lambda: good_job.id in worker.running or worker_task.done() or host_task.done())
        assert not worker_task.done() and not host_task.done()
        assert good_job.id in worker.running
        assert required(store.jobs.get_job(bad_job.id)).state is JobState.QUEUED
        worker.finish[good_job.id].set()
        await wait_until(lambda: worker.executions[good_job.required_flow_id].state.load().acknowledged)
        assert worker._connected
        replies = [(index, item) for index, message in enumerate(messages)
                   for item in message.payload if isinstance(item, ProvisionResult)]
        assert [(item.repository, item.error) for _, item in replies] == [
            (bad_repo, failure), ("owner/repo", None),
        ]
        assert all(isinstance(messages[index - 1], Inventory) for index, _ in replies)
        assert "private local path" not in "".join(message.model_dump_json() for message in messages)
        if failure == "filesystem_error":
            slots = [w for w in store.workers.list_workspaces("worker-a") if w.repository == bad_repo]
            assert len(slots) == 1  # Publish only the directory whose creation completed.
            assert (root / "workspaces" / bad_repo / slots[0].workspace).is_dir()
    finally:
        await host_transport.close()
        await asyncio.gather(host_task, worker_task, return_exceptions=True)
        await asyncio.gather(*(execution.shutdown() for execution in worker.executions.values()))
        worker.root_lock.release()
