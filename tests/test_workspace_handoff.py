"""Workspace handoff waits for writers, independently of result/stop acknowledgement."""

import asyncio
import json

import pytest
from pydantic import SecretStr

from app.contracts.protocol import StopFence
from app.worker.config import WorkerSettings
from app.worker.execution.lifecycle import WorkerProtocolError
from app.worker.runtime import WorkerRuntime
from tests.test_workspace_inventory import assignment


async def runtime_at(root):
    runtime = WorkerRuntime(WorkerSettings(
        worker_id="worker-a", worker_token=SecretStr("test"), serve_ws_url="wss://test.invalid/ws",
        worker_root=root, worker_repo_workspace_limit=1,
    ))
    runtime.initialize()
    target = (await runtime.workspaces.provision("owner/repo"))[1]
    return runtime, target


@pytest.mark.parametrize("outcome", ["terminal", "stop", "unknown"])
async def test_new_generation_requires_quiescence_but_not_ack(tmp_path, outcome):
    runtime, target = await runtime_at(tmp_path)
    first = runtime.receive(assignment(1, target.workspace))
    first.state.record_started()
    if outcome == "terminal":
        first.state.complete(RuntimeError("finished"))
    elif outcome == "stop":
        # Failed preparation can stop before a pool binding was ever journaled.
        first.state.complete(RuntimeError("preparation failed"))
        await first.stop(StopFence(request_id="stop", flow_id=1, job_id=1, assignment_token="token-1"))
    before = first.state.path.read_bytes()
    next_assignment = assignment(2, target.workspace, generation=2)
    if outcome == "unknown":
        with pytest.raises(WorkerProtocolError, match="execution"):
            runtime.receive(next_assignment)
        assert 2 not in runtime.executions
    else:
        second = runtime.receive(next_assignment)
        assert second.state.load().selected_generation == 2
        assert not first.state.load().acknowledged
    assert first.state.path.read_bytes() == before


async def test_cancelled_executor_must_finish_its_writes_before_handoff(tmp_path):
    runtime, target = await runtime_at(tmp_path)
    first = runtime.receive(assignment(1, target.workspace))
    entered, cancelling, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Executor:
        async def execute(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await finish.wait()
                raise

    first.executor = Executor()
    first.start()
    await entered.wait()
    stop = asyncio.create_task(first.stop(
        StopFence(request_id="stop", flow_id=1, job_id=1, assignment_token="token-1"),
    ))
    try:
        await cancelling.wait()
        assert first.lease() is None
        with pytest.raises(WorkerProtocolError, match="execution"):
            runtime.receive(assignment(2, target.workspace, generation=2))
        assert first.state.load().stopped is None
    finally:
        finish.set()
        await stop
    runtime.receive(assignment(2, target.workspace, generation=2))
    assert first.state.load().stopped is not None


async def test_inventory_io_failure_does_not_persist_workspace_faults(tmp_path, monkeypatch):
    runtime, _ = await runtime_at(tmp_path)
    pool = runtime.workspaces
    before = pool.manifest_path.read_bytes()
    managed = pool._managed

    def fail_shared_parent(relative):
        if relative.parts and relative.parts[0] == "workspaces":
            raise OSError("temporary shared-parent IO failure")
        return managed(relative)

    with monkeypatch.context() as patch:
        patch.setattr(pool, "_managed", fail_shared_parent)
        assert len(pool.inventory()) == 2
        with pytest.raises(OSError):
            await pool.provision("owner/repo")
    assert pool.manifest_path.read_bytes() == before
    assert len(await pool.provision("owner/repo")) == 2
    assert "state" not in json.loads(pool.manifest_path.read_text())["slots"][0]
