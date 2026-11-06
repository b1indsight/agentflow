"""Unified job delivery keeps inference cancellation independent from coding."""

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from app.contracts.protocol import (
    Heartbeat,
    InferenceRequest,
    InferenceResult,
    Register,
    Registered,
    WorkerMessage,
    message_header,
    parse_server_message,
    result_header,
)
from app.controler.workers.transport import serve_worker
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


async def test_inference_cancellation_and_results_do_not_interrupt_coding(store, tmp_path):
    coding_entered, coding_release = asyncio.Event(), asyncio.Event()
    infer_entered, infer_release, infer_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

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
    outgoing, incoming = asyncio.Queue(), asyncio.Queue()
    serving = asyncio.create_task(serve_worker(QueueSocket(outgoing, incoming),
        worker_tokens={"worker-1": "worker-secret"}, serve=server.serve_worker_session))
    working = asyncio.create_task(run_worker(worker, QueueConnection(outgoing, incoming)))
    try:
        await asyncio.wait_for(coding_entered.wait(), 3)
        first = store.jobs.enqueue_inference(request())
        server.gateway.wake_all()
        await asyncio.wait_for(infer_entered.wait(), 3)
        assert store.jobs.get_job(coding.id).state is JobState.RUNNING
        clock = utcnow() + timedelta(seconds=20)
        store.database._clock = lambda: clock
        server.gateway.wake_all()
        await settled(store, first, JobState.FAILED)
        assert infer_closed.is_set()  # Cleanup precedes release of the durable inference slot.
        assert store.jobs.get_job(coding.id).state is JobState.RUNNING
        assert not working.done() and not serving.done()

        infer_entered.clear()
        infer_closed.clear()
        second = store.jobs.enqueue_inference(request())
        server.gateway.wake_all()
        await asyncio.wait_for(infer_entered.wait(), 3)
        coding_release.set()
        await settled(store, coding, JobState.DONE)
        assert store.jobs.get_job(second.id).state is JobState.RUNNING
        infer_release.set()
        await settled(store, second, JobState.DONE)
        assert store.jobs.get_job(second.id).result_payload["output"]["text"] == "Summary"

        infer_entered.clear()
        infer_release.clear()
        infer_closed.clear()
        third = store.jobs.enqueue_inference(request())
        server.gateway.wake_all()
        await asyncio.wait_for(infer_entered.wait(), 3)
        working.cancel()
        await asyncio.gather(working, return_exceptions=True)
        assert infer_closed.is_set()
        assert store.jobs.get_job(third.id).attempt == 1
    finally:
        working.cancel()
        serving.cancel()
        await asyncio.gather(working, serving, return_exceptions=True)
        await execution.shutdown()


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
    outgoing, incoming = asyncio.Queue(), asyncio.Queue()
    tasks = [asyncio.create_task(serve_local_inference(server.serve_worker_session, Local())),
             asyncio.create_task(serve_worker(QueueSocket(outgoing, incoming),
                worker_tokens={"worker-1": "worker-secret"}, serve=server.serve_worker_session)),
             asyncio.create_task(run_worker(worker, QueueConnection(outgoing, incoming)))]
    try:
        async with asyncio.timeout(3):
            while len([w for w in store.workers.list_workers() if w.inference is not None]) != 2:
                await asyncio.sleep(.01)
        local = store.jobs.enqueue_inference(request())
        server.gateway.wake_all()
        await asyncio.wait_for(entered.wait(), 3)
        assert store.jobs.get_job(local.id).assigned_worker_id == LOCAL_BUB_WORKER_ID
        remote = store.jobs.enqueue_inference(request())
        server.gateway.wake_all()
        await settled(store, remote, JobState.DONE)
        assert store.jobs.get_job(remote.id).assigned_worker_id == "worker-1"
        release.set()
        await settled(store, local, JobState.DONE)
        assert store.jobs.get_job(local.id).result_payload["output"]["text"] == "Local answer"
        with store.database.connection() as connection:
            assert connection.execute("SELECT count(*) AS n FROM flow_executions").fetchone()["n"] == 0
        assert not next(w for w in store.workers.list_workers() if w.worker_id == LOCAL_BUB_WORKER_ID).execute
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await execution.shutdown()


async def test_network_worker_cannot_claim_local_priority_even_with_a_configured_token():
    socket = Mock(headers={"x-flowtool-worker-id": LOCAL_BUB_WORKER_ID,
                           "authorization": "Bearer secret"}, close=AsyncMock(), accept=AsyncMock())
    serve = AsyncMock()
    await serve_worker(socket, worker_tokens={LOCAL_BUB_WORKER_ID: "secret"}, serve=serve)
    socket.close.assert_awaited_once_with(code=1008)
    assert not socket.accept.called and not serve.called


@pytest.mark.parametrize("outcome", ["malformed", "silent", "heartbeats"])
async def test_invalid_or_silent_inference_peer_settles_only_its_job(store, outcome):
    # Invalid output is terminal; a silent connection must release its unknown attempt for recovery.
    outgoing, incoming = asyncio.Queue(), asyncio.Queue()
    server = AsgiRuntime(store, 1)
    server.settings.worker_heartbeat_timeout = .5
    serving = asyncio.create_task(serve_worker(QueueSocket(outgoing, incoming),
        worker_tokens={"worker-1": "worker-secret"}, serve=server.serve_worker_session))
    await outgoing.put(Register(**message_header(None), control="register", payload=[],
        worker_id="worker-1", recovery=None,
        inference=InferenceCapability(adapter_version="codex/v1")).model_dump_json())
    try:
        async with asyncio.timeout(3):
            while not isinstance(registered := parse_server_message(await incoming.get()), Registered):
                pass
            job = store.jobs.enqueue_inference(request())
            server.gateway.wake_all()
            while True:
                message = parse_server_message(await incoming.get())
                call = next((item for item in message.payload if isinstance(item, InferenceRequest)), None)
                if call is not None:
                    break
            if outcome == "silent":
                await asyncio.shield(serving)
                saved = store.jobs.get_job(job.id)
                assert saved.state is JobState.QUEUED and saved.attempt == 1
                assert saved.inference.excluded_workers == {"worker-1"}
                return
            if outcome == "heartbeats":
                for _ in range(2):
                    await asyncio.sleep(.3)
                    await outgoing.put(Heartbeat(**message_header(registered.connection_epoch),
                        control="heartbeat", payload=[], job_id=job.id,
                        assignment_token=call.assignment_token).model_dump_json())
                assert not serving.done()  # Inference-only liveness renews the connection, not its budget.
            result = InferenceResult(**result_header(call.message_id), method="worker.infer",
                job_id=job.id, assignment_token=call.assignment_token,
                output=InferenceOutput(request_id=call.arguments.request_id, request_digest=call.arguments.digest,
                                       text="Answer", adapter_version="codex/v1"))
            wire = WorkerMessage(**message_header(registered.connection_epoch), control=None,
                                 payload=[result]).model_dump(mode="json")
            wire["payload"][0]["output"]["unexpected"] = True
            await outgoing.put(json.dumps(wire))
            await settled(store, job, JobState.FAILED)
        failed = store.jobs.get_job(job.id)
        assert failed.attempt == 1 and failed.inference.summary.last_failure_code == "invalid_response"
        assert not serving.done()
        assert store.workers.list_workers()[0].current_inference_job_id is None
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
