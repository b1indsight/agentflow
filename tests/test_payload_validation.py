"""Validation failures cross the real Runtime paths without granting business authority."""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import Mock

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
from app.domain.models import VcsKind, VcsOutcome
from app.worker.execution.lifecycle import FlowToolCallError, WorkerExecution, WorkerProtocolError
from app.worker.state.execution import ExecutionState
from tests.test_communication_session import Socket
from tests.test_conversation_history import SESSION
from tests.test_conversation_worker import Connection
from tests.test_worker_client import RecordingExecutor, assignment_message, registered_message
from tests.test_worker_handler import RecordingStore, handler_for, register_message, worker_message
from tests.test_worker_session import _assignment
from tests.worker_fakes import run_worker, worker_runtime

ERROR = {"code": "validation_error", "message": "message payload failed validation"}


@asynccontextmanager
async def server_connection():
    store = RecordingStore()
    runtime = handler_for(store)
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
            if message.payload:
                return message


async def test_bad_assignment_rejects_every_sibling_without_recording_then_continues(tmp_path):
    # A valid first sibling must not launch before a later sibling fails schema validation.
    connection = Connection()
    state = ExecutionState(tmp_path, 3)
    execution = WorkerExecution(worker_id="worker", executor=RecordingExecutor(), state=state)
    runtime = worker_runtime(
        worker_id="worker",
        worker_token="token",
        serve_ws_url="unused",
        execution=execution,
        read_history=state.read,
    )
    running = asyncio.create_task(run_worker(runtime, connection))
    try:
        await connection.outgoing.get()
        await connection.incoming.put(registered_message().model_dump_json())
        valid = assignment_message(_assignment())
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
    runtime = worker_runtime(
        worker_id="worker",
        worker_token="token",
        serve_ws_url="unused",
        execution=execution,
        read_history=state.read,
    )
    running = asyncio.create_task(run_worker(runtime, connection))
    try:
        await connection.outgoing.get()
        await connection.incoming.put(registered_message().model_dump_json())
        await connection.incoming.put(assignment_message(_assignment()).model_dump_json())
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


async def test_bad_heartbeat_tool_batch_has_no_ack_lease_or_tool_effect_then_continues():
    async with server_connection() as (runtime, store, socket, serving):
        assignment = (await receive(socket)).payload[0]
        identity = ExecutionIdentity(
            job_id=assignment.arguments.job_id,
            assignment_token=assignment.arguments.assignment_token,
        )
        await socket.incoming.put(
            worker_message(
                "epoch",
                Started(
                    **call_header(),
                    method="server.execution_started",
                    arguments=identity,
                ),
            ).model_dump_json()
        )
        tool = FlowToolRequest(
            **call_header(),
            method="flow.open_pr",
            arguments=FlowToolArguments(
                **identity.model_dump(),
                flow_id=3,
                attempt_id=11,
                call_id="open",
                arguments={},
                vcs_backend="git",
                branch="flow/issue-8",
                commit_sha="abc",
                remote_sha="abc",
            ),
        )
        heartbeat = Heartbeat(
            **message_header("epoch"), control="heartbeat", payload=[tool], **identity.model_dump()
        )
        store.heartbeat_assignment = Mock(wraps=store.heartbeat_assignment)
        bad = heartbeat.model_dump(mode="json")
        bad["payload"][0]["arguments"] = {"token": "private-token"}
        await socket.incoming.put(json.dumps(bad))
        response = await receive(socket)
        assert response.ack_id != heartbeat.message_id
        assert response.payload[0].error.model_dump() == ERROR
        assert response.payload[0].reply_to == tool.message_id
        store.heartbeat_assignment.assert_not_called()
        runtime.tools.execute.assert_not_awaited()
        await socket.incoming.put(worker_message("epoch", tool).model_dump_json())
        assert (await receive(socket)).payload[0].call_id == "open"
        runtime.tools.execute.assert_awaited_once()
        assert not serving.done()


@pytest.mark.parametrize("kind", ["malformed", "repeated_reply", "rejection", "eof"])
async def test_history_invalid_response_finishes_original_query_and_allows_next(kind):
    async with server_connection() as (runtime, _store, socket, serving):
        await receive(socket)  # Read-only replies do not need the execution's Started.
        broker = ConversationBroker(runtime.gateway)
        query = asyncio.create_task(broker.query("worker-1", HistoryQuery(session_id=SESSION)))
        request = (await receive(socket)).payload[0]
        reply = ValidationErrorResult(
            **result_header(request.message_id),
            method="worker.history",
            error=PayloadValidationError(**ERROR),
        )
        message = worker_message("epoch", reply).model_dump(mode="json")
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
                "epoch",
                ConversationResult(
                    **result_header(request.message_id),
                    method="worker.history",
                    error="history_unreadable",
                ),
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
