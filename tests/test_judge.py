"""The real bub library must make one isolated, tool-free call and fail conservatively."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from functools import partial
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.contracts.conversation import HistoryQuery
from app.controler.review.judge import ReviewJudge
from app.domain.inference_models import InferenceError
from app.domain.models import CodingExecutionResult, SessionUnavailable, TriageVerdict
from app.inference.bub import BubAgent
from app.worker.coding_agent import CodingAgent
from tests.review_memory import review_context as context


def judge_for(cwd, *, timeout=60, **configuration):
    return ReviewJudge(partial(BubAgent(**configuration).start, cwd=cwd), timeout=timeout)


@asynccontextmanager
async def model_endpoint(*, output=None, status=200, delay=0, tool_calls=None, finish="stop",
                         error=None, retry_after=None):
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
            content_type = "text/event-stream"
            if error is not None:
                body = json.dumps({"error": error})
                content_type = "application/json"
            raw = body.encode()
            headers = f"Retry-After: {retry_after}\r\n" if retry_after is not None else ""
            writer.write(f"HTTP/1.1 {status} Test\r\nContent-Type: {content_type}\r\n{headers}"
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
    {"confidence": 1.1}, {"confidence": "0.9"},
    {"verdict": "resolve"}, {"extra": True}, {"verdict": "revise"},
    {"verdict": "answer", "answer": " "}, {"answer": "Unexpected answer"},
    {"verdict": "answer", "answer": "Reply", "open_points": ["Unresolved change"]},
])
def test_verdict_rejects_invalid_numbers_types_and_action_fields(change):
    with pytest.raises(ValidationError):
        TriageVerdict.model_validate({"verdict": "wait", "confidence": 0.5, **change})


async def test_real_bub_contract_is_stateless_and_has_no_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("BUB_MODEL", "malicious:override")
    monkeypatch.setenv("BUB_FALLBACK_MODELS", '["malicious:fallback"]')
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    output = json.dumps({"verdict": "answer", "confidence": 0.9,
                         "instruction": "Explain the configured retry limit."})
    async with model_endpoint(output=output) as (url, calls):
        judge = judge_for(tmp_path, model="openai:test", api_key=SecretStr("fake-key"), api_base=url)
        first = await judge.triage(context())
        second = await judge.triage(context(2))
    assert first.failure is None
    assert first.verdict.instruction == "Explain the configured retry limit."
    assert first.verdict.answer == ""
    assert first.context_digest == context().digest
    assert first.session_id != second.session_id
    assert len(calls) == 2
    for call in calls:
        assert not call.get("tools")
        assert [m["role"] for m in call["messages"]] == ["user", "system"]
        assert "schema" in call["messages"][-1]["content"]
        assert "must-not-leak" not in json.dumps(call)


@pytest.mark.parametrize("output", ["not JSON",
    '{"verdict":"revise","confidence":NaN,"instruction":"Edit"}',
    '{"verdict":"ignore","verdict":"answer","answer":"bad"}',
    '{"verdict":"answer","answer":"A final answer is no longer Judge output"}',
    '{"verdict":"answer","instruction":" "}',
])
async def test_bad_model_output_waits_without_retries(output, tmp_path):
    async with model_endpoint(output=output) as (url, calls):
        decision = await judge_for(tmp_path, model="openai:test", api_key=SecretStr("fake"),
                                  api_base=url).triage(context())
    assert decision.verdict.verdict == "wait"
    assert decision.failure == "invalid_response"
    assert len(calls) == 1


async def test_missing_configuration_never_starts_a_process(monkeypatch, tmp_path):
    async def forbidden(*args, **kwargs):
        pytest.fail("Missing configuration must not launch bub")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    decision = await judge_for(tmp_path, model="openai:test").triage(context())
    assert decision.failure == "configuration"


async def test_provider_returns_one_sanitized_failure_without_owning_retries(tmp_path):
    async with model_endpoint(status=500, output="secret-diagnostic") as (url, calls):
        with pytest.raises(InferenceError) as caught:
            await judge_for(tmp_path, model="openai:test", api_key=SecretStr("fake"),
                            api_base=url).triage(context())
    assert caught.value.failure.code == "backend_unavailable"
    assert len(calls) == 1
    assert "secret-diagnostic" not in caught.value.failure.model_dump_json()


@pytest.mark.parametrize("status,code,retry_after,failure,delay", [
    (401, "invalid_api_key", None, "authentication", None),
    (403, "forbidden", None, "authentication", None),
    (429, "insufficient_quota", "3", "configuration", None),
    (429, "rate_limit_exceeded", "3", "rate_limited", 3),
    (429, "rate_limit_exceeded", "NaN", "rate_limited", None),
    (429, "opaque", "3", "backend_error", None),
    (503, "server_error", "4", "backend_unavailable", 4),
])
async def test_real_sdk_failure_classification_and_retry_after(
    tmp_path, status, code, retry_after, failure, delay,
):
    async with model_endpoint(status=status, retry_after=retry_after,
        error={"code": code, "message": "secret-provider-diagnostic"}) as (url, calls):
        agent = BubAgent(model="openai:test", api_key=SecretStr("fake"), api_base=url)
        result = await agent.start("Answer only", tmp_path)
    assert result.exit_code != 0
    assert result.failure_kind == failure
    assert result.retry_after_seconds == delay
    assert result.backend == "bub" and result.model == "openai:test"
    assert len(calls) == 1
    assert "secret-provider-diagnostic" not in json.dumps(asdict(result))


async def test_real_sdk_start_accepts_a_plain_prompt_and_model_override(tmp_path):
    async with model_endpoint(output="Plain answer") as (url, calls):
        agent = BubAgent(model="openai:default", api_key=SecretStr("fake"), api_base=url)
        result = await agent.start("A plain prompt", tmp_path, model="openai:override")
    assert result.exit_code == 0 and result.last_message == "Plain answer"
    assert result.model == "openai:override"
    assert calls[0]["model"] == "override"
    assert calls[0]["messages"] == [{"role": "user", "content": "A plain prompt"}]
    assert not list(tmp_path.iterdir())


async def test_importing_the_agent_does_not_import_the_sdk(tmp_path):
    child = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c",
        "import sys; import app.inference.bub; "
        "assert not {'bub', 'openai', 'anthropic', 'app.inference.runner'} & sys.modules.keys()",
        cwd=tmp_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await child.communicate()
    assert child.returncode == 0, stderr.decode()


async def test_bub_explicitly_rejects_resume_and_history(tmp_path):
    agent = BubAgent()
    session = "00000000-0000-0000-0000-000000000001"
    with pytest.raises(SessionUnavailable):
        await agent.resume(session, "Continue", cwd=tmp_path)
    with pytest.raises(NotImplementedError):
        agent.read_history(HistoryQuery(session_id=session))


async def test_incomplete_context_waits_without_model_call(tmp_path):
    snapshot = context().model_copy(update={"limitations": ["files:truncated"]})
    decision = await judge_for(tmp_path).triage(snapshot)
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


async def test_child_output_and_exit_contract(monkeypatch, tmp_path):
    child = ChildProcess(err=b"secret", code=1)
    provider = BubAgent(model="openai:test", api_key=SecretStr("fake"))
    existing = tmp_path / "existing.txt"
    existing.write_text("Do not replace the caller's workspace")

    async def start(*args, **kwargs):
        assert args[1:4] == ("-I", "-m", "app.inference.runner")
        assert Path(kwargs["cwd"]) == tmp_path
        assert Path(kwargs["cwd"]).is_dir()
        assert set(kwargs["env"]) == {
            "PATH", "LANG", "FLOWTOOL_JUDGE_MODEL", "FLOWTOOL_JUDGE_API_KEY",
        }
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    result = await ReviewJudge(partial(provider.start, cwd=tmp_path)).triage(context())
    assert result.failure == "backend_error"
    assert child.waited
    assert not child.killed
    request = json.loads(child.input)
    assert set(request) == {"prompt", "output_schema", "max_output_bytes"}
    assert request["output_schema"]["type"] == "object"
    assert list(tmp_path.iterdir()) == [existing]


@pytest.mark.parametrize("text,schema,limit,failure", [
    ("Plain answer", None, 64, None),
    ('{"ok":true}', {"type": "object"}, 64, None),
    ("not JSON", {}, 64, "invalid_response"),
    ('{"value":NaN}', {}, 64, "invalid_response"),
    ("é" * 10, None, 12, "output_limit"),
])
async def test_bub_reuses_shared_final_answer_validation(
    monkeypatch, tmp_path, text, schema, limit, failure,
):
    # SDK answers must cross the same strict JSON and UTF-8 bounds as native CLI answers.
    child = ChildProcess(out=json.dumps(asdict(CodingExecutionResult(None, text, 0))).encode())

    async def start(*args, **kwargs):
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    agent = BubAgent(model="openai:test", api_key=SecretStr("fake"))
    assert BubAgent.start is CodingAgent.start
    result = await agent.start("Answer only", tmp_path, output_schema=schema, max_output_bytes=limit)
    assert result.failure_kind == failure
    assert result.last_message == ("" if failure else text)
    assert child.waited and not list(tmp_path.iterdir())


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_kill_and_reap_child(monkeypatch, tmp_path, cancel):
    child = ChildProcess(code=None)
    started = asyncio.Event()

    async def start(*args, **kwargs):
        return child

    async def exchange(*args):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    monkeypatch.setattr("app.inference.bub._exchange", exchange)
    provider = BubAgent(model="openai:test", api_key=SecretStr("fake"))
    task = asyncio.create_task(provider.start("Answer only", tmp_path,
        timeout_seconds=None if cancel else 0))
    await started.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).failure_kind == "timeout"
    assert child.killed and child.waited


async def test_process_start_error_is_sanitized(monkeypatch, tmp_path):
    async def start(*args, **kwargs):
        raise OSError("secret-path")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    result = await judge_for(tmp_path, model="openai:test", api_key=SecretStr("fake")).triage(context())
    assert result.failure == "configuration"
    assert "secret-path" not in result.model_dump_json()


async def test_real_flooded_pipe_is_drained_when_child_is_killed(monkeypatch, tmp_path):
    original = asyncio.create_subprocess_exec
    children = []

    async def start(*args, **kwargs):
        # A low transport watermark makes the paused-pipe race deterministic on macOS and Linux.
        child = await original(sys.executable, "-c",
            "import os\nwhile True: os.write(2, b'x' * 65536)", limit=1024, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    task = asyncio.create_task(judge_for(tmp_path, model="openai:test", api_key=SecretStr("fake"),
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
    assert task.result().failure == "output_limit"
    assert children[0].returncode is not None


@pytest.mark.parametrize("tool_calls,finish", [
    ([{"index": 0, "id": "call", "type": "function", "function": {
        "name": "shell", "arguments": '{"command":"touch forbidden"}',
    }}], "tool_calls"), (None, "length"),
])
async def test_tool_requests_and_unfinished_model_messages_never_become_decisions(
    tool_calls, finish, tmp_path,
):
    async with model_endpoint(output='{"verdict":"ignore"}', tool_calls=tool_calls,
                              finish=finish) as (url, calls):
        result = await judge_for(tmp_path, model="openai:test", api_key=SecretStr("fake"),
                                api_base=url).triage(context())
    assert result.failure == "invalid_response"
    assert len(calls) == 1
