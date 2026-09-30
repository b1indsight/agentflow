"""The real bub library must make one isolated, tool-free call and fail conservatively."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager

import pytest
from pydantic import SecretStr, ValidationError

from app.domain.models import TriageVerdict
from app.worker.judge import BubJudge
from tests.review_memory import review_context as context


@asynccontextmanager
async def model_endpoint(*, output=None, status=200, delay=0, tool_calls=None, finish="stop"):
    requests = []
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in header.splitlines()
                          if line.lower().startswith(b"content-length:"))
            requests.append(json.loads(await reader.readexactly(length)))
            await asyncio.sleep(delay)
            chunks = [
                {"id": "test", "object": "chat.completion.chunk", "created": 1,
                 "model": "test", "choices": [{"index": 0, "delta": {"content": output},
                                               "finish_reason": None}]},
                {"id": "test", "object": "chat.completion.chunk", "created": 1,
                 "model": "test", "choices": [{"index": 0, "delta": {},
                                               "finish_reason": finish}]},
            ]
            if tool_calls:
                chunks[0]["choices"][0]["delta"]["tool_calls"] = tool_calls
            body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            body += "data: [DONE]\n\n"
            raw = body.encode()
            writer.write(f"HTTP/1.1 {status} Test\r\nContent-Type: text/event-stream\r\n"
                         f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode())
            writer.write(raw)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1", requests
    finally:
        server.close()
        await server.wait_closed()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("change", [
    {"confidence": -0.1}, {"confidence": 1.1}, {"confidence": float("nan")},
    {"confidence": float("inf")}, {"confidence": "0.9"}, {"confidence": True},
    {"verdict": "resolve"}, {"extra": True}, {"verdict": "revise"},
    {"verdict": "answer", "answer": " "}, {"answer": "Unexpected answer"},
    {"verdict": "answer", "answer": "Reply", "open_points": ["Unresolved change"]},
])
def test_verdict_rejects_invalid_numbers_types_and_action_fields(change):
    with pytest.raises(ValidationError):
        TriageVerdict.model_validate({"verdict": "wait", "confidence": 0.5, **change})


async def test_real_bub_contract_is_stateless_and_has_no_tools(monkeypatch):
    monkeypatch.setenv("BUB_MODEL", "malicious:override")
    monkeypatch.setenv("BUB_FALLBACK_MODELS", '["malicious:fallback"]')
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    output = json.dumps({"verdict": "answer", "confidence": 0.9,
                         "answer": "The configured retry limit is 10."})
    async with model_endpoint(output=output) as (url, calls):
        judge = BubJudge(model="openai:test", api_key=SecretStr("fake-key"), api_base=url)
        first = await judge.triage(context())
        second = await judge.triage(context(2))
    assert first.failure is None
    assert first.verdict.answer == "The configured retry limit is 10."
    assert first.context_digest == context().digest
    assert first.session_id != second.session_id
    assert len(calls) == 2
    for call in calls:
        assert not call.get("tools")
        assert len(call["messages"]) == 2
        assert [m["role"] for m in call["messages"]] == ["system", "user"]
        assert "must-not-leak" not in json.dumps(call)


@pytest.mark.parametrize("output", ["", "log\n{}", "{}{}", "not JSON",
    '{"verdict":"revise","confidence":NaN,"instruction":"Edit"}',
    '{"verdict":"ignore","verdict":"answer","answer":"bad"}',
])
async def test_bad_model_output_waits_without_retries(output):
    async with model_endpoint(output=output) as (url, calls):
        decision = await BubJudge(model="openai:test", api_key=SecretStr("fake"),
                                  api_base=url).triage(context())
    assert decision.verdict.verdict == "wait"
    assert decision.failure == "invalid-verdict"
    assert len(calls) == 1


async def test_missing_configuration_never_starts_a_process(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Missing configuration must not launch bub")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    decision = await BubJudge().triage(context())
    assert decision.failure == "missing-configuration"


async def test_model_error_is_one_request_and_sanitized():
    async with model_endpoint(status=500, output="secret-diagnostic") as (url, calls):
        decision = await BubJudge(model="openai:test", api_key=SecretStr("fake"),
                                  api_base=url).triage(context())
    assert decision.failure == "model-error"
    assert len(calls) == 1
    assert "secret-diagnostic" not in decision.model_dump_json()


async def test_incomplete_context_waits_without_model_call():
    snapshot = context().model_copy(update={"limitations": ["files:truncated"]})
    decision = await BubJudge().triage(snapshot)
    assert decision.failure == "incomplete-context"


class ChildProcess:
    """Controllable pipes exercise the parent lifecycle without real sleeps or leaked children."""

    def __init__(self, out=b"", err=b"", code=0):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdin = self
        self.returncode = code
        self.killed = False
        self.waited = False
        self.stdout.feed_data(out)
        self.stderr.feed_data(err)
        if code is not None:
            self.stdout.feed_eof()
            self.stderr.feed_eof()

    def write(self, data):
        self.input = data

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self):
        self.waited = True
        return self.returncode


@pytest.mark.parametrize("out,err,code,failure", [
    (b"", b"", 0, "empty-output"), (b"{}", b"secret", 1, "nonzero-exit"),
    (b"log\n{}", b"", 0, "invalid-envelope"), (b"{}{}", b"", 0, "invalid-envelope"),
    (b"x" * 64_001, b"", None, "output-limit"),
    (b"", b"x" * 16_001, None, "output-limit"),
])
async def test_child_output_and_exit_contract(monkeypatch, out, err, code, failure):
    child = ChildProcess(out, err, code)

    async def start(*args, **kwargs):
        assert args[1:4] == ("-I", "-m", "app.worker.judge_runner")
        assert set(kwargs["env"]) == {
            "PATH", "LANG", "FLOWTOOL_JUDGE_MODEL", "FLOWTOOL_JUDGE_API_KEY", "BUB_HOME",
        }
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    result = await BubJudge(model="openai:test", api_key=SecretStr("fake")).triage(context())
    assert result.failure == failure
    assert child.waited
    assert child.killed is (code is None)


@pytest.mark.parametrize("error,failure", [(TimeoutError, "timeout"), (asyncio.CancelledError, None)])
async def test_timeout_and_cancellation_kill_and_reap_child(monkeypatch, error, failure):
    child = ChildProcess(code=None)

    async def start(*args, **kwargs):
        return child

    async def exchange(*args):
        raise error

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    monkeypatch.setattr("app.worker.judge._exchange", exchange)
    judge = BubJudge(model="openai:test", api_key=SecretStr("fake"))
    if failure:
        assert (await judge.triage(context())).failure == failure
    else:
        with pytest.raises(asyncio.CancelledError):
            await judge.triage(context())
    assert child.killed and child.waited


async def test_process_start_error_is_sanitized(monkeypatch):
    async def start(*args, **kwargs):
        raise OSError("secret-path")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    result = await BubJudge(model="openai:test", api_key=SecretStr("fake")).triage(context())
    assert result.failure == "start-failed"
    assert "secret-path" not in result.model_dump_json()


@pytest.mark.parametrize("fd", [1, 2])
async def test_real_flooded_pipe_is_drained_when_child_is_killed(monkeypatch, fd):
    original = asyncio.create_subprocess_exec
    children = []

    async def start(*args, **kwargs):
        # A low transport watermark makes the paused-pipe race deterministic on macOS and Linux.
        child = await original(sys.executable, "-c",
            f"import os\nwhile True: os.write({fd}, b'x' * 65536)", limit=1024, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    task = asyncio.create_task(BubJudge(model="openai:test", api_key=SecretStr("fake"),
                                        timeout=10).triage(context()))
    done, _ = await asyncio.wait({task}, timeout=3)
    if not done:
        # Keep a failing regression test from leaking the old implementation's blocked process.
        for child in children:
            if child.returncode is None:
                child.kill()
            await asyncio.gather(child.stdout.read(), child.stderr.read())
        await task
    assert done, "Judge hung waiting for a killed child with a paused output transport"
    assert task.result().failure == "output-limit"
    assert children[0].returncode is not None


@pytest.mark.parametrize("tool_calls,finish", [
    ([{"index": 0, "id": "call", "type": "function", "function": {
        "name": "shell", "arguments": '{"command":"touch forbidden"}',
    }}], "tool_calls"), (None, "length"),
])
async def test_tool_requests_and_unfinished_model_messages_never_become_decisions(tool_calls, finish):
    async with model_endpoint(output='{"verdict":"ignore"}', tool_calls=tool_calls,
                              finish=finish) as (url, calls):
        result = await BubJudge(model="openai:test", api_key=SecretStr("fake"),
                                api_base=url).triage(context())
    assert result.failure == "invalid-verdict"
    assert len(calls) == 1


async def test_runner_does_not_instantiate_framework_or_tape(monkeypatch):
    from bub.builtin.tape import Tape
    from bub.framework import BubFramework
    from bub.tools import ToolExecutor

    from app.domain.judge_models import JudgeRequest
    from app.worker.judge_runner import run

    def forbidden(*args, **kwargs):
        pytest.fail("Judge must not enter plugin, tape or tool execution paths")

    monkeypatch.setattr(BubFramework, "__init__", forbidden)
    monkeypatch.setattr(Tape, "__init__", forbidden)
    monkeypatch.setattr(ToolExecutor, "__init__", forbidden)
    monkeypatch.setenv("FLOWTOOL_JUDGE_MODEL", "openai:test")
    monkeypatch.setenv("FLOWTOOL_JUDGE_API_KEY", "fake")
    async with model_endpoint(output='{"verdict":"wait","reason":"Still discussing"}') as (url, _):
        monkeypatch.setenv("FLOWTOOL_JUDGE_API_BASE", url)
        result = await run(JudgeRequest(session_id="test", context=context()))
    assert result.failure is None
