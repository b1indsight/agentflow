"""File links expose only explicitly registered bytes, through the authenticated server."""

import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import Mock

import httpx
import pytest

from app.contracts.files import MAX_FILE_BYTES, FileFailure
from app.contracts.protocol import InitialTaskResult
from app.controler.api import create_app
from app.controler.runtime import ServerRuntime
from app.controler.workers.conversation import ConversationBroker
from app.controler.workers.gateway import WorkerGateway
from app.domain.models import JobKind
from app.worker.state.file_links import LocalFileLinks
from app.worker.state.session import WorkerSession
from app.worker.transport.client import WorkerClient
from tests.access import AccessIssuer
from tests.test_remote_asgi import AsgiCoordinator
from tests.test_remote_e2e import FakeExecutor, QueueConnection, QueueSocket


@asynccontextmanager
async def connected_files(store, root):
    coordinator = AsgiCoordinator(1, store)
    gateway = WorkerGateway(
        coordinator, database=store.database, worker_tokens={"worker-1": "worker-secret"},
        tools=coordinator.tools, results=coordinator.results,
        conversations=ConversationBroker(), poll_interval=0.01,
    )
    ready = asyncio.Event()
    frames = []

    class Client(WorkerClient):
        async def _serve_assignments(self, connection, first):
            ready.set()
            await super()._serve_assignments(connection, first)

    class RecordingConnection(QueueConnection):
        async def send(self, data):
            frames.append(data)
            await super().send(data)

    to_worker, to_server = asyncio.Queue(), asyncio.Queue()
    worker = Client(worker_id="worker-1", worker_token="worker-secret", serve_ws_url="unused",
                    executor=FakeExecutor(), session=WorkerSession(root))
    server = asyncio.create_task(gateway.handle(QueueSocket(to_server, to_worker)))
    client = asyncio.create_task(worker.run_connection(RecordingConnection(to_server, to_worker)))
    issuer = AccessIssuer()
    runtime = Mock(spec=ServerRuntime, access=issuer.auth, files=gateway.files)
    app = create_app(runtime)
    app.state.runtime = runtime
    try:
        await asyncio.wait_for(ready.wait(), 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                    headers=issuer.headers()) as http:
            yield worker, gateway.files, http, frames
    finally:
        server.cancel()
        client.cancel()
        await asyncio.gather(server, client, return_exceptions=True)
        await worker._cancel_execution()
        await issuer.auth.close()


@pytest.mark.parametrize("content", [b"", b"\x00\xff<script>alert('untrusted')</script>\r\n",
                                     b"x" * MAX_FILE_BYTES], ids=["empty", "binary", "limit"])
async def test_register_read_restart_and_path_privacy(store, tmp_path, content):
    path = tmp_path / "private-directory" / "private-name.bin"
    path.parent.mkdir()
    path.write_bytes(content)
    root = tmp_path / "worker"
    async with connected_files(store, root) as (worker, files, http, frames):
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
        assert not files.pending
    # Both services are reconstructed; persisted mappings keep the original URL usable.
    async with connected_files(store, root) as (worker, files, http, _):
        assert await worker.register_file(path) == link
        path.write_bytes(b"updated")
        assert (await http.get(link)).content == b"updated"
        path.unlink()
        missing = await http.get(link)
        assert missing.status_code == 404
        assert missing.json() == {"detail": "file_not_found"}
        files.disconnect(files.connections["worker-1"][1])
        assert (await http.get(link)).status_code == 503


async def test_file_registration_and_read_while_assignment_is_running(store, tmp_path, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def execute(task, secret=None, *, tool_caller):
        started.set()
        await release.wait()
        return InitialTaskResult(session_id=None, last_message="finished", exit_code=0)

    path = tmp_path / "report"
    path.write_text("progress")
    async with connected_files(store, tmp_path / "worker") as (worker, _, http, _):
        monkeypatch.setattr(worker.executor, "execute", execute)
        flow = store.flows.create_flow("owner/repo", 1, 42)
        store.flows.create_attempt(flow.id)
        store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
        try:
            await asyncio.wait_for(started.wait(), 2)
            link = await asyncio.wait_for(worker.register_file(path), 2)
            response = await asyncio.wait_for(http.get(link), 2)
            assert response.status_code == 200 and response.content == b"progress"
            assert worker.session.assignment is not None
        finally:
            release.set()


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
