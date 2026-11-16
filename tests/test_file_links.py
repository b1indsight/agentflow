"""File links expose only explicitly registered bytes, through the authenticated server."""

import asyncio
import os
import threading
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import SecretStr

from app.contracts.files import MAX_FILE_BYTES, FileFailure
from app.contracts.protocol import FileRegister, WorkerMessage, call_header, message_header
from app.controler.api import create_app
from app.controler.workers.callbacks import ServerCallbacks
from app.controler.workers.transport import serve_worker
from app.worker.config import WorkerSettings
from app.worker.execution.lifecycle import WorkerProtocolError
from app.worker.runtime import WorkerRuntime
from app.worker.state.file_links import LocalFileLinks
from app.worker.transport.state import WorkerStateCallbacks
from tests.access import AccessIssuer
from tests.test_remote_e2e import QueueConnection, QueueSocket, remote_connection
from tests.test_remote_e2e import remote_flow as remote_flow  # noqa: PLC0414
from tests.test_workspace_dispatch import wait_until
from tests.worker_fixtures import run_worker


@asynccontextmanager
async def connected_files(server_runtime, root):
    runtime = server_runtime()
    frames = []

    class RecordingConnection(QueueConnection):
        async def send(self, data):
            frames.append(data)
            await super().send(data)

    to_worker, to_server = asyncio.Queue(), asyncio.Queue()
    worker = WorkerRuntime(WorkerSettings(
        worker_id="worker-1", worker_token=SecretStr("worker-secret"),
        serve_ws_url="wss://unused", worker_root=root,
    ))
    worker.root_lock.acquire()
    server = asyncio.create_task(serve_worker(
        QueueSocket(to_server, to_worker), worker_tokens=runtime.worker_tokens,
        serve=runtime.serve_worker_session,
    ))
    client = asyncio.create_task(run_worker(worker, RecordingConnection(to_server, to_worker)))
    issuer = AccessIssuer()
    await runtime.access.close()
    runtime.access = issuer.auth
    app = create_app(runtime)
    app.state.runtime = runtime
    try:
        await wait_until(lambda: worker._connected)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                    headers=issuer.headers()) as http:
            yield worker, runtime, http, frames
    finally:
        server.cancel()
        client.cancel()
        outcomes = await asyncio.gather(server, client, return_exceptions=True)
        worker.root_lock.release()
        await issuer.auth.close()
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                raise outcome


@pytest.mark.parametrize("content", [b"", b"\x00\xff<script>alert('untrusted')</script>\r\n",
                                     b"x" * MAX_FILE_BYTES], ids=["empty", "binary", "limit"])
async def test_register_read_restart_and_path_privacy(server_runtime, tmp_path, content):
    path = tmp_path / "private-directory" / "private-name.bin"
    path.parent.mkdir()
    path.write_bytes(content)
    root = tmp_path / "worker"
    async with connected_files(server_runtime, root) as (worker, runtime, http, frames):
        links = await asyncio.gather(worker.register_file(path), worker.register_file(path))
        link = links[0]
        assert links == [link, link]
        assert link.startswith("/api/files/")
        assert worker.files.path.stat().st_mode & 0o777 == 0o600
        result = await http.get(link)
        assert result.status_code == 200
        assert result.content == content
        assert result.headers["content-type"] == "application/octet-stream"
        assert result.headers["content-disposition"] == "attachment"
        assert result.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in result.headers["content-security-policy"]
        assert result.headers["cache-control"] == "no-store"
        denied = await http.get(link, headers={"cf-access-jwt-assertion": "invalid"})
        assert denied.status_code == 401
        assert (await http.get("/api/files/" + "0" * 32)).status_code == 404
        assert (await http.get("/api/files/not-an-id")).status_code == 404
        assert path.name not in link + str(result.headers) + "".join(frames)
        assert str(path) not in "".join(frames)
        assert not runtime.files._inflight
    # Both services are reconstructed; persisted mappings keep the original URL usable.
    async with connected_files(server_runtime, root) as (worker, runtime, http, _):
        assert await worker.register_file(path) == link
        path.write_bytes(b"updated")
        assert (await http.get(link)).content == b"updated"
        path.unlink()
        missing = await http.get(link)
        assert missing.status_code == 404
        assert missing.json() == {"detail": "file_not_found"}
        await runtime.gateway.unregister(runtime.gateway.get("worker-1"))
        assert (await http.get(link)).status_code == 503


async def test_file_registration_and_read_while_assignment_is_running(remote_flow, tmp_path, monkeypatch):
    flow, runtime, _, worker, agent = remote_flow
    started, release = asyncio.Event(), asyncio.Event()
    original = agent.start

    async def execute(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(agent, "start", execute)
    path = tmp_path / "report"
    path.write_text("progress")
    async with remote_connection(runtime, worker):
        try:
            await asyncio.wait_for(started.wait(), 10)
            link = await asyncio.wait_for(worker.register_file(path), 2)
            assert await asyncio.wait_for(runtime.files.read(link.rsplit("/", 1)[1]), 2) == b"progress"
            assert worker.executions[flow.id].state.assignment is not None
        finally:
            release.set()
        await wait_until(lambda: worker.executions[flow.id].state.load().acknowledged)


@pytest.mark.parametrize("operation", ["register", "read"])
async def test_slow_file_io_and_late_replies_do_not_interrupt_execution(
    remote_flow, tmp_path, monkeypatch, operation,
):
    # Slow disk/DB IO must leave the serial driver available for execution completion.
    flow, runtime, _, worker, agent = remote_flow
    executing, finish, late = asyncio.Event(), asyncio.Event(), asyncio.Event()
    blocked, release = threading.Event(), threading.Event()
    start = agent.start

    async def execute(*args, **kwargs):
        executing.set()
        await finish.wait()
        return await start(*args, **kwargs)

    callbacks = WorkerStateCallbacks if operation == "register" else ServerCallbacks
    unmatched = callbacks.on_unmatched_reply

    async def observe_late(self, reply, message):
        await unmatched(self, reply, message)
        if reply.method == ("server.file_register" if operation == "register" else "worker.file_read"):
            late.set()

    monkeypatch.setattr(agent, "start", execute)
    monkeypatch.setattr(callbacks, "on_unmatched_reply", observe_late)
    path = tmp_path / "report"
    path.write_bytes(b"progress")
    async with remote_connection(runtime, worker):
        await asyncio.wait_for(executing.wait(), 10)
        link = await worker.register_file(path)
        target = runtime.files.store if operation == "register" else worker.files
        original = getattr(target, operation)

        def slow(*args):
            blocked.set()
            assert release.wait(10)
            return original(*args)

        monkeypatch.setattr(target, operation, slow)
        runtime.files.timeout = 0.05
        pending = asyncio.create_task(worker.register_file(path) if operation == "register"
                                      else runtime.files.read(link.rsplit("/", 1)[1]))
        try:
            assert await asyncio.to_thread(blocked.wait, 5)
            if operation == "register":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                with pytest.raises(FileFailure, match="query_timeout"):
                    await pending
            finish.set()
            await wait_until(lambda: worker.executions[flow.id].state.load().acknowledged)
            assert not release.is_set()
            release.set()
            await asyncio.wait_for(late.wait(), 2)
            runtime.files.timeout = 1
            assert await worker.register_file(path) == link
            assert await runtime.files.read(link.rsplit("/", 1)[1]) == b"progress"
        finally:
            release.set()
            finish.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_stale_registration_cannot_allocate_a_link(server_runtime, tmp_path):
    with pytest.raises(WorkerProtocolError, match="connection epoch"):
        async with connected_files(server_runtime, tmp_path / "worker") as (worker, runtime, _, _):
            call = FileRegister(**call_header(), method="server.file_register", file_id="a" * 32)
            await worker.gateway.get("server").session.send(
                WorkerMessage(**message_header("stale"), control=None, payload=[call]),
            )
            await wait_until(lambda: runtime.gateway.get("worker-1") is None)
    with runtime.database.connection() as connection:
        assert connection.execute("SELECT * FROM worker_file_links").fetchall() == []


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo", "large", "unreadable"])
def test_registration_rejects_unreadable_or_unbounded_files(tmp_path, kind):
    path = tmp_path / "private-file"
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "large":
        path.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    elif kind == "unreadable":
        path.touch(mode=0)
    with pytest.raises(FileFailure) as error:
        LocalFileLinks(tmp_path / "worker").register(path)
    assert str(path) not in str(error.value)


def test_links_reject_file_substitution_and_growth(tmp_path):
    # Workspaces are reused: an old URL must not reveal the next Flow's replacement file.
    path = tmp_path / "file"
    path.write_bytes(b"original")
    links = LocalFileLinks(tmp_path / "worker")
    handle = links.register(path)
    path.rename(tmp_path / "original")
    path.write_bytes(b"another Flow")
    with pytest.raises(FileFailure, match="file_changed"):
        links.read(handle)
    path.unlink()
    path.symlink_to(tmp_path / "original")
    with pytest.raises(FileFailure):
        links.read(handle)
    path.unlink()
    (tmp_path / "original").rename(path)
    path.write_bytes(b"x" * MAX_FILE_BYTES)
    assert links.read(handle) == b"x" * MAX_FILE_BYTES
    with path.open("ab") as stream:
        stream.write(b"x")
    with pytest.raises(FileFailure, match="file_too_large"):
        links.read(handle)
