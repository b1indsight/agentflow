"""Unified job delivery keeps inference cancellation independent from coding."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from app.contracts.protocol import (
    Assignment,
    ExecutionIdentity,
    Heartbeat,
    InferenceRequest,
    InferenceResult,
    ProtocolError,
    Register,
    Registered,
    Started,
    WorkerMessage,
    call_header,
    message_header,
    parse_server_message,
    result_header,
)
from app.controler.workers.transport import serve_worker
from app.controler.workflow.inference import InferencePending, JobInference
from app.domain.inference_models import (
    LOCAL_BUB_WORKER_ID,
    InferenceCapability,
    InferenceMessage,
    InferenceOutput,
    InferenceTask,
)
from app.domain.models import JobKind, JobState, utcnow
from app.inference.bub import ADAPTER_VERSION
from app.inference.local import serve_local_inference
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.state.execution import ExecutionState
from tests.test_remote_asgi import AsgiRuntime
from tests.test_remote_e2e import QueueConnection, QueueSocket
from tests.test_worker_client import RecordingExecutor
from tests.worker_fakes import run_worker, worker_runtime


def request():
    return InferenceTask(messages=[InferenceMessage(role="user", content="Summarize supplied facts")],
                         timeout_seconds=10)


async def settled(store, job, state):
    async with asyncio.timeout(3):
        while store.jobs.get_job(job.id).state != state:
            await asyncio.sleep(.01)


@asynccontextmanager
async def connected(server, worker):
    outgoing, incoming = asyncio.Queue(), asyncio.Queue()
    serving = asyncio.create_task(serve_worker(QueueSocket(outgoing, incoming),
        worker_tokens={"worker-1": "worker-secret"}, serve=server.serve_worker_session))
    working = asyncio.create_task(run_worker(worker, QueueConnection(outgoing, incoming)))
    try:
        yield working, serving
    finally:
        working.cancel()
        serving.cancel()
        await asyncio.gather(working, serving, return_exceptions=True)
        await worker.execution.shutdown()


async def test_inference_cleanup_keeps_its_slot_without_blocking_coding(store, tmp_path):
    coding_entered, coding_release = asyncio.Event(), asyncio.Event()
    infer_entered, infer_release, infer_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    infer_cleaning, cleanup_release = asyncio.Event(), asyncio.Event()

    class Coding(RecordingExecutor):
        async def execute(self, task, secret=None, *, tool_caller=None):
            coding_entered.set()
            await coding_release.wait()
            return await super().execute(task, secret, tool_caller=tool_caller)

    class Inference:
        def capability(self):
            return InferenceCapability(adapter_version="codex/v1")

        async def infer(self, task):
            infer_entered.set()
            try:
                await infer_release.wait()
                return InferenceOutput(request_id=task.request_id, request_digest=task.digest,
                                       text="Summary", adapter_version="codex/v1")
            finally:
                infer_cleaning.set()
                await cleanup_release.wait()
                infer_closed.set()

    flow = store.flows.create_flow("owner/repo", 8, 42)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    coding = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    server = AsgiRuntime(store, attempt.id)
    execution = WorkerExecution(worker_id="worker-1", executor=Coding(), state=ExecutionState(tmp_path))
    worker = worker_runtime(worker_id="worker-1", worker_token="worker-secret",
                            serve_ws_url="ws://test", execution=execution, read_history=Mock())
    worker.inference = Inference()
    async with connected(server, worker) as (working, serving):
        try:
            await asyncio.wait_for(coding_entered.wait(), 3)
            inference = store.jobs.enqueue_inference(request())
            server.gateway.wake_all()
            await asyncio.wait_for(infer_entered.wait(), 3)
            clock = utcnow() + timedelta(seconds=20)
            store.database._clock = lambda: clock
            server.gateway.wake_all()
            await asyncio.wait_for(infer_cleaning.wait(), 3)
            # Slow provider cleanup must not block coding results on the same session.
            coding_release.set()
            await settled(store, coding, JobState.DONE)
            assert store.jobs.get_job(inference.id).state is JobState.RUNNING
            assert store.workers.list_workers()[0].current_inference_job_id == inference.id
            assert not infer_closed.is_set()
            cleanup_release.set()
            await settled(store, inference, JobState.FAILED)
            assert infer_closed.is_set()
            assert store.workers.list_workers()[0].current_inference_job_id is None
            assert not working.done() and not serving.done()
        finally:
            cleanup_release.set()
            coding_release.set()


async def test_local_mini_and_repo_free_worker_share_dispatch_and_results(store, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Local:
        async def infer(self, task):
            entered.set()
            await release.wait()
            return InferenceOutput(request_id=task.request_id, request_digest=task.digest,
                                   text="Local answer", adapter_version=ADAPTER_VERSION)

    class Remote:
        def capability(self):
            return InferenceCapability(adapter_version="codex/v1")

        async def infer(self, task):
            return InferenceOutput(request_id=task.request_id, request_digest=task.digest,
                                   text="Remote answer", adapter_version="codex/v1")

    server = AsgiRuntime(store, 1)
    execution = WorkerExecution(worker_id="worker-1", executor=RecordingExecutor(), state=ExecutionState(tmp_path))
    worker = worker_runtime(worker_id="worker-1", worker_token="worker-secret",
                            serve_ws_url="ws://test", execution=execution, read_history=Mock())
    worker.inference = Remote()
    mini = asyncio.create_task(serve_local_inference(server.serve_worker_session, Local()))
    caller = JobInference(store.database, server.gateway.wake_all)
    local_task, remote_task = request(), request()
    try:
        async with connected(server, worker):
            async with asyncio.timeout(3):
                while len([w for w in store.workers.list_workers() if w.inference is not None]) != 2:
                    await asyncio.sleep(.01)
            with pytest.raises(InferencePending) as pending:
                await caller.infer(local_task)
            local = store.jobs.get_job(pending.value.job_id)
            await asyncio.wait_for(entered.wait(), 3)
            assert store.jobs.get_job(local.id).assigned_worker_id == LOCAL_BUB_WORKER_ID
            with pytest.raises(InferencePending) as pending:
                await caller.infer(remote_task)
            remote = store.jobs.get_job(pending.value.job_id)
            await settled(store, remote, JobState.DONE)
            assert store.jobs.get_job(remote.id).assigned_worker_id == "worker-1"
            release.set()
            await settled(store, local, JobState.DONE)
            assert local.flow_id is None and remote.flow_id is None
            with store.database.connection() as connection:
                assert connection.execute("SELECT count(*) AS n FROM flow_executions").fetchone()["n"] == 0
            assert not next(w for w in store.workers.list_workers() if w.worker_id == LOCAL_BUB_WORKER_ID).execute
    finally:
        mini.cancel()
        await asyncio.gather(mini, return_exceptions=True)
    # A recreated caller reads saved answers with both workers gone, without a new attempt.
    recovered = JobInference(store.database, server.gateway.wake_all)
    assert (await recovered.infer(local_task)).text == "Local answer"
    assert (await recovered.infer(remote_task)).text == "Remote answer"
    assert store.jobs.get_job(local.id).attempt == store.jobs.get_job(remote.id).attempt == 1


async def test_network_worker_cannot_claim_local_priority_even_with_a_configured_token():
    socket = Mock(headers={"x-flowtool-worker-id": LOCAL_BUB_WORKER_ID,
                           "authorization": "Bearer secret"}, close=AsyncMock(), accept=AsyncMock())
    serve = AsyncMock()
    await serve_worker(socket, worker_tokens={LOCAL_BUB_WORKER_ID: "secret"}, serve=serve)
    socket.close.assert_awaited_once_with(code=1008)
    assert not socket.accept.called and not serve.called


@pytest.mark.parametrize("wrong_token", [False, True])
async def test_completed_inference_heartbeat_skips_renewal_but_dispatches_payload(
    store, wrong_token,
):
    # A queued heartbeat can follow its result, even after another slot starts work.
    outgoing, incoming = asyncio.Queue(), asyncio.Queue()
    flow = store.flows.create_flow("owner/repo", 8, 42)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    server = AsgiRuntime(store, attempt.id)
    serving = asyncio.create_task(serve_worker(QueueSocket(outgoing, incoming),
        worker_tokens={"worker-1": "worker-secret"}, serve=server.serve_worker_session))

    async def next_call(model):
        async with asyncio.timeout(3):
            while True:
                message = parse_server_message(await incoming.get())
                assert not isinstance(message, ProtocolError)
                for item in message.payload:
                    if isinstance(item, model):
                        return item

    def answer(call):
        return InferenceResult(**result_header(call.message_id), method="worker.infer",
            job_id=call.job_id, assignment_token=call.assignment_token,
            output=InferenceOutput(request_id=call.arguments.request_id,
                request_digest=call.arguments.digest, text="Answer", adapter_version="codex/v1"))

    await outgoing.put(Register(**message_header(None), control="register", payload=[],
        worker_id="worker-1", recovery=None,
        inference=InferenceCapability(adapter_version="codex/v1")).model_dump_json())
    try:
        async with asyncio.timeout(3):
            while not isinstance(registered := parse_server_message(await incoming.get()), Registered):
                pass
        epoch = registered.connection_epoch
        first = store.jobs.enqueue_inference(request())
        server.gateway.wake_all()
        first_call = await next_call(InferenceRequest)
        await outgoing.put(WorkerMessage(**message_header(epoch), control=None,
                                         payload=[answer(first_call)]).model_dump_json())
        await settled(store, first, JobState.DONE)

        coding = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
        server.gateway.wake_all()
        call = await next_call(Assignment)
        await outgoing.put(WorkerMessage(**message_header(epoch), control=None, payload=[
            Started(**call_header(), method="server.execution_started",
                    arguments=ExecutionIdentity(job_id=coding.id,
                        assignment_token=call.arguments.assignment_token)),
        ]).model_dump_json())
        async with asyncio.timeout(3):
            while store.jobs.get_job(coding.id).started_at is None:
                await asyncio.sleep(.01)

        second = store.jobs.enqueue_inference(request())
        server.gateway.wake_all()
        second_call = await next_call(InferenceRequest)
        saved_first = store.jobs.get_job(first.id)
        saved_coding = store.jobs.get_job(coding.id)
        heartbeat = Heartbeat(**message_header(epoch),
            control="heartbeat", job_id=first.id,
            assignment_token="wrong-token" if wrong_token else first_call.assignment_token,
            payload=[answer(second_call)])
        await outgoing.put(heartbeat.model_dump_json())
        if wrong_token:
            await asyncio.wait_for(asyncio.shield(serving), 3)
            assert store.jobs.get_job(second.id).state is JobState.QUEUED
            assert store.workers.list_workers()[0].last_disconnect_reason is not None
        else:
            await settled(store, second, JobState.DONE)
            assert not serving.done()
            assert store.jobs.get_job(first.id) == saved_first
            assert store.jobs.get_job(coding.id) == saved_coding
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)


async def test_slow_coding_preparation_keeps_inference_live(store, tmp_path, monkeypatch):
    # GitHub preparation must not hold an idle inference slot or its incoming results.
    preparing, prepare_release = asyncio.Event(), asyncio.Event()
    inferring, infer_release = asyncio.Event(), asyncio.Event()
    flow = store.flows.create_flow("owner/repo", 8, 42)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    coding = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    server = AsgiRuntime(store, attempt.id)
    server.settings.worker_heartbeat_interval = .02
    prepare = server.coordinator.prepare_for_assignment

    async def delayed_prepare(*args):
        preparing.set()
        await prepare_release.wait()
        return await prepare(*args)

    prepare_call = AsyncMock(side_effect=delayed_prepare)
    monkeypatch.setattr(server.coordinator, "prepare_for_assignment", prepare_call)

    class Inference:
        def capability(self):
            return InferenceCapability(adapter_version="codex/v1")

        async def infer(self, task):
            inferring.set()
            await infer_release.wait()
            return InferenceOutput(request_id=task.request_id, request_digest=task.digest,
                                   text="Answer during preparation", adapter_version="codex/v1")

    execution = WorkerExecution(worker_id="worker-1", executor=RecordingExecutor(), state=ExecutionState(tmp_path))
    worker = worker_runtime(worker_id="worker-1", worker_token="worker-secret",
                            serve_ws_url="ws://test", execution=execution, read_history=Mock())
    worker.inference = Inference()
    async with connected(server, worker) as (working, serving):
        await asyncio.wait_for(preparing.wait(), 3)
        first_coding_heartbeat = store.jobs.get_job(coding.id).heartbeat_at
        inference = store.jobs.enqueue_inference(request())
        server.gateway.wake_all()
        await asyncio.wait_for(inferring.wait(), 3)
        first_inference_heartbeat = store.jobs.get_job(inference.id).heartbeat_at
        async with asyncio.timeout(3):
            while (store.jobs.get_job(coding.id).heartbeat_at <= first_coding_heartbeat
                   or store.jobs.get_job(inference.id).heartbeat_at <= first_inference_heartbeat):
                await asyncio.sleep(.01)
        infer_release.set()
        await settled(store, inference, JobState.DONE)
        assert not prepare_release.is_set()
        assert store.jobs.get_job(coding.id).dispatch_authorized_at is None
        assert store.jobs.get_job(coding.id).started_at is None
        prepare_call.assert_awaited_once()
        assert not working.done() and not serving.done()

        prepare_release.set()
        await settled(store, coding, JobState.DONE)
        prepare_call.assert_awaited_once()
