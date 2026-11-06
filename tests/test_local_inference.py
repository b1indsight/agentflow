"""Local inference crosses both real Session drivers without a router or coding runtime."""

import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager

import pytest
from pydantic import SecretStr

from app.communication.gateway import Gateway
from app.communication.inprocess import in_process_transport_pair
from app.communication.requests import ReplyRejected, request_reply
from app.communication.session import Session, SessionClosed, SessionOverflow
from app.contracts.protocol import (
    AcceptResultRequest,
    AcceptResultResponse,
    Assignment,
    AssignmentArguments,
    CancelJob,
    ConversationQuery,
    ExecuteResult,
    Heartbeat,
    HistoryArguments,
    InferenceRequest,
    InferenceResult,
    InitialTask,
    InitialTaskResult,
    RecoveryDecision,
    Register,
    Registered,
    ResultAcceptance,
    ServerMessage,
    ServerMessageUnion,
    Started,
    WorkerMessageUnion,
    call_header,
    execution_call_id,
    message_header,
    parse_worker_message,
    result_header,
)
from app.contracts.validation import parse_worker_input
from app.domain.inference_models import (
    LOCAL_BUB_WORKER_ID,
    InferenceCapability,
    InferenceMessage,
    InferenceOutput,
    InferenceTask,
)
from app.inference.bub import ADAPTER_VERSION
from app.inference.local import serve_local_inference
from app.inference.transport import InferenceProtocolError
from app.worker.config import WorkerSettings
from app.worker.execution.lifecycle import WorkerExecution, WorkerProtocolError
from app.worker.runtime import WorkerRuntime
from app.worker.state.execution import ExecutionState


def request(*, job_id=7, token="inference-1"):
    task = InferenceTask(messages=[InferenceMessage(role="user", content="Summarize supplied facts")])
    return InferenceRequest(
        **call_header(task.request_id), method="worker.infer", arguments=task,
        job_id=job_id, assignment_token=token,
    )


def coding_request():
    return Assignment(
        **call_header(execution_call_id(5, "coding")), method="worker.execute",
        arguments=AssignmentArguments(
            job_id=5, assignment_token="coding", flow_id=3,
            task=InitialTask(flow_id=3, attempt_id=1, repo="owner/repo", issue_number=1,
                             issue_title="Title", issue_body="", branch="flow/3"),
        ),
    )


@pytest.mark.parametrize("module", ["app.inference.local", "app.controler.runtime"])
def test_local_entrypoints_do_not_import_the_coding_worker(module):
    # Moving a facade is insufficient if its imports still pull in the coding runtime or Flow tools.
    result = subprocess.run(
        [sys.executable, "-c",
         f"import json, sys\nimport {module}\nprint(json.dumps(sorted(sys.modules)))"],
        check=True, capture_output=True, text=True,
    )
    modules = json.loads(result.stdout)
    assert not [name for name in modules if name == "app.worker" or name.startswith("app.worker.")]


class Provider:
    def __init__(self):
        self.entered, self.finish = asyncio.Event(), asyncio.Event()
        self.cleaning, self.release, self.closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.calls = []

    def capability(self):
        return InferenceCapability(adapter_version=ADAPTER_VERSION)

    async def infer(self, task):
        self.calls.append(task)
        self.entered.set()
        try:
            await self.finish.wait()
            return InferenceOutput(
                request_id=task.request_id, request_digest=task.digest,
                text="Summary", adapter_version=ADAPTER_VERSION,
            )
        finally:
            self.cleaning.set()
            await self.release.wait()
            self.closed.set()


class Host:
    """Only protocol driving: scheduler/persistence decisions belong to controller tests."""

    def __init__(self, *, register_worker=True):
        self.peers = Gateway()
        self.ready, self.closed = asyncio.Event(), asyncio.Event()
        self.messages = asyncio.Queue()
        self.register_worker = register_worker

    async def serve(self, identity, register, session):
        self.identity, self.register, self.session = identity, register, session
        try:
            await self.peers.serve(identity, session, self, replace=False)
        finally:
            self.closed.set()

    def bind(self, routes):
        self.routes = routes
        routes.register("server.execution_started", Started, self.on_started)
        routes.register("server.accept_result", AcceptResultRequest, self.on_accept)
        routes.configure(
            opened=self.opened, message=self.message,
            unmatched_reply=self.unexpected, validation_error=self.on_error,
        )

    async def opened(self):
        if self.register_worker:
            await self.session.send(Registered(
                **message_header(self.session.epoch), control="registered", payload=[],
                recovery=None, heartbeat_interval=.01,
            ))
        self.ready.set()

    async def message(self, message):
        await self.messages.put(message)

    async def on_started(self, call, message):
        pass

    async def on_accept(self, call, message):
        await self.session.send(ServerMessage(
            **message_header(self.session.epoch), control=None, payload=[AcceptResultResponse(
                **result_header(call.message_id), method="server.accept_result",
                result=ResultAcceptance(job_id=call.arguments.job_id,
                                        assignment_token=call.arguments.assignment_token, state="done"),
            )],
        ))

    async def unexpected(self, item, message):
        raise AssertionError(f"unexpected {item.method}")

    async def on_error(self, item, message):
        if not self.routes.reject_reply(item, "message payload failed validation"):
            await self.unexpected(item, message)

    async def infer(self, call):
        peer = self.peers.get(self.identity)
        assert peer is not None
        result, message = await request_reply(
            peer, call, InferenceResult,
            ServerMessage(**message_header(self.session.epoch), control=None, payload=[call]),
            timeout=2,
        )
        assert message.connection_epoch == self.session.epoch
        assert (result.job_id, result.assignment_token, result.reply_to) == (
            call.job_id, call.assignment_token, call.message_id,
        )
        return result

    async def cancel(self, call, **fields):
        await self.session.send(CancelJob(
            **(message_header(self.session.epoch) | fields), control="cancel_job", payload=[],
            job_id=call.job_id, assignment_token=call.assignment_token,
        ))


@asynccontextmanager
async def local_worker(*, register_worker=True):
    provider, host = Provider(), Host(register_worker=register_worker)
    running = asyncio.create_task(serve_local_inference(host.serve, provider))
    try:
        await asyncio.wait_for(host.ready.wait(), 1)
        yield host, provider, running
    finally:
        provider.release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_local_round_trip_registers_only_infer_and_keeps_the_job_heartbeat_alive():
    async with local_worker() as (host, provider, running):
        assert host.identity == LOCAL_BUB_WORKER_ID == "__local_bub__"
        assert host.register.worker_id == host.identity
        assert not host.register.execute and host.register.recovery is None
        assert host.register.payload == []
        assert host.register.inference == provider.capability()
        call = request()
        pending = asyncio.create_task(host.infer(call))
        await asyncio.wait_for(provider.entered.wait(), 1)
        heartbeat = await asyncio.wait_for(host.messages.get(), 1)
        assert isinstance(heartbeat, Heartbeat)
        assert (heartbeat.job_id, heartbeat.assignment_token) == (call.job_id, call.assignment_token)
        provider.finish.set()
        provider.release.set()
        result = await pending
        assert result.output is not None and result.output.text == "Summary"
        assert result.failure is None and provider.closed.is_set()
        assert not running.done()  # One answer must not stop the controller-managed worker.
        await host.session.close()
        await asyncio.wait_for(running, 1)
        assert host.closed.is_set()


async def test_cancel_is_fenced_single_slot_and_replies_only_after_cleanup():
    # A delayed provider cleanup must neither release the slot nor block the session driver.
    async with local_worker() as (host, provider, _):
        call = request()
        pending = asyncio.create_task(host.infer(call))
        await asyncio.wait_for(provider.entered.wait(), 1)
        await host.cancel(request(job_id=call.job_id + 1))
        await host.cancel(request(token="stale-token"))
        busy = await host.infer(request(job_id=8))
        assert busy.failure.code == "busy" and not provider.cleaning.is_set()
        await host.cancel(call)
        await asyncio.wait_for(provider.cleaning.wait(), 1)
        await host.cancel(call)  # Duplicate delivery cannot interrupt the cleanup await.
        busy = await host.infer(request(job_id=8))
        assert busy.failure.code == "busy" and not pending.done()
        assert len(provider.calls) == 1 and not provider.closed.is_set()
        provider.release.set()
        result = await pending
        assert result.failure.code == "cancelled" and provider.closed.is_set()

        provider.entered.clear()
        again = asyncio.create_task(host.infer(request(token="inference-2")))
        await asyncio.wait_for(provider.entered.wait(), 1)
        await host.cancel(call)  # A prior attempt on the same Job cannot cancel its replacement.
        busy = await host.infer(request(job_id=8))
        assert busy.failure.code == "busy" and not again.done()
        provider.finish.set()
        assert (await again).output.text == "Summary"


@pytest.mark.parametrize("end", ["cancel", "disconnect", "host_failure", "worker_failure"])
async def test_local_teardown_joins_provider_and_both_endpoints_through_repeated_cancel(end):
    initial_tasks = asyncio.all_tasks()
    async with local_worker() as (host, provider, running):
        pending = asyncio.create_task(host.infer(request()))
        await asyncio.wait_for(provider.entered.wait(), 1)
        if end == "cancel":
            running.cancel()
        elif end == "disconnect":
            await host.session.close()
        elif end == "host_failure":
            async def fail():
                raise RuntimeError("host failed")

            async def unused(value):
                raise AssertionError("unreachable")

            host.session.watch("failure", fail, unused)
        else:
            await host.cancel(request(), connection_epoch="wrong-epoch")
        await asyncio.wait_for(provider.cleaning.wait(), 1)
        if end == "cancel":
            for _ in range(2):
                running.cancel()
                await asyncio.sleep(0)
        assert not running.done() and not provider.closed.is_set()
        provider.release.set()
        result, = await asyncio.gather(running, return_exceptions=True)
        if end == "cancel":
            assert isinstance(result, asyncio.CancelledError)
        elif end == "disconnect":
            assert result is None
        else:
            assert isinstance(result, ExceptionGroup)
            assert result.subgroup(RuntimeError if end == "host_failure" else InferenceProtocolError)
        assert provider.closed.is_set() and host.closed.is_set() and host.session.closed
        with pytest.raises(SessionClosed):
            await pending
    assert not asyncio.all_tasks() - initial_tasks


@pytest.mark.parametrize("control", ["infer", "cancel"])
async def test_unready_local_worker_does_not_accept_inference_or_cancel(control):
    async with local_worker(register_worker=False) as (host, provider, running):
        if control == "infer":
            pending = asyncio.create_task(host.infer(request()))
        else:
            pending = None
            await host.cancel(request())
        with pytest.raises(ExceptionGroup) as error:
            await asyncio.wait_for(running, 1)
        assert error.value.subgroup(InferenceProtocolError)
        assert not provider.calls
        if pending is not None:
            with pytest.raises(SessionClosed):
                await pending


@pytest.mark.parametrize("method", ["history", "execute"])
async def test_mini_worker_does_not_mount_history_or_coding_routes(method):
    async with local_worker() as (host, provider, running):
        call = coding_request() if method == "execute" else ConversationQuery(
            **call_header(), method="worker.history",
            arguments=HistoryArguments(session_id="T-019eac3b-a60c-7a93-a442-3718f660fc8c"),
        )
        await host.session.send(ServerMessage(
            **message_header(host.session.epoch), control=None, payload=[call],
        ))
        with pytest.raises(ExceptionGroup) as error:
            await asyncio.wait_for(running, 1)
        assert error.value.subgroup(InferenceProtocolError)
        assert not provider.calls


@pytest.mark.parametrize("recovery", [False, True])
async def test_mini_rejects_repeated_registration_and_coding_recovery(recovery):
    async with local_worker(register_worker=not recovery) as (host, provider, running):
        await host.session.send(Registered(
            **message_header(host.session.epoch), control="registered", payload=[],
            heartbeat_interval=1,
            recovery=RecoveryDecision(action="resume", job_id=5, assignment_token="coding")
            if recovery else None,
        ))
        with pytest.raises(ExceptionGroup) as error:
            await asyncio.wait_for(running, 1)
        assert error.value.subgroup(InferenceProtocolError)
        assert not provider.calls


async def test_mini_rejects_malformed_inference_without_starting_a_provider_or_closing():
    async with local_worker() as (host, provider, running):
        call = request()
        malformed = call.model_copy(update={"arguments": call.arguments.model_copy(update={"messages": []})})
        with pytest.raises(ReplyRejected):
            await host.infer(malformed)
        assert not provider.calls and not running.done()
        provider.finish.set()
        provider.release.set()
        assert (await host.infer(request())).output.text == "Summary"


async def test_ordinary_worker_handles_shared_inference_protocol_failure(tmp_path):
    runtime = WorkerRuntime(WorkerSettings(
        worker_id="ordinary", worker_token=SecretStr("token"), worker_root=tmp_path,
        serve_ws_url="wss://unused",
    ))
    provider = Provider()
    runtime.inference = provider
    host, worker = in_process_transport_pair()
    running = asyncio.create_task(runtime._serve_connection(worker))
    try:
        register = parse_worker_message(await asyncio.wait_for(host.receive_text(), 1))
        assert isinstance(register, Register) and register.execute
        # A shared callback fault still goes through the ordinary worker's protocol lifecycle.
        await host.send_text(ServerMessage(
            **message_header("unready"), control=None, payload=[request()],
        ).model_dump_json())
        with pytest.raises(WorkerProtocolError, match="completed registration"):
            await asyncio.wait_for(running, 1)
        assert not provider.calls and not runtime._connected
        with pytest.raises(SessionClosed):
            await host.receive_text()
    finally:
        await host.close()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await runtime.execution.shutdown()


async def test_coding_slot_and_results_progress_while_inference_cancel_is_cleaning(tmp_path):
    host, provider = Host(), Provider()
    coding_entered, coding_finish, coding_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Executor:
        async def execute(self, task, secret=None, *, tool_caller):
            coding_entered.set()
            try:
                await coding_finish.wait()
                return InitialTaskResult(session_id="coding-session", last_message="Done", exit_code=0)
            finally:
                coding_closed.set()

        async def reclaim_workspace(self):
            raise AssertionError("inference cancellation must not stop coding")

    runtime = WorkerRuntime(WorkerSettings(
        worker_id="ordinary", worker_token=SecretStr("token"), worker_root=tmp_path,
        serve_ws_url="wss://unused",
    ))
    runtime.execution = WorkerExecution(
        worker_id="ordinary", executor=Executor(), state=ExecutionState(tmp_path),
    )
    runtime.inference = provider
    host_transport, worker_transport = in_process_transport_pair()

    async def serve():
        register = parse_worker_message(await host_transport.receive_text())
        assert isinstance(register, Register) and register.execute
        session = Session[WorkerMessageUnion, ServerMessageUnion](
            host_transport, parse=parse_worker_input,
            make_ack=lambda identity: ServerMessage(
                **(message_header(None) | {"ack": True, "ack_id": identity}), control=None, payload=[],
            ), epoch="epoch",
        )
        session.register_control(register)
        session.start()
        await host.serve("ordinary", register, session)

    working = asyncio.create_task(runtime._serve_connection(worker_transport))
    serving = asyncio.create_task(serve())
    pending = coding = None
    try:
        await asyncio.wait_for(host.ready.wait(), 1)
        call = request()
        pending = asyncio.create_task(host.infer(call))
        await asyncio.wait_for(provider.entered.wait(), 1)
        assert runtime.execution.lease() is None  # No coding/Flow binding is required for inference.
        heartbeat = await asyncio.wait_for(host.messages.get(), 1)
        assert isinstance(heartbeat, Heartbeat)
        assert (heartbeat.job_id, heartbeat.assignment_token) == (call.job_id, call.assignment_token)
        assignment = coding_request()
        coding = asyncio.create_task(request_reply(
            host.peers.get("ordinary"), assignment, ExecuteResult,
            ServerMessage(**message_header("epoch"), control=None, payload=[assignment]), timeout=2,
        ))
        await asyncio.wait_for(coding_entered.wait(), 1)
        async with asyncio.timeout(1):
            while True:
                heartbeat = await host.messages.get()
                if isinstance(heartbeat, Heartbeat) and heartbeat.job_id == 5:
                    assert heartbeat.assignment_token == "coding"
                    break
        await host.cancel(call)
        await asyncio.wait_for(provider.cleaning.wait(), 1)
        assert not coding_closed.is_set()
        coding_finish.set()
        terminal, _ = await coding
        assert terminal.result.session_id == "coding-session" and coding_closed.is_set()
        assert not pending.done() and runtime._connected
        assert (await host.infer(request(job_id=8))).failure.code == "busy"
        provider.release.set()
        assert (await pending).failure.code == "cancelled"
        assert runtime.execution.state.assignment is None
        assert runtime._connected
    finally:
        provider.release.set()
        coding_finish.set()
        for task in (working, serving, pending, coding):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (working, serving, pending, coding) if task is not None),
                             return_exceptions=True)
        await runtime.execution.shutdown()


@pytest.mark.parametrize("limit", ["messages", "bytes"])
async def test_transport_backpressure_and_close_wake_both_directions(limit):
    first, second = in_process_transport_pair()
    payload, count = ("x", 32) if limit == "messages" else ("x" * (16 * 1024 * 1024), 1)
    for _ in range(count):
        await first.send_text(payload)
    blocked = asyncio.create_task(first.send_text("next"))
    reading = asyncio.create_task(first.receive_text())
    try:
        await asyncio.sleep(0)
        assert not blocked.done() and not reading.done()
        assert await second.receive_text() == payload
        await asyncio.wait_for(blocked, 1)
        with pytest.raises(SessionOverflow):
            await second.send_text("x" * (16 * 1024 * 1024 + 1))
        await second.close()
        with pytest.raises(SessionClosed):
            await reading
        with pytest.raises(SessionClosed):
            await first.send_text("closed")
    finally:
        await first.close()
        await asyncio.gather(blocked, reading, return_exceptions=True)
