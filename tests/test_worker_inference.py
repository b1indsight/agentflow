"""Inference selection and delivery use real sessions while coding keeps its own slot."""

import asyncio
from unittest.mock import Mock

import pytest

from app.communication.gateway import Gateway
from app.contracts.protocol import (
    PROTOCOL_VERSION,
    InferenceRequest,
    InferenceResult,
    WorkerMessage,
    message_header,
    parse_server_message,
    result_header,
)
from app.controler.workers.inference import InferencePool
from app.controler.workers.transport import serve_worker
from app.domain.inference_models import (
    InferenceCapability,
    InferenceError,
    InferenceMessage,
    InferenceOutput,
    InferenceTask,
)
from app.domain.models import JobKind, JobState
from app.inference.router import InferenceRouter
from app.worker.execution.lifecycle import WorkerExecution
from app.worker.state.execution import ExecutionState
from tests.test_conversation_proxy import (
    history_connection as history_connection,  # noqa: PLC0414 -- pytest fixture
)
from tests.test_remote_asgi import AsgiRuntime
from tests.test_remote_e2e import QueueConnection, QueueSocket
from tests.test_worker_client import RecordingExecutor
from tests.worker_fakes import run_worker, worker_runtime


def request():
    return InferenceTask(messages=[InferenceMessage(role="user", content="Summarize supplied facts")],
                         timeout_seconds=1)


def answer(task):
    return InferenceOutput(request_id=task.request_id, request_digest=task.digest,
                           text="Summary", adapter_version="codex/v1")


async def receive(socket):
    message = parse_server_message(await asyncio.wait_for(socket.sent.get(), 1))
    assert isinstance(message.payload[0], InferenceRequest)
    return message.payload[0].arguments


async def reply(socket, task, *, digest=None):
    output = answer(task)
    if digest:
        output = output.model_copy(update={"request_digest": digest})
    result = InferenceResult(**result_header(task.request_id), method="worker.infer", output=output)
    await socket.incoming.put(WorkerMessage(**message_header("epoch"), control=None,
                                           payload=[result]).model_dump_json())


async def connect(pool, store, history_connection, identity="worker", *, ready=True):
    registration, socket = await history_connection(pool.peers, worker=identity)
    store.workers.worker_connected(identity, PROTOCOL_VERSION, "epoch")
    if ready:
        pool.ready(identity, registration.session, InferenceCapability(adapter_version="codex/v1"))
    return registration, socket


async def test_selection_requires_ready_capability_epoch_and_free_independent_slot(store, history_connection):
    pool = InferencePool(Gateway(), database=store.database)
    registration, first = await connect(pool, store, history_connection, ready=False)
    with pytest.raises(InferenceError, match="unavailable"):
        await pool.infer(request())
    pool.ready("worker", registration.session, InferenceCapability(adapter_version="codex/v1"))
    _, second = await connect(pool, store, history_connection, "other")
    with store.database.connection() as connection:
        connection.execute("UPDATE workers SET status='busy' WHERE worker_id='worker'")
    running = asyncio.create_task(pool.infer(request()))
    task = await receive(first)
    another = asyncio.create_task(pool.infer(request()))
    second_task = await receive(second)
    with pytest.raises(InferenceError, match="unavailable"):
        await pool.infer(request())
    await reply(first, task)
    await reply(second, second_task)
    assert (await running).text == (await another).text == "Summary"
    for status, epoch, draining in [("recovering", "epoch", False), ("failed", "epoch", False),
                                    ("online", "old", False), ("online", "epoch", True)]:
        with store.database.connection() as connection:
            connection.execute("UPDATE workers SET status=%s,connection_epoch=%s,draining=%s",
                               (status, epoch, draining))
        with pytest.raises(InferenceError, match="unavailable"):
            await pool.infer(request())


@pytest.mark.parametrize("rejection", ["digest", "caller-schema"])
async def test_partition_retries_on_another_worker_and_rejects_invalid_output(
    store, history_connection, rejection,
):
    pool = InferencePool(Gateway(), database=store.database)
    _, first = await connect(pool, store, history_connection)
    _, second = await connect(pool, store, history_connection, "other")
    running = asyncio.create_task(InferenceRouter(local=None, workers=pool).infer(request()))
    old = await receive(first)
    first.incoming.put_nowait(ConnectionError("partition"))
    current = await receive(second)
    assert current.request_id != old.request_id and current.messages == old.messages
    await reply(second, current)
    assert (await running).text == "Summary"
    invalid = asyncio.create_task(pool.infer(
        request(), validate=(lambda result: int(result.text)) if rejection == "caller-schema" else None,
    ))
    task = await receive(second)
    await reply(second, task, digest="0" * 64 if rejection == "digest" else None)
    with pytest.raises(InferenceError, match="invalid_response") as caught:
        await invalid
    assert caught.value.worker_id == "other" and caught.value.backend == "codex/v1"
    again = asyncio.create_task(pool.infer(request()))
    await reply(second, task)  # Late old reply cannot satisfy the next logical call.
    await reply(second, await receive(second))
    assert (await again).text == "Summary"


async def test_authenticated_worker_infers_while_coding_and_joins_inference_on_disconnect(store, tmp_path):
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
                return answer(task)
            finally:
                infer_closed.set()

    flow = store.flows.create_flow("owner/repo", 8, 42)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    server = AsgiRuntime(store, attempt.id)
    execution = WorkerExecution(worker_id="worker-1", executor=Coding(), state=ExecutionState(tmp_path))
    worker = worker_runtime(worker_id="worker-1", worker_token="worker-secret",
                            serve_ws_url="ws://test", execution=execution, read_history=Mock())
    worker.inference = Inference()
    outgoing, incoming = asyncio.Queue(), asyncio.Queue()
    serving = asyncio.create_task(serve_worker(QueueSocket(outgoing, incoming),
        worker_tokens={"worker-1": "worker-secret"}, serve=server.serve_worker_session))
    working = asyncio.create_task(run_worker(worker, QueueConnection(outgoing, incoming)))
    pending = None
    try:
        await asyncio.wait_for(coding_entered.wait(), 2)
        pending = asyncio.create_task(server.inference_pool.infer(request()))
        await asyncio.wait_for(infer_entered.wait(), 2)
        assert store.jobs.get_job(job.id).state == JobState.RUNNING
        with pytest.raises(InferenceError, match="unavailable"):
            await server.inference_pool.infer(request())
        coding_release.set()
        async with asyncio.timeout(2):
            while store.jobs.get_job(job.id).state != JobState.DONE:
                await asyncio.sleep(.01)
        assert not pending.done()  # Coding result and acceptance made progress during slow inference.
        infer_release.set()
        assert (await pending).text == "Summary"
        infer_entered.clear()
        infer_release.clear()
        infer_closed.clear()
        pending = asyncio.create_task(server.inference_pool.infer(request()))
        await asyncio.wait_for(infer_entered.wait(), 2)
        working.cancel()
        await asyncio.gather(working, return_exceptions=True)
        assert infer_closed.is_set()
    finally:
        for task in (working, serving, pending):
            if task is not None:
                task.cancel()
        await asyncio.gather(working, serving, *([pending] if pending else []), return_exceptions=True)
        await execution.shutdown()
