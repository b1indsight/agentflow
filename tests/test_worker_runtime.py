import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.contracts.conversation import HistoryPage, HistoryQuery
from app.contracts.protocol import (
    ExecutionIdentity,
    FlowSearchCall,
    FlowSearchPage,
    FlowSearchQuery,
    FlowSearchResult,
    InferenceRequest,
    InferenceResult,
    Register,
    ServerMessage,
    StopFence,
    call_header,
    message_header,
    parse_worker_message,
    result_header,
)
from app.domain.inference_models import InferenceError, InferenceMessage, InferenceTask
from app.domain.models import CodingExecutionResult
from app.worker.config import WorkerSettings
from app.worker.execution.amp_agent import AmpCodingAgent
from app.worker.execution.codex import CodexAgent
from app.worker.execution.lifecycle import WorkerProtocolError
from app.worker.execution.permissions import FULL_ACCESS
from app.worker.execution.vcs import _run
from app.worker.runtime import WorkerRootLock, WorkerRuntime
from tests.test_remote_e2e import QueueConnection
from tests.test_worker_client import (
    CancellableExecutor,
    ScriptedConnection,
    assignment_message,
    registered_message,
)
from tests.test_worker_session import _assignment
from tests.worker_fixtures import run_worker


async def test_embedded_worker_leaves_process_setup_to_the_host(tmp_path, caplog):
    # A host may run the worker loop off the main thread, where signals cannot be installed.
    async def serve():
        runtime = WorkerRuntime(WorkerSettings(
            worker_id="embedded", worker_token=SecretStr("secret"), worker_root=tmp_path,
            serve_ws_url="wss://unused",
        ))
        asyncio.get_running_loop().call_soon(runtime.stop)
        await runtime.run()

    await asyncio.to_thread(lambda: asyncio.run(serve()))
    assert (tmp_path / ".worker-id").read_text().strip() == "embedded"
    logging.getLogger("host").warning("worker stopped")
    assert "worker stopped" in caplog.messages


def test_worker_root_lock_prevents_a_second_process_root_owner(tmp_path: Path) -> None:
    first = WorkerRootLock(tmp_path)
    second = WorkerRootLock(tmp_path)

    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_distinct_worker_roots_record_identity_and_lock_independently(tmp_path: Path) -> None:
    first_root = tmp_path / "worker-a"
    second_root = tmp_path / "worker-b"
    first = WorkerRootLock(first_root, "worker-a")
    second = WorkerRootLock(second_root, "worker-b")

    first.acquire()
    second.acquire()
    try:
        assert (first_root / ".worker-id").read_text() == "worker-a\n"
        assert (second_root / ".worker-id").read_text() == "worker-b\n"
    finally:
        first.release()
        second.release()


def test_worker_root_rejects_sequential_identity_reuse(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    first = WorkerRootLock(root, "worker-a")
    first.acquire()
    first.release()

    with pytest.raises(RuntimeError, match="belongs to worker-a"):
        WorkerRootLock(root, "worker-b").acquire()


@pytest.mark.parametrize("backend,expected,timeout", [(None, CodexAgent, 23), ("amp", AmpCodingAgent, 47)])
@pytest.mark.parametrize("trusted_host", [False, True])
async def test_runtime_shares_configured_agent_for_coding_and_history(
    tmp_path, monkeypatch, backend, expected, timeout, trusted_host,
):
    # Sharing must include history requests and multiple Flows, without changing coding authorization.
    if backend is None:
        monkeypatch.delenv("AGENTFLOW_CODING_AGENT", raising=False)
    else:
        monkeypatch.setenv("AGENTFLOW_CODING_AGENT", backend)
    settings = WorkerSettings(worker_id="worker", worker_token=SecretStr("secret"),
                              worker_root=tmp_path, serve_ws_url="wss://control.example/connect",
                              codex_run_timeout_seconds=23, amp_run_timeout_seconds=47,
                              trusted_host_execution=trusted_host, execution_isolation="container")
    runtime = WorkerRuntime(settings)
    execution = runtime._create_execution(3)
    other = runtime._create_execution(4)
    agent = execution.executor.code_agent
    assert runtime.executions == {}
    assert execution.state.flow_id == 3
    assert execution.executor.workspaces is runtime.workspaces
    assert isinstance(agent, expected)
    assert agent.timeout == timeout
    assert agent.trusted_host is trusted_host
    assert agent.permissions is FULL_ACCESS
    assert agent.max_output_bytes == 8 * 1024 * 1024

    session_id = ("T-" if backend == "amp" else "") + "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    page = HistoryPage(session_id=session_id, status="unsupported")
    monkeypatch.setattr(expected, "read_history", lambda self, query: HistoryPage(
        session_id=query.session_id, status="not_found",
    ))
    monkeypatch.setattr(agent, "read_history", lambda query: page)
    assert await runtime.read_history(HistoryQuery(session_id=session_id)) == page
    assert other.executor.code_agent is agent


@pytest.mark.parametrize("outcome", ["page", "disconnect", "stop"])
async def test_search_belongs_to_one_live_assignment_and_connection(tmp_path, outcome):
    # Unlike delivery, a disconnected read must fail, and a reply after stop grants no data.
    runtime = WorkerRuntime(WorkerSettings(worker_id="w", worker_token=SecretStr("secret"),
                            worker_root=tmp_path, serve_ws_url="wss://unused"))
    runtime.initialize()
    slots = await runtime.workspaces.provision("owner/repo")
    assignment = _assignment(workspace=next(s.workspace for s in slots if s.repository))
    execution = runtime.receive(assignment)
    executor = CancellableExecutor()
    execution.executor = executor
    query = FlowSearchQuery(q="status:blocked", limit=2, cursor=None)
    with pytest.raises(WorkerProtocolError, match="active"):
        await execution.search_flows(query)

    outgoing, incoming = asyncio.Queue(), asyncio.Queue()
    serving = asyncio.create_task(run_worker(runtime, QueueConnection(outgoing, incoming)))
    try:
        async with asyncio.timeout(3):
            await incoming.put(registered_message().model_dump_json())
            await incoming.put(assignment_message(assignment).model_dump_json())
            await executor.started.wait()
            search = asyncio.create_task(execution.search_flows(query))
            while True:
                frame = parse_worker_message(await outgoing.get())
                call = next((i for i in frame.payload if isinstance(i, FlowSearchCall)), None)
                if call is not None:
                    break
            identity = ExecutionIdentity(job_id=assignment.job_id,
                                         assignment_token=assignment.assignment_token)
            assert call.arguments == identity and call.query == query
            assert frame.connection_epoch == "epoch-1" and execution.pending_tool_call() is None
            if outcome == "disconnect":
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)
                with pytest.raises(WorkerProtocolError, match="unavailable"):
                    await search
                assert execution.lease() == assignment
                with pytest.raises(WorkerProtocolError, match="unavailable"):
                    await execution.search_flows(query)
                return

            if outcome == "stop":
                await execution.stop(StopFence(request_id="stop", flow_id=assignment.flow_id,
                                               **identity.model_dump()))
            page = FlowSearchPage(items=[], counts={}, next_cursor=None, has_more=False)
            reply = FlowSearchResult(**result_header(call.message_id), method=call.method,
                                     page=page, error=None)
            await incoming.put(ServerMessage(**message_header("epoch-1"), control=None,
                                             payload=[reply]).model_dump_json())
            if outcome == "stop":
                with pytest.raises(WorkerProtocolError, match="assignment"):
                    await search
            else:
                assert await search == page
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        await execution.shutdown()


@pytest.mark.parametrize("backend,expected", [("codex", CodexAgent), ("amp", AmpCodingAgent)])
async def test_runtime_inference_uses_shared_agent_with_call_local_configuration(
    tmp_path, monkeypatch, backend, expected,
):
    # Inference and coding can overlap: inference must never reconfigure their shared agent.
    runtime = WorkerRuntime(WorkerSettings(
        worker_id="worker", worker_token=SecretStr("secret"), worker_root=tmp_path,
        serve_ws_url="wss://control.example/connect", coding_agent=backend,
        trusted_host_execution=True, execution_isolation="container",
    ))
    runtime.initialize()
    agent = runtime._agents[backend]
    original_fields = vars(agent).copy()
    assert runtime._create_execution(3).executor.code_agent is agent
    messages = [
        {"role": "system", "content": "Summarize only the supplied facts"},
        {"role": "user", "content": 'Facts with quotes: "hello" and 中文'},
    ]
    calls = []

    async def start(self, prompt, cwd, *, output_schema=None, context=None, model=None,
                    effort=None, max_output_bytes=None, timeout_seconds=None):
        assert self is agent and cwd == runtime.workspaces.inference_path and cwd.is_dir()
        assert context is None and model is None and effort is None
        assert [message.model_dump() for message in prompt] == messages
        calls.append((output_schema, max_output_bytes, timeout_seconds))
        return CodingExecutionResult("answer-session", '"Answer"', 0, model="observed-model")

    monkeypatch.setattr(expected, "start", start)
    outgoing, incoming = asyncio.Queue(), asyncio.Queue()
    serving = asyncio.create_task(run_worker(runtime, QueueConnection(outgoing, incoming)))
    try:
        registration = parse_worker_message(await asyncio.wait_for(outgoing.get(), 2))
        assert isinstance(registration, Register)
        assert registration.inference.adapter_version == f"{backend}/v1"
        await incoming.put(registered_message().model_dump_json())
        task = InferenceTask(messages=[InferenceMessage(**message) for message in messages],
                             output_schema={"type": "string"}, max_output_bytes=64, timeout_seconds=5)
        call = InferenceRequest(**call_header(task.request_id), method="worker.infer", job_id=1,
                                assignment_token="token", arguments=task)
        await incoming.put(ServerMessage(**message_header("epoch-1"), control=None,
                                         payload=[call]).model_dump_json())
        async with asyncio.timeout(2):
            while True:
                message = parse_worker_message(await outgoing.get())
                output = next((item for item in message.payload if isinstance(item, InferenceResult)), None)
                if output is not None:
                    break
        assert output.reply_to == call.message_id and output.assignment_token == "token"
        assert output.output.adapter_version == f"{backend}/v1" and output.output.model == "observed-model"
        output.output.validate_for(task)
        assert calls == [({"type": "string"}, 64, 5)]
        assert vars(agent) == original_fields
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)


async def test_inference_requires_trusted_host_opt_in(tmp_path):
    runtime = WorkerRuntime(WorkerSettings(
        worker_id="worker", worker_token=SecretStr("secret"), worker_root=tmp_path,
        serve_ws_url="wss://unused",
    ))
    assert runtime.inference_capability() is None
    with pytest.raises(InferenceError) as rejected:
        await runtime.infer(InferenceTask(messages=[InferenceMessage(role="user", content="Answer")]))
    assert rejected.value.failure.code == "configuration"


@pytest.mark.parametrize("kind,exit_code,expected", [
    ("timeout", 124, "execution_timeout"),  # The only renamed code.
    ("invalid_response", 1, "invalid_response"),  # Other agent failures keep their code.
    (None, 7, "backend_error"),  # An untyped nonzero exit is still a failure.
])
async def test_runtime_translates_agent_failures_to_external_inference_errors(
    tmp_path, monkeypatch, kind, exit_code, expected,
):
    runtime = WorkerRuntime(WorkerSettings(
        worker_id="worker", worker_token=SecretStr("secret"), worker_root=tmp_path,
        serve_ws_url="wss://unused", trusted_host_execution=True, execution_isolation="container",
    ))
    runtime.initialize()

    async def run(*args, **kwargs):
        return CodingExecutionResult(None, "", exit_code, failure_kind=kind,
                                     failure_detail="private backend diagnostics")

    monkeypatch.setattr(runtime._agents["codex"], "_run", run)
    with pytest.raises(InferenceError) as failed:
        await runtime.infer(InferenceTask(messages=[InferenceMessage(role="user", content="Answer")]))
    assert failed.value.failure.code == expected
    assert failed.value.backend == "codex/v1"
    assert "private" not in str(failed.value)


@pytest.mark.parametrize("shutdown", ["stop", "cancel"])
async def test_runtime_keeps_root_locked_until_owned_process_cleanup_finishes(tmp_path, monkeypatch, shutdown):
    # Process shutdown must survive repeated caller cancellation, without inventing a stop receipt.
    cleaning, release = asyncio.Event(), asyncio.Event()
    ready = tmp_path / "pids"
    script = ("import os,subprocess,sys,time; from pathlib import Path; "
              "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
              "Path(sys.argv[1]).write_text(str(os.getpid())+' '+str(child.pid)); time.sleep(60)")

    class Executor:
        async def execute(self, task, secret=None, *, tool_caller):
            process = asyncio.create_task(_run(sys.executable, "-c", script, str(ready)))
            try:
                await asyncio.shield(process)
            finally:
                cleaning.set()
                await release.wait()
                process.cancel()
                await asyncio.gather(process, return_exceptions=True)

    @asynccontextmanager
    async def connect(*args, **kwargs):
        inventory = await runtime.workspaces.provision("owner/repo")
        slot = next(item for item in inventory if item.repository == "owner/repo")
        connection = ScriptedConnection()
        connection.messages[1] = assignment_message(_assignment(
            workspace=slot.workspace, generation=slot.generation,
        )).model_dump_json()
        yield connection

    monkeypatch.setattr("app.worker.runtime.connect", connect)
    runtime = WorkerRuntime(WorkerSettings(worker_id="worker", worker_token=SecretStr("secret"),
        worker_root=tmp_path, serve_ws_url="wss://control.example/connect"))
    create_execution = runtime._create_execution

    def owned_execution(flow_id):
        execution = create_execution(flow_id)
        execution.executor = Executor()
        return execution

    monkeypatch.setattr(runtime, "_create_execution", owned_execution)
    running = asyncio.create_task(runtime.run())
    second = WorkerRootLock(tmp_path)
    try:
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(0.01)
        pids = list(map(int, ready.read_text().split()))
        if shutdown == "stop":
            runtime.stop()
        else:
            running.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        for _ in range(2):
            running.cancel()
            await asyncio.sleep(0)
        assert not running.done()
        with pytest.raises(RuntimeError, match="already in use"):
            second.acquire()
        for pid in pids:
            os.kill(pid, 0)
        release.set()
        if shutdown == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 5)
        else:
            await asyncio.wait_for(running, 5)
        async with asyncio.timeout(5):
            for pid in pids:
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.01)
        assert set(runtime.executions) == {3}
        record = runtime.executions[3].state.load()
        assert record.state == "started" and record.stopped is None and record.terminal is None
        assert not record.acknowledged
        second.acquire()
    finally:
        second.release()
        release.set()
        runtime.stop()
        await asyncio.gather(running, return_exceptions=True)
