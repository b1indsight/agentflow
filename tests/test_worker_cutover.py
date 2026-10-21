"""Exercise cooperative cutover against real journals and PostgreSQL fences."""

import asyncio
import json
from unittest.mock import Mock

import pytest

from app.contracts.protocol import (
    ExecutionStopped,
    Failed,
    Heartbeat,
    Register,
    Registered,
    StopAccepted,
    parse_worker_message,
)
from app.contracts.updates import MaintenanceReply
from app.controler.workers.connection import WorkerConnection
from app.controler.workers.conversation import ConversationBroker
from app.domain.models import JobState
from app.persistence.maintenance import MaintenanceStore
from app.persistence.worker_updates import WorkerUpdateStore
from app.releases import ReleaseError
from app.worker.state.session import WorkerSession
from app.worker.transport.client import WorkerClient
from app.worker.updates import coordinator
from app.worker.updates.channel import UpdateChannel
from app.worker.updates.coordinator import UpdateCoordinator
from app.worker.updates.state import PreparedRelease, UpdateOperation, write_json
from tests.test_maintenance import idle_binding
from tests.test_worker_connection import Socket as ServerSocket
from tests.test_worker_session import _assignment
from tests.test_worker_updates import request


def local_client(path, channel):
    return WorkerClient(
        worker_id="w",
        worker_token="test",
        serve_ws_url="wss://example.test/worker",
        executor=Mock(),
        session=WorkerSession(path),
        updates=channel,
    )


def test_local_idle_requires_result_and_stop_ack_even_without_executor(tmp_path):
    client = local_client(tmp_path, UpdateChannel())
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
    assert not client.idle_for_update()
    client.session.assignment = None  # A transport reconnect can have no in-memory assignment.
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


@pytest.mark.parametrize("failure", ["", "prepare", "commit-reply", "pointer"])
async def test_cutover_orders_pause_intent_pointer_and_new_instance_confirmation(
    store, tmp_path, monkeypatch, failure
):
    idle_binding(store)
    updates = WorkerUpdateStore(store.database)
    root = tmp_path / "update"
    root.mkdir()
    target = PreparedRelease(release=request().release, path=root / "releases/new")
    write_json(root / "current.json", {"old": True})
    stopped = Mock()
    actions = []

    class Channel(UpdateChannel):
        async def request(self, message):
            actions.append(message.action)
            operation = UpdateOperation.model_validate_json((root / "operation.json").read_bytes())
            assert operation.phase == ("cutover" if message.action == "commit" else "paused")
            reply = updates.handle("w", self.epoch, message)
            if failure == "commit-reply" and message.action == "commit":
                raise ConnectionError("lost commit response")
            return reply

    channel = Channel()
    channel.epoch = "epoch"
    worker_root = tmp_path / "worker"
    worker_root.mkdir()
    client = local_client(worker_root, channel)
    update = UpdateCoordinator(root, client, channel, apply=True, stop=stopped)
    monkeypatch.setattr(coordinator, "running_sha", lambda: "1" * 40)
    monkeypatch.setattr(UpdateCoordinator, "_managed", lambda *args: True)
    monkeypatch.setattr(coordinator, "verify_release", lambda event: event)
    monkeypatch.setattr(coordinator, "validate_prepared", lambda *args: None)

    def prepare(*args):
        if failure == "prepare":
            raise ReleaseError("bad build")
        return target

    monkeypatch.setattr(coordinator, "prepare", prepare)
    real_write = coordinator.write_json

    def write(path, value):
        if path.name == "current.json":
            assert actions[-1] == "commit"
            assert not stopped.called
            if failure == "pointer":
                raise OSError("disk full")
        return real_write(path, value)

    monkeypatch.setattr(coordinator, "write_json", write)
    if failure:
        with pytest.raises((ConnectionError, OSError, ReleaseError)):
            await update.update(target.release, apply=True)
        assert not stopped.called
        assert json.loads((root / "current.json").read_text()) == {"old": True}
        if failure == "prepare":
            assert not actions and not update.operation_path.exists()
        else:
            assert (
                UpdateOperation.model_validate_json(update.operation_path.read_bytes()).phase
                == "cutover"
            )
            assert actions == ["pause", "commit"]  # No automatic abort/rollback after intent.
        return
    await update.update(target.release, apply=True)
    assert stopped.call_count == 1
    assert actions == ["pause", "commit"]
    assert PreparedRelease.model_validate_json((root / "current.json").read_bytes()) == target
    operation = UpdateOperation.model_validate_json(update.operation_path.read_bytes())
    store.workers.worker_connected("w", 13, "new-epoch")
    channel.epoch = "new-epoch"
    new = UpdateCoordinator(root, client, channel, apply=True, stop=Mock())
    monkeypatch.setattr(coordinator, "running_sha", lambda: target.release.sha)
    write_json(root / "launch.json", {"operation": operation.operation_id})

    async def resume(message):
        assert message.action == "resume" and message.instance_id != operation.source_instance
        return updates.handle("w", "new-epoch", message)

    monkeypatch.setattr(channel, "request", resume)
    await asyncio.wait_for(new._finish(operation), 3)
    assert (
        UpdateOperation.model_validate_json(update.operation_path.read_bytes()).phase == "complete"
    )


async def test_check_only_does_not_pause_or_change_pointer(tmp_path, monkeypatch):
    channel = UpdateChannel()
    client = local_client(tmp_path, channel)
    update = UpdateCoordinator(tmp_path, client, channel, apply=False, stop=Mock())
    target = PreparedRelease(release=request().release, path=tmp_path / "releases/new")
    monkeypatch.setattr(coordinator, "prepare", lambda *args: target)
    await update.update(target.release, apply=False)
    assert not update.operation_path.exists() and not (tmp_path / "current.json").exists()
    assert json.loads((tmp_path / "status.json").read_text())["state"] == "checked"


async def test_update_replies_are_connection_scoped_without_a_second_reader():
    channel = UpdateChannel()
    sent = asyncio.Queue()

    class Socket:
        async def send(self, data):
            await sent.put(data)

    channel.connect(Socket(), "epoch")
    message = request()
    pending = asyncio.create_task(channel.request(message))
    await sent.get()
    reply = MaintenanceReply(
        request_id=message.request_id,
        operation_id=message.operation_id,
        connection_epoch="old-epoch",
        accepted=True,
        idle=True,
    )
    channel.accept(reply)
    assert not pending.done()
    channel.disconnect()
    with pytest.raises(ConnectionError):
        await pending


async def test_release_and_maintenance_share_the_execution_stream(store, tmp_path):
    # Exercise both production parsers/readers, not a second maintenance socket.
    idle_binding(store)
    updates = WorkerUpdateStore(store.database)
    updates.publish(request().release)
    socket = ServerSocket()
    channel = UpdateChannel()
    client = local_client(tmp_path, channel)

    class WorkerSocket:
        async def send(self, raw):
            await socket.incoming.put(raw)

        async def recv(self):
            return (await socket.sent.get()).model_dump_json()

    transport = WorkerSocket()
    task = asyncio.create_task(client.run_connection(transport))
    connection = None
    try:
        registration = parse_worker_message(await asyncio.wait_for(socket.incoming.get(), 1))
        assert isinstance(registration, Register) and registration.update_version == 1
        connection = WorkerConnection(socket, ConversationBroker(), "w", "epoch", updates,
                                      maintenance=MaintenanceStore(store.database))
        await connection.send_text(
            Registered(connection_epoch="epoch", heartbeat_interval=60).model_dump_json()
        )
        connection.start_updates()
        assert await asyncio.wait_for(channel.notices.get(), 1) == request().release
        heartbeat = Heartbeat(
            job_id=1, assignment_token="token", connection_epoch="epoch"
        ).model_dump_json()
        await transport.send(heartbeat)
        reply = await asyncio.wait_for(channel.request(request()), 1)
        assert reply.accepted and reply.idle
        assert await asyncio.wait_for(connection.receive_text(), 1) == heartbeat
        assert client.connected and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if connection is not None:
            await connection.stop()
