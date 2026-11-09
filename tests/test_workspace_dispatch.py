"""Real PostgreSQL, Session endpoints and local Git slots; no external coding service."""

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.communication.gateway import Gateway
from app.communication.inprocess import in_process_transport_pair
from app.communication.session import Session
from app.contracts.protocol import (
    AssignmentArguments,
    ExecutionTask,
    InitialTask,
    Register,
    ServerMessageUnion,
    WorkerMessageUnion,
    parse_worker_message,
)
from app.contracts.validation import parse_worker_input
from app.controler.workers.callbacks import ServerCallbacks
from app.controler.workers.transport import control_ack
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.results import ResultApplication
from app.domain.models import ArchiveReason, FlowState, Job, JobState
from app.persistence.records import required
from app.persistence.store import Store
from app.worker.config import WorkerSettings
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.runtime import WorkerRuntime
from app.worker.state.execution import ExecutionState
from tests.test_worker_workspaces import managed_seed
from tests.test_workspace_claims import queued


class ControlledWorker(WorkerRuntime):
    def __init__(self, root: Path, limit: int) -> None:
        super().__init__(WorkerSettings(
            worker_id="worker-a", worker_token=SecretStr("test"), serve_ws_url="wss://unused",
            worker_root=root, worker_repo_workspace_limit=limit,
        ))
        self.running: dict[int, Path] = {}
        self.finish: dict[int, asyncio.Event] = {}

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

            async def reclaim_workspace(self):
                record = required(state.load())
                assert record.workspace is not None
                await worker.workspaces.release(
                    record.workspace, job_id=record.job_id, token=record.assignment_token, recycle=True,
                )

        return WorkerExecution(worker_id="worker-a", executor=Executor(), state=state,
                               on_change=self.notify)


class ControlledCoordinator(JobCoordinator):
    def __init__(self, store: Store) -> None:
        self.store = store

    async def prepare_for_assignment(self, job, worker_id, token, epoch):
        flow = required(self.store.flows.get_flow(job.required_flow_id))
        assert flow.current_attempt_id is not None
        return AssignmentArguments(
            job_id=job.id, flow_id=flow.id, assignment_token=token,
            workspace=job.workspace, generation=job.generation,
            task=InitialTask(
                flow_id=flow.id, attempt_id=flow.current_attempt_id,
                repo=flow.repo, repo_identity=f"{worker_id}:{flow.repo}", issue_number=flow.issue_number,
                issue_title="Controlled", issue_body="", branch=f"flow/{flow.id}",
            ),
        )

    def mark_started(self, job: Job, task: ExecutionTask, worker_id: str) -> None:
        pass


class AppliedResults(ResultApplication):
    async def _apply(self, job_id: int) -> JobState:
        assert self.jobs.complete_persisted_result(job_id, JobState.FAILED)
        return JobState.FAILED


async def wait_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(10):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("limit", [2, 3, 17])
async def test_demand_concurrent_dispatch_stop_and_result_isolate_workspaces(store, tmp_path, limit):
    root, _ = managed_seed(tmp_path)
    shutil.copytree(root / "repos/owner/repo", root / "repos/owner/other")
    jobs = [queued(store, issue, repo) for repo in ("owner/repo", "owner/other")
            for issue in range(limit + 1)]
    worker = ControlledWorker(root, limit)
    worker.root_lock.acquire()
    worker.initialize()
    assert len(worker.workspaces.inventory()) == 1  # Only the real inference directory initially.
    assert worker.workspaces.inference_path.is_dir()
    peers = Gateway[WorkerMessageUnion, ServerMessageUnion]()
    host_transport, worker_transport = in_process_transport_pair()
    results = AppliedResults(Mock(), Mock(), Mock(), database=store.database, effects=Mock())
    callbacks = None

    async def host():
        nonlocal callbacks
        register = parse_worker_message(await host_transport.receive_text())
        assert isinstance(register, Register)
        session = Session(host_transport, parse=parse_worker_input, make_ack=control_ack, epoch=uuid4().hex)
        session.register_control(register)
        session.start()
        callbacks = ServerCallbacks(
            ControlledCoordinator(store), worker_id="worker-a", register=register, session=session,
            database=store.database, tools=Mock(), results=results, notify_jobs=peers.wake_all,
            heartbeat_interval=0.05, heartbeat_timeout=10, poll_interval=0.02,
        )
        try:
            await peers.serve("worker-a", session, callbacks, replace=True)
        finally:
            await callbacks.aclose()

    host_task = asyncio.create_task(host())
    worker_task = asyncio.create_task(worker._serve_connection(worker_transport))
    try:
        await wait_until(lambda: len(worker.running) == 2 * limit)
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

        # Stopping one Flow must not stop its siblings; its cleaned slot is reusable only after ACK.
        stopped_job = required(store.jobs.get_job(stopped))
        with store.transaction() as tx:
            flow = required(tx.lock_workflow(stopped_job.required_flow_id))
            flow.state, flow.archive_reason = FlowState.FINALIZING, ArchiveReason.ABANDONED
            tx.flows.update_flow(flow)
            tx.stops.request(flow.id)
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
