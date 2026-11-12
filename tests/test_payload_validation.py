"""Validation failures cross the real Runtime paths without granting business authority."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.communication.session import Session
from app.contracts.conversation import ConversationFailure, HistoryQuery
from app.contracts.protocol import (
    ConversationResult,
    ExecutionIdentity,
    FlowToolArguments,
    FlowToolRequest,
    Heartbeat,
    PayloadValidationError,
    RepositoryDemand,
    ServerMessage,
    ServerMessageUnion,
    Started,
    ValidationErrorResult,
    WorkerMessageUnion,
    call_header,
    message_header,
    parse_server_message,
    result_header,
)
from app.contracts.validation import parse_worker_input
from app.controler.workers.conversation import ConversationBroker
from app.controler.workers.transport import control_ack
from app.controler.workflow.service import ControlService
from app.domain.models import VcsKind, VcsOutcome
from app.persistence.records import required
from app.worker.execution.executor import WorkerExecutor
from app.worker.execution.lifecycle import FlowToolCallError, WorkerExecution, WorkerProtocolError
from app.worker.state.execution import ExecutionState
from tests.test_communication_session import Socket
from tests.test_conversation_history import SESSION
from tests.test_conversation_worker import Connection
from tests.test_worker_client import RecordingExecutor, assignment_message, registered_message
from tests.test_worker_handler import register_message
from tests.test_worker_session import _assignment
from tests.test_workspace_dispatch import wait_until
from tests.wire import worker_message
from tests.worker_fixtures import run_worker, worker_runtime

ERROR = {"code": "validation_error", "message": "message payload failed validation"}
pytestmark = pytest.mark.usefixtures("recording_resources")


@asynccontextmanager
async def server_connection(store, runtime):
    assert ControlService(database=store.database).on_trigger("owner/repo", 8, 42) is not None
    socket = Socket()
    session = Session[WorkerMessageUnion, ServerMessageUnion](
        socket,
        parse=parse_worker_input,
        make_ack=control_ack,
        epoch="epoch",
    )
    session.start()
    serving = asyncio.create_task(
        runtime.serve_worker_session("worker-1", register_message(), session)
    )
    try:
        yield runtime, store, socket, serving
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)


async def receive(socket):
    async with asyncio.timeout(2):
        while True:
            message = parse_server_message(await socket.sent.get())
            if message.payload and not all(isinstance(item, RepositoryDemand) for item in message.payload):
                return message


async def test_bad_assignment_rejects_every_sibling_without_recording_then_continues(tmp_path, monkeypatch):
    # A valid first sibling must not launch before a later sibling fails schema validation.
    connection = Connection()
    monkeypatch.setattr(WorkerExecutor, "execute", RecordingExecutor.execute)
    state = ExecutionState(tmp_path, 3)
    execution = WorkerExecution(worker_id="worker", executor=RecordingExecutor(), state=state)
    runtime = await worker_runtime(
        worker_token="token",
        execution=execution,
        read_history=state.read,
    )
    running = asyncio.create_task(run_worker(runtime, connection))
    try:
        await connection.outgoing.get()
        await connection.incoming.put(registered_message().model_dump_json())
        valid = assignment_message(_assignment(workspace=runtime.workspaces.inventory()[1].workspace))
        invalid = assignment_message(_assignment()).model_dump(mode="json")
        invalid["payload"][0]["message_id"] = call_header()["message_id"]
        invalid["payload"][0]["arguments"] = {"private-token": "secret-input"}
        invalid["payload"].insert(0, valid.payload[0].model_dump(mode="json"))
        await connection.incoming.put(json.dumps(invalid))
        response = await connection.receive_message()
        assert [item.reply_to for item in response.payload] == [
            item["message_id"] for item in invalid["payload"]
        ]
        assert all(
            item.method == "worker.execute" and item.error.model_dump() == ERROR
            for item in response.payload
        )
        assert "secret-input" not in response.model_dump_json()
        assert state.load() is None
        await connection.incoming.put(valid.model_dump_json())
        assert isinstance((await connection.receive_message()).payload[0], Started)
        terminal = await connection.receive_message()
        assert terminal.payload[0].result.exit_code == 0
        assert runtime._connected
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await execution.shutdown()


@pytest.mark.parametrize("kind", ["malformed", "rejection", "critical_batch"])
async def test_tool_failure_retires_original_waiter_before_critical_recovery(tmp_path, kind):
    failed = asyncio.Event()

    class Executor:
        async def execute(self, task, secret=None, *, tool_caller):
            with pytest.raises(FlowToolCallError) as raised:
                await tool_caller.call_tool(
                    "flow.open_pr",
                    {},
                    vcs_outcome=VcsOutcome(
                        backend=VcsKind.GIT,
                        branch=task.branch,
                        commit_sha="abc",
                        remote_sha="abc",
                    ),
                )
            assert not raised.value.retryable
            if kind == "malformed":
                assert "outcome unknown" in str(raised.value)
            failed.set()
            await asyncio.Event().wait()

    connection = Connection()
    state = ExecutionState(tmp_path, 3)
    execution = WorkerExecution(worker_id="worker", executor=Executor(), state=state)
    runtime = await worker_runtime(
        worker_token="token",
        execution=execution,
        read_history=state.read,
    )
    work = _assignment(workspace=runtime.workspaces.inventory()[1].workspace)
    execution.receive(work)
    running = asyncio.create_task(run_worker(runtime, connection))
    try:
        await connection.outgoing.get()
        await connection.incoming.put(registered_message().model_dump_json())
        await connection.incoming.put(assignment_message(work).model_dump_json())
        started = (await connection.receive_message()).payload[0]
        tool = (await connection.receive_message()).payload[0]
        items = [
            ValidationErrorResult(
                **result_header(tool.message_id),
                method=tool.method,
                error=PayloadValidationError(**ERROR),
            )
        ]
        if kind == "critical_batch":
            # The critical error comes first; it must not prevent failing the ordinary call.
            items.insert(
                0,
                ValidationErrorResult(
                    **result_header(started.message_id),
                    method=started.method,
                    error=PayloadValidationError(**ERROR),
                ),
            )
        message = ServerMessage(
            **message_header("epoch-1"), control=None, payload=items
        ).model_dump(mode="json")
        if kind == "malformed":
            message["payload"][0].pop("error")
            message["payload"][0]["result"] = {"secret": "invalid-response"}
        await connection.incoming.put(json.dumps(message))
        await asyncio.wait_for(failed.wait(), 2)
        if kind == "critical_batch":
            with pytest.raises(WorkerProtocolError, match="recovery required"):
                await running
            assert not runtime._connected
        else:
            assert not running.done() and runtime._connected
        # Closing communication cannot cancel execution or acknowledge its persistent evidence.
        assert execution.lease() is not None
        assert state.load().state == "started" and not state.load().acknowledged
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await execution.shutdown()


async def test_bad_heartbeat_tool_batch_has_no_ack_lease_or_tool_effect_then_continues(store, monkeypatch, server_runtime):
    async with server_connection(store, server_runtime()) as (_runtime, _store, socket, serving):
        assignment = (await receive(socket)).payload[0]
        identity = ExecutionIdentity(
            job_id=assignment.arguments.job_id,
            assignment_token=assignment.arguments.assignment_token,
        )
        await socket.incoming.put(
            worker_message(
                Started(
                    **call_header(),
                    method="server.execution_started",
                    arguments=identity,
                ),
                epoch="epoch",
            ).model_dump_json()
        )
        await wait_until(lambda: required(store.jobs.get_job(identity.job_id)).started_at is not None)
        before = required(store.jobs.get_job(identity.job_id))
        later = store.database.now() + timedelta(seconds=10)
        monkeypatch.setattr(store.database, "_clock", lambda: later)
        task = assignment.arguments.task
        tool = FlowToolRequest(
            **call_header(),
            method="flow.open_pr",
            arguments=FlowToolArguments(
                **identity.model_dump(),
                flow_id=task.flow_id,
                attempt_id=task.attempt_id,
                call_id="open",
                arguments={"title": "Implement", "body": "Done"},
                vcs_backend="git",
                branch=task.branch,
                commit_sha="abc123",
                remote_sha="abc123",
            ),
        )
        heartbeat = Heartbeat(
            **message_header("epoch"), control="heartbeat", payload=[tool], **identity.model_dump()
        )
        bad = heartbeat.model_dump(mode="json")
        bad["payload"][0]["arguments"] = {"token": "private-token"}
        await socket.incoming.put(json.dumps(bad))
        response = await receive(socket)
        assert response.ack_id != heartbeat.message_id
        assert response.payload[0].error.model_dump() == ERROR
        assert response.payload[0].reply_to == tool.message_id
        assert required(store.jobs.get_job(identity.job_id)).heartbeat_at == before.heartbeat_at
        assert required(store.jobs.get_job(identity.job_id)).lease_expires_at == before.lease_expires_at
        assert store.deliveries.get_tool_call("open") is None
        await socket.incoming.put(worker_message(tool, epoch="epoch").model_dump_json())
        reply = (await receive(socket)).payload[0]
        assert reply.call_id == "open" and reply.error is None
        assert required(store.deliveries.get_tool_call("open")).response == reply.result
        assert not serving.done()


@pytest.mark.parametrize("kind", ["malformed", "repeated_reply", "rejection", "eof"])
async def test_history_invalid_response_finishes_original_query_and_allows_next(store, kind, server_runtime):
    async with server_connection(store, server_runtime()) as (runtime, _store, socket, serving):
        await receive(socket)  # Read-only replies do not need the execution's Started.
        broker = ConversationBroker(runtime.gateway)
        query = asyncio.create_task(broker.query("worker-1", HistoryQuery(session_id=SESSION)))
        request = (await receive(socket)).payload[0]
        reply = ValidationErrorResult(
            **result_header(request.message_id),
            method="worker.history",
            error=PayloadValidationError(**ERROR),
        )
        message = worker_message(reply, epoch="epoch").model_dump(mode="json")
        if kind != "rejection":
            message["payload"][0].pop("error")
            message["payload"][0]["page"] = {"session_id": 123}
        if kind == "repeated_reply":
            # Distinct reply items may share one call; reject its Future exactly once.
            message["payload"].append(
                dict(message["payload"][0], message_id=call_header()["message_id"])
            )
        await socket.incoming.put(json.dumps(message))
        if kind == "eof":
            # A disconnect immediately after the malformed reply cannot replace its error.
            socket.incoming.put_nowait(ConnectionError("EOF"))
        with pytest.raises(ConversationFailure, match="invalid_response"):
            await asyncio.wait_for(query, 2)
        if kind == "eof":
            await asyncio.wait_for(serving, 2)
            return
        if kind != "rejection":
            errors = (await receive(socket)).payload
            assert [item.reply_to for item in errors] == [
                item["message_id"] for item in message["payload"]
            ]
            assert all(item.error.model_dump() == ERROR for item in errors)
        again = asyncio.create_task(broker.query("worker-1", HistoryQuery(session_id=SESSION)))
        request = (await receive(socket)).payload[0]
        await socket.incoming.put(
            worker_message(
                ConversationResult(
                    **result_header(request.message_id),
                    method="worker.history",
                    error="history_unreadable",
                ),
                epoch="epoch",
            ).model_dump_json()
        )
        with pytest.raises(ConversationFailure, match="history_unreadable"):
            await again
        assert not serving.done() and not broker._inflight


@pytest.mark.parametrize("change", ["extra", "missing", "wrong"])
def test_error_has_only_fixed_required_fields(change):
    error = dict(ERROR)
    if change == "extra":
        error["details"] = []
    elif change == "missing":
        error.pop("message")
    else:
        error["message"] = "secret input"
    with pytest.raises(ValidationError):
        PayloadValidationError.model_validate(error)
