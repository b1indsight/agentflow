"""Unified job delivery keeps inference cancellation independent from coding."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from app.contracts.protocol import (
    AgentResult,
    AgentStart,
    Assignment,
    ExecutionIdentity,
    Heartbeat,
    PayloadValidationError,
    ProtocolError,
    Register,
    Registered,
    Started,
    ValidationErrorResult,
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
    InferenceFailure,
)
from app.domain.models import CodingExecutionResult, JobKind, JobState, utcnow
from app.inference.local import serve_local_inference
from app.persistence.records import required
from app.worker.config import WorkerSettings
from app.worker.runtime import WorkerRuntime
from tests.flow_fixtures import enqueue_job
from tests.test_remote_e2e import QueueConnection, QueueSocket
from tests.test_remote_e2e import remote_flow as remote_flow  # noqa: PLC0414
from tests.worker_fixtures import inventory, naming_start, run_worker, script_inference
from tests.workspace_fixtures import register_worker

pytestmark = pytest.mark.usefixtures("recording_resources")


def request():
    return "Summarize supplied facts"


@pytest.mark.parametrize("changes", [
    {"task": "Different evidence"}, {"output_schema": {"type": "object"}},
    {"model": "other-model"}, {"effort": "low"}, {"max_output_bytes": 256},
    {"timeout_seconds": 30}, {"validation": "flow_naming"},
])
def test_explicit_request_identity_cannot_change_frozen_arguments(store, changes):
    arguments = {"task": "Answer", "request_id": "a" * 32,
                 "output_schema": {"type": "string"}, "model": "selected", "effort": "high",
                 "max_output_bytes": 128, "timeout_seconds": 60}
    job = store.inference.enqueue_inference(**arguments)
    with pytest.raises(ValueError, match="frozen call"):
        store.inference.enqueue_inference(**(arguments | changes))
    assert store.jobs.get_job(job.id) == job


def test_explicit_request_identity_cannot_be_reused_by_another_flow(store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    job = store.inference.enqueue_inference("Answer", request_id="a" * 32)
    with pytest.raises(ValueError, match="another caller"):
        store.inference.enqueue_inference("Answer", request_id="a" * 32, flow_id=flow.id)
    assert store.jobs.get_job(job.id) == job


def test_retry_keeps_original_deadline_and_fences_each_assignment(store, monkeypatch):
    # Provider backoff must not reset a logical call or let a late prior answer win.
    clock = utcnow()
    monkeypatch.setattr(store.database, "_clock", lambda: clock)
    register_worker(store, "worker", epoch="epoch")
    store.workers.worker_ready("worker", "epoch", "codex")
    arguments = {"request_id": "a" * 32, "output_schema": {"type": "string"},
                     "model": "selected", "effort": "high", "max_output_bytes": 128, "timeout_seconds": 60}
    queued = store.inference.enqueue_inference("Answer", **arguments)
    first = store.assignments.assign_job("worker", "first", "epoch", slot="infer")
    failure = AgentResult(**result_header(first.inference.current_request_id), method="worker.start",
        job_id=first.id, assignment_token="first",
        failure=InferenceFailure(code="rate_limited", retry_after_seconds=1))
    assert store.assignments.record_assignment_result(first.id, "worker", "first", "epoch", failure) == "pending"
    assert store.inference.complete_inference_result(first.id)
    assert store.inference.enqueue_inference("Answer", **arguments).id == first.id
    clock += timedelta(seconds=.5)
    assert store.assignments.assign_job("worker", "early", "epoch", slot="infer") is None
    assert store.jobs.get_job(first.id).attempt == 1
    clock += timedelta(seconds=.5)
    second = store.assignments.assign_job("worker", "second", "epoch", slot="infer")
    assert second is not None and second.id == queued.id and second.attempt == 2
    assert second.inference.request_id == queued.inference.request_id
    assert second.inference.deadline_at == queued.inference.deadline_at
    assert (second.inference.deadline_at - clock).total_seconds() == 59
    assert second.inference.current_request_id != first.inference.current_request_id
    assert second.inference.task == "Answer" and second.inference.output_schema == {"type": "string"}
    assert (second.inference.model, second.inference.effort, second.inference.max_output_bytes) == (
        "selected", "high", 128)
    answer = AgentResult(**result_header(second.inference.current_request_id), method="worker.start",
        job_id=second.id, assignment_token="second",
        result=CodingExecutionResult(None, '"Answer"', 0, backend="codex", model="selected"))
    stale_reply = answer.model_copy(update={"reply_to": first.inference.current_request_id})
    for token, epoch, result in (("first", "epoch", answer), ("second", "stale-epoch", answer),
                                  ("second", "epoch", stale_reply)):
        assert store.assignments.record_assignment_result(second.id, "worker", token, epoch, result) == "rejected"
    assert store.jobs.get_job(second.id).result_received_at is None
    assert store.assignments.record_assignment_result(second.id, "worker", "second", "epoch", answer) == "pending"
    assert store.inference.complete_inference_result(second.id)
    assert store.jobs.get_job(second.id).state is JobState.DONE


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
        await asyncio.gather(*(execution.shutdown() for execution in worker.executions.values()))


async def test_inference_cleanup_keeps_its_slot_without_blocking_coding(store, remote_flow, monkeypatch):
    coding_entered, coding_release = asyncio.Event(), asyncio.Event()
    infer_entered, infer_release, infer_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    infer_cleaning, cleanup_release = asyncio.Event(), asyncio.Event()

    flow, server, _github, worker, agent = remote_flow
    coding, = store.jobs.list_jobs(flow.id)
    start = agent.start

    async def delayed_coding(task, cwd, **kwargs):
        if cwd == worker.workspaces.inference_path:
            return await start(task, cwd, **kwargs)
        coding_entered.set()
        await coding_release.wait()
        return await start(task, cwd, **kwargs)

    monkeypatch.setattr(agent, "start", delayed_coding)

    async def infer(task, cwd, **kwargs):
        if kwargs["output_schema"] is not None:
            return await naming_start(task, cwd, **kwargs)
        infer_entered.set()
        try:
            await infer_release.wait()
            return CodingExecutionResult(None, "Summary", 0, backend="codex")
        finally:
            infer_cleaning.set()
            await cleanup_release.wait()
            infer_closed.set()

    script_inference(monkeypatch, worker, infer)
    async with connected(server, worker) as (working, serving):
        try:
            await asyncio.wait_for(coding_entered.wait(), 3)
            inference = store.inference.enqueue_inference(request(), timeout_seconds=10)
            server.gateway.wake_all()
            await asyncio.wait_for(infer_entered.wait(), 3)
            clock = utcnow() + timedelta(seconds=20)
            store.database._clock = lambda: clock
            monkeypatch.setattr(server.database, "_clock", lambda: clock)
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
            assert store.workers.list_workers()[0].current_inference_job_id != inference.id
            assert not working.done() and not serving.done()
        finally:
            cleanup_release.set()
            coding_release.set()


@pytest.mark.parametrize("flow_bound", [False, True])
async def test_local_mini_and_repo_free_worker_share_dispatch_and_results(
    store, tmp_path, server_runtime, flow_bound, monkeypatch,
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def local_start(task, cwd, **kwargs):
        assert cwd == workspace
        entered.set()
        await release.wait()
        return CodingExecutionResult(None, "Local answer", 0, backend="bub", model="local-model")

    async def remote_start(task, cwd, **kwargs):
        return CodingExecutionResult(None, "Remote answer", 0, backend="codex", model="remote-model")

    server = server_runtime()
    worker = WorkerRuntime(WorkerSettings(
        worker_id="worker-1", worker_token="worker-secret", serve_ws_url="wss://unused",
        worker_root=tmp_path / "worker", trusted_host_execution=True,
        execution_isolation="container",
    ))
    script_inference(monkeypatch, worker, remote_start)
    workspace = tmp_path / "local-default"
    workspace.mkdir()
    mini = asyncio.create_task(serve_local_inference(server.serve_worker_session, local_start, workspace))
    flow = store.flows.create_flow("owner/repo", 7, 42) if flow_bound else None
    flow_id = flow.id if flow is not None else None
    local_caller = JobInference(store.database, server.gateway.wake_all, flow_id=flow_id)
    remote_caller = JobInference(store.database, server.gateway.wake_all, flow_id=flow_id)
    try:
        async with connected(server, worker):
            async with asyncio.timeout(3):
                while len([w for w in store.workers.list_workers() if w.inference is not None]) != 2:
                    await asyncio.sleep(.01)
            with pytest.raises(InferencePending) as pending:
                await local_caller.start(request())
            local = store.jobs.get_job(pending.value.job_id)
            await asyncio.wait_for(entered.wait(), 3)
            assert store.jobs.get_job(local.id).assigned_worker_id == LOCAL_BUB_WORKER_ID
            with pytest.raises(InferencePending) as replay:
                await local_caller.start(request())
            assert replay.value.job_id == local.id
            assert store.jobs.get_job(local.id).attempt == 1
            if flow is not None:
                # A text call referenced as the title job must still have no naming side effect.
                with store.connection() as connection:
                    connection.execute("UPDATE flows SET title_job_id=%s WHERE id=%s", (local.id, flow.id))
            with pytest.raises(InferencePending) as pending:
                await remote_caller.start(request())
            remote = store.jobs.get_job(pending.value.job_id)
            assert remote.id != local.id
            await settled(store, remote, JobState.DONE)
            assert store.jobs.get_job(remote.id).assigned_worker_id == "worker-1"
            release.set()
            await settled(store, local, JobState.DONE)
            assert local.flow_id == remote.flow_id == flow_id
            with store.database.connection() as connection:
                assert connection.execute("SELECT count(*) AS n FROM flow_executions").fetchone()["n"] == 0
            assert not next(w for w in store.workers.list_workers() if w.worker_id == LOCAL_BUB_WORKER_ID).execute
    finally:
        mini.cancel()
        await asyncio.gather(mini, return_exceptions=True)
    # A recreated caller reads saved answers with both workers gone, without a new attempt.
    recovered_local = JobInference(store.database, server.gateway.wake_all, flow_id=flow_id,
                                   request_id=local.inference.request_id)
    recovered_remote = JobInference(store.database, server.gateway.wake_all, flow_id=flow_id,
                                    request_id=remote.inference.request_id)
    for recovered, text, backend, model in (
        (recovered_local, "Local answer", "bub", "local-model"),
        (recovered_remote, "Remote answer", "codex", "remote-model"),
    ):
        result = await recovered.start(request())
        assert (result.last_message, result.backend, result.model) == (text, backend, model)
    assert store.jobs.get_job(local.id).attempt == store.jobs.get_job(remote.id).attempt == 1
    if flow is not None:
        assert store.flows.get_flow(flow.id).title is None


async def test_network_worker_cannot_claim_local_priority_even_with_a_configured_token():
    socket = Mock(headers={"x-flowtool-worker-id": LOCAL_BUB_WORKER_ID,
                           "authorization": "Bearer secret"}, close=AsyncMock(), accept=AsyncMock())
    serve = AsyncMock()
    await serve_worker(socket, worker_tokens={LOCAL_BUB_WORKER_ID: "secret"}, serve=serve)
    socket.close.assert_awaited_once_with(code=1008)
    assert not socket.accept.called and not serve.called


@pytest.mark.parametrize("wrong_token", [False, True])
async def test_completed_inference_heartbeat_skips_renewal_but_dispatches_payload(
    store, wrong_token, server_runtime,
):
    # A queued heartbeat can follow its result, even after another slot starts work.
    outgoing, incoming = asyncio.Queue(), asyncio.Queue()
    flow = store.flows.create_flow("owner/repo", 8, 42)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.set_current_attempt(flow)
    from tests.github import RecordingGitHub

    github = RecordingGitHub()
    github.refs = {}
    server = server_runtime(github=github)
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
        return AgentResult(**result_header(call.message_id), method="worker.start",
            job_id=call.job_id, assignment_token=call.assignment_token,
            result=CodingExecutionResult(None,
                '{"title":"Implement task","branch_slug":"feat/implement-task"}'
                    if call.output_schema is not None else "Answer", 0, backend="codex"))

    await outgoing.put(Register(**message_header(None), control="register", payload=[],
        worker_id="worker-1", coding_agent="codex", recoveries=[], workspace="b" * 32, repo_workspace_limit=2,
        inventory=inventory(), inventory_complete=True,
        inference="codex").model_dump_json())
    try:
        async with asyncio.timeout(3):
            while not isinstance(registered := parse_server_message(await incoming.get()), Registered):
                pass
        epoch = registered.connection_epoch
        first = store.inference.enqueue_inference(request())
        server.gateway.wake_all()
        first_call = await next_call(AgentStart)
        await outgoing.put(WorkerMessage(**message_header(epoch), control=None,
                                         payload=[answer(first_call)]).model_dump_json())
        await settled(store, first, JobState.DONE)

        coding = enqueue_job(store, flow.id, JobKind.INITIAL)
        server.gateway.wake_all()
        title_call = await next_call(AgentStart)
        assert title_call.job_id == store.flows.get_flow(flow.id).title_job_id
        await outgoing.put(WorkerMessage(**message_header(epoch), control=None,
                                         payload=[answer(title_call)]).model_dump_json())
        await settled(store, store.jobs.get_job(title_call.job_id), JobState.DONE)
        call = await next_call(Assignment)
        await outgoing.put(WorkerMessage(**message_header(epoch), control=None, payload=[
            Started(**call_header(), method="server.execution_started",
                    arguments=ExecutionIdentity(job_id=coding.id,
                        assignment_token=call.arguments.assignment_token)),
        ]).model_dump_json())
        async with asyncio.timeout(3):
            while store.jobs.get_job(coding.id).started_at is None:
                await asyncio.sleep(.01)

        second = store.inference.enqueue_inference(request())
        server.gateway.wake_all()
        second_call = await next_call(AgentStart)
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


@pytest.mark.parametrize("defect", ["malformed_reply", "rejected_request"])
async def test_invalid_inference_exchange_fails_its_slot_and_keeps_the_connection(
    store, defect, server_runtime,
):
    # The AgentStart's own reply receiver records the failure; without it a malformed
    # answer or a worker rejection would wait for the provider deadline instead.
    outgoing, incoming = asyncio.Queue(), asyncio.Queue()
    server = server_runtime()
    serving = asyncio.create_task(serve_worker(QueueSocket(outgoing, incoming),
        worker_tokens={"worker-1": "worker-secret"}, serve=server.serve_worker_session))
    await outgoing.put(Register(**message_header(None), control="register", payload=[],
        worker_id="worker-1", coding_agent="codex", recoveries=[], workspace="b" * 32, repo_workspace_limit=2,
        inventory=inventory(), inventory_complete=True,
        inference="codex").model_dump_json())
    try:
        async with asyncio.timeout(3):
            while not isinstance(registered := parse_server_message(await incoming.get()), Registered):
                pass
            job = store.inference.enqueue_inference(request())
            server.gateway.wake_all()
            while not isinstance(
                call := next(iter(parse_server_message(await incoming.get()).payload), None),
                AgentStart,
            ):
                pass
        item = AgentResult(**result_header(call.message_id), method="worker.start",
            job_id=call.job_id, assignment_token=call.assignment_token,
            result=CodingExecutionResult(None, "Answer", 0, backend="codex"),
        ).model_dump(mode="json")
        if defect == "malformed_reply":
            item["result"] = {"last_message": "missing exit status"}
        else:
            item = ValidationErrorResult(**result_header(call.message_id), method=call.method,
                error=PayloadValidationError(code="validation_error",
                                             message="message payload failed validation"),
            ).model_dump(mode="json")
        await outgoing.put(json.dumps(
            {**message_header(registered.connection_epoch), "control": None, "payload": [item]}
        ))
        async with asyncio.timeout(3):
            while not (failures := required(store.jobs.get_job(job.id)).inference.failures):
                await asyncio.sleep(.01)
        assert [failure.code for failure in failures] == ["invalid_response"]
        assert not serving.done()
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)


async def test_slow_coding_preparation_keeps_inference_live(store, remote_flow, monkeypatch):
    # GitHub preparation must not hold an idle inference slot or its incoming results.
    preparing, prepare_release = asyncio.Event(), asyncio.Event()
    inferring, infer_release = asyncio.Event(), asyncio.Event()
    flow, server, _github, worker, _agent = remote_flow
    coding, = store.jobs.list_jobs(flow.id)
    server.settings.worker_heartbeat_interval = .02
    prepare = server.coordinator.prepare_for_assignment

    async def delayed_prepare(*args):
        preparing.set()
        await prepare_release.wait()
        return await prepare(*args)

    prepare_call = AsyncMock(side_effect=delayed_prepare)
    monkeypatch.setattr(server.coordinator, "prepare_for_assignment", prepare_call)

    async def infer(task, cwd, **kwargs):
        if kwargs["output_schema"] is not None:
            return await naming_start(task, cwd, **kwargs)
        inferring.set()
        await infer_release.wait()
        return CodingExecutionResult(None, "Answer during preparation", 0, backend="codex")

    script_inference(monkeypatch, worker, infer)
    async with connected(server, worker) as (working, serving):
        await asyncio.wait_for(preparing.wait(), 3)
        first_coding_heartbeat = store.jobs.get_job(coding.id).heartbeat_at
        inference = store.inference.enqueue_inference(request())
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
