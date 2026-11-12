import asyncio
import base64
import threading
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.contracts.files import MAX_FILE_BYTES, FileFailure
from app.contracts.protocol import (
    PROTOCOL_VERSION,
    FileRegister,
    FileRegistered,
    FileResult,
    Heartbeat,
    parse_worker_message,
)
from app.controler.workers.connection import WorkerConnection
from app.controler.workers.conversation import ConversationBroker
from app.controler.workers.files import FileService
from tests.test_conversation_proxy import history_connection
from tests.test_worker_connection import Socket


@pytest.fixture
def files(store):
    store.workers.worker_connected("worker", PROTOCOL_VERSION, "epoch")
    return FileService(store.database)


async def test_file_routing_isolates_worker_identity_and_connection_epoch(files, store):
    file_id = uuid4().hex
    link = files.store.register("worker", file_id)
    store.workers.worker_connected("other", PROTOCOL_VERSION, "other-epoch")
    other_link = files.store.register("other", file_id)
    assert link.link_id != other_link.link_id
    owner, other = history_connection(), history_connection()
    files.connect("worker", "epoch", owner)
    files.connect("other", "other-epoch", other)
    reading = asyncio.create_task(files.read(link.link_id))
    request = await asyncio.wait_for(owner.sent.get(), 1)
    assert other.sent.empty()
    result = FileResult(request_id=request.request_id, connection_epoch="epoch", content_base64="")
    forged = result.model_copy(update={"content_base64": base64.b64encode(b"forged").decode()})
    assert files.accept(other, forged)
    assert files.accept(owner, forged.model_copy(update={"connection_epoch": "old"}))
    assert files.accept(owner, result)
    assert await reading == b""
    assert not files.pending


@pytest.mark.parametrize("ending", ["timeout", "cancel", "replace", "disconnect"])
async def test_pending_file_reads_release_slots(files, ending):
    link = files.store.register("worker", uuid4().hex)
    connection = history_connection()
    files.connect("worker", "epoch", connection)
    if ending == "timeout":
        files.timeout = 0.01
    reading = asyncio.create_task(files.read(link.link_id))
    await asyncio.wait_for(connection.sent.get(), 1)
    if ending == "cancel":
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
    else:
        if ending == "replace":
            files.connect("worker", "new-epoch", history_connection())
        elif ending == "disconnect":
            files.disconnect(connection)
        with pytest.raises(FileFailure, match="query_timeout" if ending == "timeout" else "worker_unavailable"):
            await reading
    assert not files.pending


@pytest.mark.parametrize("payload", ["not-base64!", base64.b64encode(b"x" * (MAX_FILE_BYTES + 1)).decode()])
async def test_worker_response_must_be_valid_bounded_bytes(files, payload):
    link = files.store.register("worker", uuid4().hex)
    connection = history_connection()
    files.connect("worker", "epoch", connection)
    reading = asyncio.create_task(files.read(link.link_id))
    request = await asyncio.wait_for(connection.sent.get(), 1)
    files.accept(connection, FileResult(request_id=request.request_id, connection_epoch="epoch",
                                        content_base64=payload))
    with pytest.raises(FileFailure, match="invalid_response"):
        await reading


async def test_file_queries_have_per_worker_backpressure(files):
    link = files.store.register("worker", uuid4().hex)
    connection = history_connection()
    files.connect("worker", "epoch", connection)
    queries = [asyncio.create_task(files.read(link.link_id)) for _ in range(4)]
    try:
        for _ in queries:
            await asyncio.wait_for(connection.sent.get(), 1)
        with pytest.raises(FileFailure, match="query_busy"):
            await files.read(link.link_id)
    finally:
        for query in queries:
            query.cancel()
        await asyncio.gather(*queries, return_exceptions=True)
    assert not files.pending


async def test_registration_does_not_block_execution_messages(files, monkeypatch):
    # Slow DB registration must not stall the one socket reader or execution heartbeats.
    started, release = threading.Event(), threading.Event()
    original = files.store.register

    def register(worker_id, file_id):
        started.set()
        assert release.wait(5)
        return original(worker_id, file_id)

    monkeypatch.setattr(files.store, "register", register)
    socket = Socket()
    connection = WorkerConnection(socket, ConversationBroker(), files, "worker", "epoch")
    request = FileRegister(request_id=uuid4().hex, file_id=uuid4().hex, connection_epoch="epoch")
    heartbeat = Heartbeat(job_id=1, assignment_token="token", connection_epoch="epoch")
    try:
        await socket.incoming.put(request.model_dump_json())
        assert await asyncio.to_thread(started.wait, 5)
        await socket.incoming.put(heartbeat.model_dump_json())
        assert await asyncio.wait_for(connection.receive_text(), 1) == heartbeat.model_dump_json()
        release.set()
        result = await asyncio.wait_for(socket.sent.get(), 2)
        assert isinstance(result, FileRegistered) and result.link
    finally:
        release.set()
        await connection.stop()


async def test_stale_registration_cannot_allocate_a_link(files, monkeypatch):
    register = Mock()
    monkeypatch.setattr(files.store, "register", register)
    connection = history_connection()
    files.connect("worker", "epoch", connection)
    await files.register("worker", connection, FileRegister(
        request_id=uuid4().hex, file_id=uuid4().hex, connection_epoch="old",
    ))
    register.assert_not_called()


def test_file_protocol_rejects_paths_and_old_versions():
    message = FileRegister(request_id=uuid4().hex, file_id=uuid4().hex, connection_epoch="epoch")
    assert parse_worker_message(message.model_dump_json()) == message
    payload = message.model_dump()
    with pytest.raises(ValidationError):
        FileRegister.model_validate({**payload, "path": "/private/path"})
    with pytest.raises(ValidationError):
        FileRegister.model_validate({**payload, "file_id": "../../private/path"})
    for version in (15, 16):
        with pytest.raises(ValidationError):
            FileRegister.model_validate({**payload, "protocol_version": version})
