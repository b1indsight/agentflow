import asyncio
import base64
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.communication.gateway import Gateway
from app.contracts.files import MAX_FILE_BYTES, FileFailure
from app.contracts.protocol import (
    FileRegister,
    FileResult,
    WorkerMessage,
    call_header,
    message_header,
    parse_server_message,
    parse_worker_message,
    result_header,
)
from app.controler.workers.files import FileService
from tests.test_conversation_proxy import history_connection as history_connection  # noqa: PLC0414


@pytest.fixture
def peers():
    return Gateway()


@pytest.fixture
def files(store, peers):
    return FileService(peers, database=store.database)


async def request_from(socket):
    message = parse_server_message(await asyncio.wait_for(socket.sent.get(), 1))
    return message.payload[0]


async def answer(socket, request, *, epoch="epoch", content=b"", payload=None):
    response = FileResult(**result_header(request.message_id), method="worker.file_read",
                          content_base64=payload if payload is not None else base64.b64encode(content).decode())
    await socket.incoming.put(WorkerMessage(**message_header(epoch), control=None,
                                            payload=[response]).model_dump_json())


async def test_file_routing_isolates_worker_identity(files, peers, history_connection):
    file_id = uuid4().hex
    link = files.store.register("worker", file_id)
    other_link = files.store.register("other", file_id)
    assert link.link_id != other_link.link_id
    _, owner = await history_connection(peers)
    _, other = await history_connection(peers, worker="other", epoch="other-epoch")
    reading = asyncio.create_task(files.read(link.link_id))
    request = await request_from(owner)
    assert other.sent.empty()
    await answer(other, request, epoch="other-epoch", content=b"forged")
    await asyncio.wait_for(other.delivered.get(), 1)
    assert not reading.done()
    await answer(owner, request)
    assert await reading == b""
    assert not files._inflight


@pytest.mark.parametrize("ending", ["timeout", "cancel", "replace", "disconnect", "stale-epoch"])
async def test_pending_file_reads_release_slots(files, peers, history_connection, ending):
    link = files.store.register("worker", uuid4().hex)
    registration, socket = await history_connection(peers)
    if ending == "timeout":
        files.timeout = 0.02
    reading = asyncio.create_task(files.read(link.link_id))
    request = await request_from(socket)
    if ending == "cancel":
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
    else:
        if ending == "replace":
            await history_connection(peers, epoch="new-epoch")
        elif ending == "disconnect":
            await peers.unregister(registration)
        elif ending == "stale-epoch":
            await answer(socket, request, epoch="old", content=b"forged")
        with pytest.raises(FileFailure, match="query_timeout" if ending == "timeout" else "worker_unavailable"):
            await reading
    assert not files._inflight


@pytest.mark.parametrize("payload", ["not-base64!", base64.b64encode(b"x" * (MAX_FILE_BYTES + 1)).decode()])
async def test_worker_response_must_be_valid_bounded_bytes(files, peers, history_connection, payload):
    link = files.store.register("worker", uuid4().hex)
    _, socket = await history_connection(peers)
    reading = asyncio.create_task(files.read(link.link_id))
    await answer(socket, await request_from(socket), payload=payload)
    with pytest.raises(FileFailure, match="invalid_response"):
        await reading


async def test_file_queries_have_per_worker_backpressure(files, peers, history_connection):
    link = files.store.register("worker", uuid4().hex)
    _, socket = await history_connection(peers)
    queries = [asyncio.create_task(files.read(link.link_id)) for _ in range(4)]
    try:
        for _ in queries:
            await request_from(socket)
        with pytest.raises(FileFailure, match="query_busy"):
            await files.read(link.link_id)
    finally:
        for query in queries:
            query.cancel()
        await asyncio.gather(*queries, return_exceptions=True)
    assert not files._inflight


def test_file_protocol_rejects_paths_and_old_versions():
    call = FileRegister(**call_header(), method="server.file_register", file_id=uuid4().hex)
    message = WorkerMessage(**message_header("epoch"), control=None, payload=[call])
    assert parse_worker_message(message.model_dump_json()) == message
    payload = call.model_dump()
    with pytest.raises(ValidationError):
        FileRegister.model_validate({**payload, "path": "/private/path"})
    with pytest.raises(ValidationError):
        FileRegister.model_validate({**payload, "file_id": "../../private/path"})
    for version in (15, 16, 17, 24):
        with pytest.raises(ValidationError):
            parse_worker_message(json.dumps({**message.model_dump(), "protocol_version": version}))
