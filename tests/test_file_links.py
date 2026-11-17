"""File links expose only explicitly registered bytes, through the authenticated server."""

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.contracts.files import MAX_FILE_BYTES, FileFailure
from app.contracts.protocol import FileRegister, WorkerMessage, call_header, message_header
from app.controler.api import create_app
from app.controler.workers.callbacks import ServerCallbacks
from app.controler.workers.transport import serve_worker
from app.worker.config import WorkerSettings
from app.worker.execution.amp_agent import AmpAttachment, AmpCodingAgent, AmpProcessResult
from app.worker.execution.codex import CodexAgent
from app.worker.execution.lifecycle import WorkerProtocolError
from app.worker.runtime import WorkerRuntime
from app.worker.state.file_links import FileLinks
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
        method = "register" if operation == "register" else "lookup"
        original = getattr(target, method)

        def slow(*args):
            blocked.set()
            assert release.wait(10)
            return original(*args)

        monkeypatch.setattr(target, method, slow)
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
        CodexAgent().register_file(path, FileLinks(tmp_path / "worker"))
    assert str(path) not in str(error.value)


@pytest.mark.parametrize("agent_type", [CodexAgent, AmpCodingAgent])
async def test_links_reject_file_substitution_and_growth(tmp_path, agent_type):
    # Workspaces are reused: an old URL must not reveal the next Flow's replacement file.
    path = tmp_path / "file"
    path.write_bytes(b"original")
    root = tmp_path / "worker"
    links = FileLinks(root)
    agent = agent_type(trusted_host=False)
    handle = agent.register_file(path, links)
    other = AmpCodingAgent() if agent_type is CodexAgent else CodexAgent()
    assert other.register_file(path, links) == handle
    owner, source = FileLinks(root).lookup(handle)
    assert owner == agent.backend
    restarted = WorkerRuntime(WorkerSettings(
        worker_id="worker", worker_token=SecretStr("secret"), worker_root=root,
        serve_ws_url="wss://unused", coding_agent=other.backend,
    ))
    selected, persisted = await restarted.resolve_file(handle)
    assert selected is restarted._agents[agent.backend]
    assert await selected.read_file(persisted, root=root) == b"original"
    path.rename(tmp_path / "original")
    path.write_bytes(b"another Flow")
    with pytest.raises(FileFailure, match="file_changed"):
        await agent.read_file(source, root=root)
    path.unlink()
    path.symlink_to(tmp_path / "original")
    with pytest.raises(FileFailure):
        await agent.read_file(source, root=root)
    path.unlink()
    (tmp_path / "original").rename(path)
    path.write_bytes(b"x" * MAX_FILE_BYTES)
    assert await agent.read_file(source, root=root) == b"x" * MAX_FILE_BYTES
    with path.open("ab") as stream:
        stream.write(b"x")
    with pytest.raises(FileFailure, match="file_too_large"):
        await agent.read_file(source, root=root)


AMP_ATTACHMENT = "https://ampcode.com/user-content/attachments/" + "b" * 64 + "-screen.png"


@pytest.mark.parametrize("url", [
    AMP_ATTACHMENT.replace("https:", "http:"),
    AMP_ATTACHMENT.replace("ampcode.com/", "ampcode.com.evil.test/"),
    AMP_ATTACHMENT.replace("ampcode.com/", "evil.test@ampcode.com/"),
    AMP_ATTACHMENT.replace("ampcode.com/", "ampcode.com:443/"),
    AMP_ATTACHMENT + "?download=1",
    AMP_ATTACHMENT.replace("screen.png", "%2fprivate.png"),
    AMP_ATTACHMENT.replace("screen.png", "%5cprivate.png"),
    AMP_ATTACHMENT.replace("screen.png", "../private.png"),
    AMP_ATTACHMENT.replace("screen.png", "%2e%2e"),
    AMP_ATTACHMENT.replace("screen.png", "%00.png"),
    AMP_ATTACHMENT.replace("screen.png", "%FF.png"),
])
def test_amp_attachment_rejects_other_authorities_and_paths(url, tmp_path):
    with pytest.raises(ValidationError):
        AmpAttachment(url=url)
    with pytest.raises(FileFailure, match="file_unreadable"):
        AmpCodingAgent().register_file(url, FileLinks(tmp_path))


@pytest.mark.parametrize("legacy", [False, True])
async def test_amp_download_is_lazy_private_ephemeral_and_restartable(server_runtime, tmp_path, monkeypatch, legacy):
    # Remote registrations survive restart alongside old local records, without storing image bytes.
    root = tmp_path / "worker"
    local = tmp_path / "local.txt"
    local.write_bytes(b"local")
    if legacy:
        # Real pre-ownership records, rather than records created by the new registration API.
        root.mkdir()
        metadata = local.stat()
        (root / "file-links.json").write_text(json.dumps({
            "a" * 32: {"path": str(local), "device": metadata.st_dev, "inode": metadata.st_ino},
            "c" * 32: {"url": AMP_ATTACHMENT},
        }))
    content = b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_FILE_BYTES - 8)
    downloads = []

    async def download(args, **kwargs):
        assert args[:3] == ["files", "get", AMP_ATTACHMENT]
        assert kwargs["max_file_bytes"] == MAX_FILE_BYTES + 1
        target = Path(args[args.index("-o") + 1])
        assert target.parent.stat().st_mode & 0o777 == 0o700
        downloads.append(target)
        target.write_bytes(content)
        return AmpProcessResult(b"", b"", 0)

    monkeypatch.setattr(AmpCodingAgent, "run_process", staticmethod(download))
    source = AmpAttachment(url=AMP_ATTACHMENT + "#amp-media-source=file%3A%2F%2F%2Fprivate")
    async with connected_files(server_runtime, root) as (worker, _, http, frames):
        assert worker.settings.coding_agent == "codex" and not worker.settings.trusted_host_execution
        local_link = await worker.register_file(local)
        link = await worker.register_file(source.url, backend="amp")
        assert await worker.register_file(AMP_ATTACHMENT, backend="amp") == link
        handle = worker._agents["amp"].register_file(source.url, worker.files)
        agent, _ = await worker.resolve_file(handle)
        assert agent is worker._agents["amp"]
        if legacy:
            assert handle == "c" * 32
            assert agent.register_file(local, worker.files) == "a" * 32
        assert downloads == []
        assert (await http.get(link, headers={"cf-access-jwt-assertion": "invalid"})).status_code == 401
        assert downloads == []
        response = await http.get(link)
        assert response.status_code == 200 and response.content == content
        assert len(downloads) == 1 and not downloads[-1].parent.exists()
        assert "no-store" in response.headers["cache-control"]
        assert "sandbox" in response.headers["content-security-policy"]
        assert AMP_ATTACHMENT not in "".join(frames)
        assert (await http.get(local_link)).content == b"local"
    async with connected_files(server_runtime, root) as (worker, _, http, _):
        assert await worker.register_file(source.url, backend="amp") == link
        assert (await http.get(link)).content == content
        assert len(downloads) == 2 and not downloads[-1].parent.exists()
        assert (await http.get(local_link)).content == b"local"


@pytest.mark.parametrize(("outcome", "status", "error"), [
    ("large", 413, "file_too_large"),
    ("auth", 502, "file_unreadable"),
    ("missing", 502, "file_unreadable"),
    ("timeout", 504, "query_timeout"),
])
async def test_amp_download_failure_is_bounded_retryable_and_cleaned(
    server_runtime, tmp_path, monkeypatch, outcome, status, error,
):
    directories = []

    async def download(args, **kwargs):
        target = Path(args[args.index("-o") + 1])
        directories.append(target.parent)
        if outcome == "large":
            # The CLI can hit its quota on a staging file before the final rename.
            stage = target.parent / ".download" / "partial"
            stage.parent.mkdir()
            stage.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        elif outcome == "timeout":
            target.write_bytes(b"partial")
        elif outcome == "ok":
            target.write_bytes(b"retry succeeded")
        return AmpProcessResult(b"", b"private error", int(outcome in {"large", "auth"}),
                                timed_out=outcome == "timeout")

    monkeypatch.setattr(AmpCodingAgent, "run_process", staticmethod(download))
    async with connected_files(server_runtime, tmp_path / "worker") as (worker, _, http, _):
        link = await worker.register_file(AMP_ATTACHMENT, backend="amp")
        response = await http.get(link)
        assert response.status_code == status and response.json() == {"detail": error}
        assert not directories[-1].exists()
        outcome = "ok"
        response = await http.get(link)
        assert response.status_code == 200 and response.content == b"retry succeeded"
        assert not directories[-1].exists()


async def test_worker_disconnect_cancels_amp_download_and_removes_partial_copy(
    server_runtime, tmp_path, monkeypatch,
):
    started, stopped = asyncio.Event(), asyncio.Event()
    directories = []

    async def download(args, **kwargs):
        target = Path(args[args.index("-o") + 1])
        target.write_bytes(b"partial")
        directories.append(target.parent)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(AmpCodingAgent, "run_process", staticmethod(download))
    async with connected_files(server_runtime, tmp_path / "worker") as (worker, _, http, _):
        link = await worker.register_file(AMP_ATTACHMENT, backend="amp")
        pending = asyncio.create_task(http.get(link))
        await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(stopped.wait(), 2)
    assert not directories[-1].exists()
    assert (await pending).status_code == 503
