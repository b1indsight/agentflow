"""The updater drains the ordinary protocol and the next process registers normally."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from websockets.exceptions import ConnectionClosedError

from app.contracts.protocol import (
    AvailabilityAccepted,
    ExecutionStopped,
    Failed,
    Heartbeat,
    InitialTaskResult,
    Register,
    ResultAccepted,
    StopAccepted,
    parse_worker_message,
)
from app.domain.models import JobState
from app.releases import ReleaseEvent
from app.worker.state.session import WorkerSession
from app.worker.transport.client import WorkerClient
from app.worker.updates import coordinator
from app.worker.updates.coordinator import UpdateCoordinator
from app.worker.updates.state import PreparedRelease, write_json
from tests.test_controller_update_boundary import DuplexSocket, scenario
from tests.test_worker_session import _assignment


def local_client(path, executor=None):
    return WorkerClient(
        worker_id="w",
        worker_token="test",
        serve_ws_url="ws://unused",
        executor=executor or Mock(),
        session=WorkerSession(path),
    )


def test_local_idle_requires_durable_result_and_stop_ack_without_in_memory_assignment(tmp_path):
    client = local_client(tmp_path)
    assignment = _assignment()
    client.session.record_received(assignment)
    record = client.session.record_started()
    client.session.record_terminal(
        Failed(
            job_id=assignment.job_id,
            assignment_token=assignment.assignment_token,
            connection_epoch=assignment.connection_epoch,
            error="test",
        )
    )
    client.session.assignment = None  # An interrupted transport can have only the journal.
    assert not client.idle_for_update()
    client.session.acknowledge(assignment.job_id, JobState.FAILED)
    assert client.idle_for_update() and client.session.path.exists()
    stopped = ExecutionStopped(
        request_id="stop",
        flow_id=assignment.flow_id,
        job_id=assignment.job_id,
        assignment_token=assignment.assignment_token,
        connection_epoch=assignment.connection_epoch,
        execution_nonce=record.execution_nonce,
    )
    client.session.record_stopped(stopped)
    assert not client.idle_for_update()
    client.session.acknowledge_stop(
        StopAccepted(**stopped.model_dump(exclude={"type", "execution_nonce"}))
    )
    assert client.idle_for_update()


@pytest.fixture
def prepared_update(tmp_path, monkeypatch):
    root = tmp_path / "update"
    old = PreparedRelease(
        release=ReleaseEvent(run_id=1, attempt=1, run_number=1, sha="1" * 40),
        path=root / "releases/old",
    )
    target = PreparedRelease(
        release=ReleaseEvent(run_id=2, attempt=1, run_number=2, sha="2" * 40),
        path=root / "releases/new",
    )
    write_json(root / "current.json", old.model_dump(mode="json"))
    monkeypatch.setattr(coordinator, "prepare", lambda *args: target)
    monkeypatch.setattr(coordinator, "running_sha", lambda: old.release.sha)
    monkeypatch.setattr(coordinator, "verify_release", lambda event: event)
    monkeypatch.setattr(coordinator, "validate_prepared", lambda *args: None)
    monkeypatch.setattr(UpdateCoordinator, "_managed", lambda self: True)
    return root, old, target


@pytest.mark.parametrize("ack", [ResultAccepted, StopAccepted])
async def test_update_waits_for_execution_ack_then_next_process_registers_normally(
    store,
    tmp_path,
    prepared_update,
    ack,
):
    # Pause while execution is active, then withhold its terminal/stop ACK. The
    # process cannot exit just because the executor has finished or was cancelled.
    root, old, target = prepared_update
    case = scenario(store)
    gateway = case.gateway()
    finish = asyncio.Event()

    async def execute(*args, **kwargs):
        await finish.wait()
        return InitialTaskResult(session_id="session", last_message="done", exit_code=0)

    class RecordingSocket(DuplexSocket):
        def __init__(self, blocked=None):
            super().__init__(blocked)
            self.registration = None

        async def send(self, raw):
            message = parse_worker_message(raw)
            if isinstance(message, Register):
                self.registration = json.loads(raw)
            await super().send(raw)

    client = local_client(tmp_path / "worker", Mock(execute=AsyncMock(side_effect=execute)))
    socket = RecordingSocket(ack)
    stopped = Mock()
    updater = UpdateCoordinator(root, client, apply=True, stop=stopped)
    tasks = [
        asyncio.create_task(gateway.handle(socket)),
        asyncio.create_task(client.run_connection(socket)),
    ]
    try:
        await asyncio.wait_for(case.started.wait(), 2)
        update = asyncio.create_task(updater.update(target.release, apply=True))
        tasks.append(update)
        async with asyncio.timeout(2):
            while not gateway._sessions["w"][1].dispatch_paused:
                await asyncio.sleep(0)
        if ack is StopAccepted:
            with store.transaction() as tx:
                tx.lock_workflow(case.job.flow_id)
                tx.stops.request(case.job.flow_id)
            assignment = client.session.assignment
            await socket.send(
                Heartbeat(
                    job_id=assignment.job_id,
                    assignment_token=assignment.assignment_token,
                    connection_epoch=assignment.connection_epoch,
                ).model_dump_json()
            )
        else:
            finish.set()
        await asyncio.wait_for(socket.entered.wait(), 2)
        assert not client.idle_for_update() and not stopped.called
        assert PreparedRelease.model_validate_json((root / "current.json").read_bytes()) == old
        socket.release.set()
        await asyncio.wait_for(update, 3)
        assert stopped.call_count == 1 and client.idle_for_update()
        assert PreparedRelease.model_validate_json((root / "current.json").read_bytes()) == target
        assert not (root / "operation.json").exists()

        new = RecordingSocket()
        replacement = local_client(tmp_path / "worker")
        tasks.extend(
            [
                asyncio.create_task(gateway.handle(new)),
                asyncio.create_task(replacement.run_connection(new)),
            ]
        )
        async with asyncio.timeout(2):
            while replacement.availability.epoch is None:
                await asyncio.sleep(0)
        assert new.registration.keys() <= {"type", "protocol_version", "worker_id", "recovery"}
        assert not gateway._sessions["w"][1].dispatch_paused
        assert client.availability.epoch != replacement.availability.epoch
        replacement.executor.execute.assert_not_called()
    finally:
        socket.release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client._cancel_execution()


@pytest.mark.parametrize("reconnect_at", ["pause", "closed-send", "verification"])
async def test_connection_change_requires_a_new_pause_before_switch(
    tmp_path,
    prepared_update,
    monkeypatch,
    reconnect_at,
):
    root, _, target = prepared_update
    client = local_client(tmp_path)
    channel = client.availability
    attempted = asyncio.Queue()

    class Socket:
        async def send(self, raw):
            message = parse_worker_message(raw)
            await attempted.put(message)
            if message.connection_epoch == "old" and reconnect_at == "closed-send":
                # The send can observe a closed WebSocket before its reader has
                # cleared the old epoch; the updater must wait for normal reconnect.
                asyncio.get_running_loop().call_soon(channel.connect, self, "new")
                raise ConnectionClosedError(None, None)
            if message.connection_epoch == "old" and reconnect_at == "pause":
                channel.disconnect()
                channel.connect(self, "new")
                return
            channel.accept(AvailabilityAccepted(**message.model_dump(exclude={"type"})))

    socket = Socket()
    channel.connect(socket, "old")
    if reconnect_at == "verification":
        real_thread = asyncio.to_thread

        async def thread(function, *args):
            if function is coordinator.verify_release and channel.epoch == "old":
                channel.disconnect()
                channel.connect(socket, "new")
            return await real_thread(function, *args)

        monkeypatch.setattr(asyncio, "to_thread", thread)
    stopped = Mock()
    await UpdateCoordinator(root, client, apply=True, stop=stopped).update(
        target.release, apply=True
    )
    assert [(await attempted.get()).connection_epoch for _ in range(attempted.qsize())] == [
        "old",
        "new",
    ]
    stopped.assert_called_once()


@pytest.mark.parametrize("lose_resume", [False, True])
async def test_lost_pause_ack_resumes_or_closes_instead_of_leaving_worker_paused(
    tmp_path,
    prepared_update,
    monkeypatch,
    lose_resume,
):
    root, old, target = prepared_update
    client = local_client(tmp_path)
    channel = client.availability
    sent = []
    close = AsyncMock()
    timeout = asyncio.timeout

    monkeypatch.setattr(
        asyncio, "timeout", lambda seconds: timeout(0.01 if seconds == 15 else seconds)
    )

    class Socket:
        async def send(self, raw):
            message = parse_worker_message(raw)
            sent.append(message.paused)
            if not message.paused and not lose_resume:
                channel.accept(AvailabilityAccepted(**message.model_dump(exclude={"type"})))

        async def close(self):
            await close()

    channel.connect(Socket(), "epoch")
    stopped = Mock()
    with pytest.raises(TimeoutError):
        await UpdateCoordinator(root, client, apply=True, stop=stopped).update(
            target.release, apply=True
        )
    assert sent == [True, False] and not stopped.called
    assert close.await_count == int(lose_resume)
    assert PreparedRelease.model_validate_json((root / "current.json").read_bytes()) == old


async def test_check_only_prepares_without_pausing_or_changing_pointer(tmp_path, prepared_update):
    root, old, target = prepared_update
    client = local_client(tmp_path)
    stopped = Mock()
    await UpdateCoordinator(root, client, apply=False, stop=stopped).update(
        target.release, apply=False
    )
    assert PreparedRelease.model_validate_json((root / "current.json").read_bytes()) == old
    assert json.loads((root / "status.json").read_bytes())["state"] == "checked"
    assert not stopped.called and client.availability.epoch is None
