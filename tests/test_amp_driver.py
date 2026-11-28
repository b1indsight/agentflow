"""Amp's public CodingAgent contract, without network or model calls."""

import asyncio
import base64
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.contracts.conversation import ConversationFailure, HistoryQuery
from app.domain.models import CodingExecutionContext, GitHubCapability
from app.worker.execution.amp_agent import AmpCodingAgent, AmpProcessResult, amp_environment
from app.worker.execution.git_identity import configure_git_identity
from app.worker.execution.prompts import build_revision_retry_prompt
from tests.flow_fixtures import PNG_IMAGE

THREAD = "T-01a15d70-eb3d-722c-9f6d-563dd65eb0a8"


async def test_worker_pages_captured_turns_and_repairs_only_from_a_covering_export(tmp_path, monkeypatch):
    # The CLI echoes stdin and repeats the final assistant text in result. Neither is a new message.
    root = tmp_path / "history"
    agent = AmpCodingAgent(trusted_host=True, history_root=root)
    query = HistoryQuery(session_id=THREAD, limit=1)
    native: list[dict] = []
    exports: list[dict] | None = None

    async def run(args, **kw):
        if args[:2] == ['threads', 'export']:
            assert exports is not None, 'a healthy local history must not export'
            kw['stream_stdout'](json.dumps({'id': THREAD, 'messages': exports}).encode())
            return AmpProcessResult(b'', b'', 0)
        prompt = kw['input_text']
        answer = 'same reply'
        init, final = stream(result=answer).splitlines()
        rows = [init]
        for role, text in [('user', prompt), ('assistant', answer)]:
            blocks = [{'type': 'text', 'text': text}]
            rows.append(json.dumps({'type': role, 'message': {'content': blocks}}).encode())
            native.append({'role': role, 'protocolMessageID': str(len(native)), 'content': blocks})
        rows.append(final)
        data = b'\n'.join(rows) + b'\n'
        for offset in range(0, len(data), 71):
            kw['observe_stdout'](data[offset:offset+71])
        return AmpProcessResult(data, b'', 0)

    monkeypatch.setattr(AmpCodingAgent, 'run_process', staticmethod(run))
    await agent.start('first', tmp_path, context=CodingExecutionContext())
    cursor = (await asyncio.to_thread(agent.read_history, query)).next_cursor
    await agent.resume(THREAD, 'second', cwd=tmp_path)
    await agent.resume(THREAD, 'third', cwd=tmp_path)
    reader = AmpCodingAgent(history_root=root)
    full = query.model_copy(update={'limit': 100})
    page = await asyncio.to_thread(reader.read_history, full)
    assert [item.role for item in page.items] == ['user', 'assistant'] * 3
    assert [item.content.text for item in page.items[1::2]] == ['same reply'] * 3

    path = root / f'{THREAD}.jsonl'
    lines = path.read_bytes().splitlines(keepends=True)
    damaged = b''.join(lines[:2] + lines[4:])
    path.write_bytes(damaged)
    exports = native[:2] + native[4:]
    with pytest.raises(ConversationFailure, match='history_unreadable'):
        await asyncio.to_thread(reader.read_history, full)
    assert path.read_bytes() == damaged
    exports = native
    with pytest.raises(ConversationFailure, match='cursor_invalid'):
        await asyncio.to_thread(reader.read_history, HistoryQuery(session_id=THREAD, cursor=cursor))
    exports = None
    repaired = await asyncio.to_thread(reader.read_history, full)
    assert [(i.id, i.content) for i in repaired.items] == [(i.id, i.content) for i in page.items]


async def test_amp_backfill_matches_call_and_result_before_assigning_text_offsets(tmp_path, monkeypatch):
    # Recovery no longer has the capture-time credential. The public tool result must stay redacted.
    agent = AmpCodingAgent(trusted_host=True, history_root=tmp_path)
    secret = 'ephemeral-fixture-value'
    query = HistoryQuery(session_id=THREAD)
    rows: list[dict] = []

    async def run(args, **kw):
        if args[:2] == ['threads', 'export']:
            kw['stream_stdout'](json.dumps({'id': THREAD, 'messages': rows}).encode())
            return AmpProcessResult(b'', b'', 0)
        init, final = stream(result='C').splitlines()
        events = []
        for role, block in [
            ('user', {'type': 'text', 'text': kw['input_text']}),
            ('assistant', {'type': 'tool_use', 'id': 'TU-X', 'name': 'shell', 'input': {}}),
            ('user', {'type': 'tool_result', 'tool_use_id': 'TU-X', 'content': json.dumps(secret)}),
            ('assistant', {'type': 'text', 'text': 'A'}),
            ('assistant', {'type': 'text', 'text': 'C'}),
        ]:
            events.append(json.dumps({'type': role, 'message': {'content': [block]}}).encode())
            if block['type'] == 'tool_result':
                block = {'type': 'tool_result', 'toolUseID': 'TU-X', 'run': {'result': secret}}
            rows.append({'role': role, 'protocolMessageID': str(len(rows)), 'content': [block]})
        data = b'\n'.join([init, *events, final, b''])
        kw['observe_stdout'](data)
        return AmpProcessResult(data, b'', 0)

    monkeypatch.setenv('AMP_API_KEY', secret)
    monkeypatch.setattr(AmpCodingAgent, 'run_process', staticmethod(run))
    await agent.start('begin', tmp_path, context=CodingExecutionContext())
    original = (await asyncio.to_thread(agent.read_history, query)).items
    monkeypatch.delenv('AMP_API_KEY')
    path = tmp_path / f'{THREAD}.jsonl'
    damaged = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b''.join(damaged[:-1]))
    call = rows.pop(1)  # A result alone cannot cover the missing call with the same TU ID.
    with pytest.raises(ConversationFailure, match='history_unreadable'):
        await asyncio.to_thread(agent.read_history, query)
    rows.insert(1, call)
    rows.insert(-1, dict(rows[-1], protocolMessageID='extra'))  # two equally plausible C's
    with pytest.raises(ConversationFailure, match='history_unreadable'):
        await asyncio.to_thread(agent.read_history, query)
    assert path.read_bytes() == b''.join(damaged[:-1])
    rows[-2]['content'] = [{'type': 'text', 'text': 'B'}]
    repaired = (await asyncio.to_thread(agent.read_history, query)).items
    assert [i.id for i in repaired[:4] + repaired[-1:]] == [i.id for i in original]
    assert [i.content.text for i in repaired[3:]] == ['A', 'B', 'C']
    assert [i.id for i in repaired[3:]] == [f'{THREAD}:r-TU-X/{n}' for n in (0, 2, 1)]
    assert repaired[1].id == f'{THREAD}:c-TU-X' and repaired[2].id == f'{THREAD}:r-TU-X'
    assert secret.encode() not in path.read_bytes()
    assert repaired[2].content == original[2].content


def stream(*, result=None, session=THREAD, tools=None, **overrides):
    final = {"type": "result", "subtype": "success", "is_error": False,
             "session_id": session,
             "result": '{"caller_owned":true}' if result is None else result} | overrides
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in [
        {"type": "system", "subtype": "init", "session_id": THREAD,
         "tools": ["shell_command"] if tools is None else tools}, final,
    ]).encode()


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("model,effort", [
    (None, None), ("anthropic/claude-opus-5-5", None), ("anthropic/claude-opus-5-5", "xhigh"),
])
async def test_amp_uses_current_capabilities_and_raw_caller_schema(monkeypatch, tmp_path, resume, model, effort):
    # Native workspace/user config and custom MCP credentials must survive every round.
    settings = tmp_path / ".amp" / "settings.jsonc"
    settings.parent.mkdir()
    native_settings = '{// repository policy\n"amp.mcpPermissions":[]}'
    settings.write_text(native_settings)
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q"], cwd=tmp_path, check=True)
    await configure_git_identity(tmp_path, "agentflow[bot]", 7654321)
    monkeypatch.delenv("AMP_DISABLE_AMP_COAUTHOR_TRAILER", raising=False)
    monkeypatch.setenv("AMP_SETTINGS_FILE", str(tmp_path / "user.jsonc"))
    monkeypatch.setenv("EXAMPLE_MCP_TOKEN", "custom-mcp-token")
    monkeypatch.setenv("AGENTFLOW_MCP_TOKEN", "stale-broker-token")
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object","required":["caller_owned"]}')
    context = CodingExecutionContext(
        github=GitHubCapability(SecretStr("bot-secret"), "owner/repo",
                                datetime.now(UTC) + timedelta(minutes=5), "agentflow[bot]", 7654321),
        mcp_url=None if resume else "http://127.0.0.1:12345/mcp",
        mcp_token=None if resume else SecretStr("broker-secret"),
        images=(PNG_IMAGE,),
    )
    directories = []

    def github(capability, directory, source):
        directories.append(directory)
        assert capability == context.github
        return {"GH_TOKEN": "bot-secret", "GH_CONFIG_DIR": str(directory / "config")}

    async def execute(args, *, cwd, env, input_text, **kwargs):
        assert cwd == tmp_path
        assert "--no-archive-after-execute" in args
        assert "bot-secret" not in " ".join(args) and "broker-secret" not in input_text
        assert "caller_owned" in input_text and "task" in input_text
        assert "opt out of automatic GitHub follow-ups" in input_text
        instructions, _, request = input_text.partition("\n]]>\n</agentflow_context>")
        assert request == ("\n\n# Current request\ntask" if resume else "")
        assert Path(instructions.splitlines()[-1]).read_bytes() == base64.b64decode(PNG_IMAGE.split(",")[1])
        assert env["GH_TOKEN"] == env["AGENTFLOW_PUSH_TOKEN"] == "bot-secret"
        assert env["EXAMPLE_MCP_TOKEN"] == "custom-mcp-token"
        assert env["AMP_SETTINGS_FILE"] == str(tmp_path / "user.jsonc")
        assert env["AMP_DISABLE_AMP_COAUTHOR_TRAILER"] == "1"
        assert "--settings-file" not in args
        servers = json.loads(args[args.index("--mcp-config") + 1])
        assert set(servers) == {"flow"}
        broker = servers["flow"]
        if resume:
            assert args[:3] == ["threads", "continue", THREAD]
            assert "--mode" not in args and "--model" not in args and "--effort" not in args
            assert broker == {"url": "http://127.0.0.1:0/mcp"}
            assert "AGENTFLOW_MCP_TOKEN" not in env
        else:
            if model is None:
                assert args[args.index("--mode") + 1] == "low"
                assert "--model" not in args
            else:
                # A concrete model must not compete with the old default --mode low.
                assert args[args.index("--model") + 1] == model
                assert "--mode" not in args
            if effort is None:
                assert "--effort" not in args
            else:
                assert args[args.index("--effort") + 1] == effort
            assert broker["url"] == context.mcp_url
            assert broker["headers"]["Authorization"] == "Bearer ${AGENTFLOW_MCP_TOKEN}"
            assert env["AGENTFLOW_MCP_TOKEN"] == "broker-secret"
        return AmpProcessResult(stream(result='{"caller_owned":"bot-secret"}', tools=[
            "shell_command", "skill", "tool_search", "code_exec", "mcp__example__lookup",
        ]), b"", 0)

    monkeypatch.setattr("app.worker.execution.amp_agent.github_environment", github)
    monkeypatch.setattr(AmpCodingAgent, "run_process", staticmethod(execute))
    agent = AmpCodingAgent(trusted_host=True)
    result = (await agent.resume(THREAD, build_revision_retry_prompt("retry", "task"), cwd=tmp_path, output_schema=schema, context=context,
                                 model=model, effort=effort)
              if resume else await agent.start("task", tmp_path, output_schema=schema, context=context,
                                              model=model, effort=effort))
    assert result.session_id == THREAD and result.exit_code == 0
    assert result.backend == "amp" and result.model == (None if resume else model)
    assert result.last_message == '{"caller_owned":"[REDACTED]"}'
    assert settings.read_text() == native_settings
    assert all(not directory.exists() for directory in directories)


async def test_jsonl_keeps_unicode_separators_inside_the_final_answer(monkeypatch, tmp_path):
    # These are valid JSON string characters, not JSONL record delimiters.
    answer = json.dumps({"caller_owned": "a\u0085b\u2028c\u2029d"}, ensure_ascii=False)

    async def execute(*args, **kwargs):
        return AmpProcessResult(stream(result=answer), b"", 0)

    monkeypatch.setattr(AmpCodingAgent, "run_process", staticmethod(execute))
    result = await AmpCodingAgent(trusted_host=True).start("task", tmp_path)
    assert result.exit_code == 0 and result.last_message == answer


async def test_final_json_redacts_credentials_after_escaped_separators(monkeypatch, tmp_path):
    # JSON encodes the newline as \\n; redaction must still see the token's real boundary.
    monkeypatch.setenv("AMP_API_KEY", "current-private-key")
    payload = {"summary": "line\nsgamp_external-client_123456789",
               "nested": [{"key": "line\tghp_external_client_123456789"},
                          "line\rcurrent-private-key"],
               "path": "task-abcdefghijklmnop", "count": 2, "ok": True}

    async def execute(*args, **kwargs):
        return AmpProcessResult(stream(result=json.dumps(payload)), b"", 0)

    monkeypatch.setattr(AmpCodingAgent, "run_process", staticmethod(execute))
    result = await AmpCodingAgent(trusted_host=True).start("task", tmp_path)
    assert result.exit_code == 0
    assert json.loads(result.last_message) == {
        "summary": "line\n[REDACTED]",
        "nested": [{"key": "line\t[REDACTED]"}, "line\r[REDACTED]"],
        "path": "task-abcdefghijklmnop", "count": 2, "ok": True,
    }


@pytest.mark.parametrize("text", ["7" * 5000, "[" * 1200 + "0" + "]" * 1200],
                         ids=["long-number", "deep-bracket-text"])
async def test_final_answer_keeps_text_that_exceeds_json_decoder_limits(monkeypatch, tmp_path, text):
    # A successful answer can quote long numbers or bracket text without being a JSON document.
    async def execute(*args, **kwargs):
        return AmpProcessResult(stream(result=json.dumps({"summary": text})), b"", 0)

    monkeypatch.setattr(AmpCodingAgent, "run_process", staticmethod(execute))
    result = await AmpCodingAgent(trusted_host=True).start("task", tmp_path)
    assert result.exit_code == 0 and json.loads(result.last_message) == {"summary": text}


async def test_empty_success_and_failed_result_without_answer_keep_native_contract(monkeypatch, tmp_path):
    output = stream(result="")

    async def execute(*args, **kwargs):
        return AmpProcessResult(output, b"", 0)

    monkeypatch.setattr(AmpCodingAgent, "run_process", staticmethod(execute))
    agent = AmpCodingAgent(trusted_host=True)
    result = await agent.start("task", tmp_path)
    assert result.exit_code == 0 and result.last_message == ""

    events = [json.loads(line) for line in stream(is_error=True, error="User cancelled").split(b"\n")]
    events[-1].pop("result")
    output = "\n".join(json.dumps(event) for event in events).encode()
    result = await agent.resume(THREAD, "task", cwd=tmp_path)
    assert result.exit_code != 0 and result.failure_detail == "User cancelled"


@pytest.mark.parametrize("stdout,exit_code", [
    (stream(is_error=True, subtype="error_during_execution", error="User cancelled"), 0),
    (stream(is_error="false"), 0),
    (stream(is_error=0), 0),
    (stream(tools=["shell_command", 1]), 0),
    (stream(tools="shell_command"), 0),
    (stream(error=None), 0),
    (stream(result=1), 0),
    (stream(result={"caller_owned": True}), 0),
    (stream(subtype=1), 0),
    (stream(session="T-00000000-0000-0000-0000-000000000000"), 0),
    (stream() + b'\n{"type":"result"}', 0),
    (b'{"type":"assistant"}\n', 0),
    (b'not json\n' + stream(), 0),
    (stream(tools=["skill", "mcp__example__lookup"]), 0),
    (stream(), 1),
])
async def test_incomplete_or_failed_execution_never_succeeds(monkeypatch, tmp_path, stdout, exit_code):
    async def execute(*args, **kwargs):
        return AmpProcessResult(stdout, b"", exit_code)

    monkeypatch.setattr(AmpCodingAgent, "run_process", staticmethod(execute))
    result = await AmpCodingAgent(trusted_host=True).resume(THREAD, "task", cwd=tmp_path)
    assert result.exit_code != 0 and result.failure_detail


async def test_unproven_missing_thread_does_not_trigger_replay(monkeypatch, tmp_path):
    # A single observed stderr spelling cannot establish all authentication/network cases.
    monkeypatch.setenv("AMP_API_KEY", "current-test-token")

    async def execute(*args, **kwargs):
        stderr = ("startup diagnostic\n" * 300
                  + f"Thread https://ampcode.com/threads/{THREAD} does not exist.\n"
                  + "AMP_API_KEY=current-test-token other=sgamp_old-client_ABC\n")
        return AmpProcessResult(b"", stderr.encode(), 1)

    monkeypatch.setattr(AmpCodingAgent, "run_process", staticmethod(execute))
    result = await AmpCodingAgent(trusted_host=True).resume(THREAD, "task", cwd=tmp_path)
    assert result.exit_code != 0
    assert result.failure_detail and "does not exist" in result.failure_detail
    assert len(result.failure_detail) <= 4000
    assert "current-test-token" not in result.failure_detail
    assert "sgamp_old-client_ABC" not in result.failure_detail
    for invalid_session in ("../thread", THREAD.upper(), THREAD[2:]):
        with pytest.raises(ValueError, match="Amp thread"):
            await AmpCodingAgent(trusted_host=True).resume(invalid_session, "task", cwd=tmp_path)
    with pytest.raises(RuntimeError, match="trusted-host"):
        await AmpCodingAgent().start("task", tmp_path)


def test_environment_keeps_amp_login_but_excludes_ambient_credentials():
    environment = amp_environment({"HOME": "/worker", "AMP_API_KEY": "amp-secret",
                                   "PATH": "/usr/bin", "GIT_AUTHOR_NAME": "bot",
                                   "AGENTFLOW_WORKER_TOKEN": "worker-secret",
                                   "GH_TOKEN": "personal", "OPENAI_API_KEY": "other",
                                   "AMP_URL": "https://amp.example",
                                   "GIT_CONFIG_PARAMETERS": "personal-credentials",
                                   "SSH_AUTH_SOCK": "/personal/ssh-agent",
                                   "AMP_CURRENT_THREAD_ID": "parent-thread"})
    assert environment["HOME"] == "/worker" and environment["AMP_API_KEY"] == "amp-secret"
    assert "GIT_AUTHOR_NAME" not in environment  # Workspace Git config owns commit identity.
    assert environment["OPENAI_API_KEY"] == "other" and environment["AMP_URL"] == "https://amp.example"
    assert not {"GH_TOKEN", "AGENTFLOW_WORKER_TOKEN", "GIT_CONFIG_PARAMETERS",
                "SSH_AUTH_SOCK", "AMP_CURRENT_THREAD_ID"} & environment.keys()


async def test_interrupted_inference_archives_only_its_initialized_thread(monkeypatch, tmp_path):
    # A killed execute process cannot reach its native auto-archive step.
    binary = tmp_path / "amp"
    init = stream().splitlines()[0].decode()
    binary.write_text(f'''#!{sys.executable}
import pathlib, sys, time
if sys.argv[1:3] == ["threads", "archive"]:
    pathlib.Path({str(tmp_path / "archived")!r}).write_text(sys.argv[3])
    sys.exit(1)  # Cleanup failure must not replace the inference timeout.
assert "--no-archive-after-execute" not in sys.argv
print({init!r}, flush=True)
time.sleep(30)
''')
    binary.chmod(0o700)
    monkeypatch.setattr("app.worker.execution.amp_agent.AMP_BIN", str(binary))
    result = await AmpCodingAgent(trusted_host=True, timeout=1).start("infer", tmp_path)
    assert result.failure_kind == "timeout" and (tmp_path / "archived").read_text() == THREAD


async def test_process_supervisor_drains_both_pipes_and_bounds_output(monkeypatch, tmp_path):
    monkeypatch.setattr("app.worker.execution.amp_agent.AMP_BIN", sys.executable)
    result = await AmpCodingAgent.run_process(
        ["-c", "import os; os.write(1,b'o'*131072); os.write(2,b'e'*131072)"],
        cwd=tmp_path, max_stdout=262144, timeout_kind="idle",
    )
    assert result.exit_code == 0 and len(result.stdout) == len(result.stderr) == 131072
    result = await AmpCodingAgent.run_process(
        ["-c", "import os,time; os.write(2,b'e'*262144); time.sleep(20)"],
        cwd=tmp_path, max_stdout=1024, timeout_kind="idle",
    )
    assert result.overflow and not result.timed_out and len(result.stderr) <= 1024


async def test_download_quota_bounds_files_not_just_process_output(monkeypatch, tmp_path):
    # Amp downloads to disk: limiting stdout alone would still fill the worker disk.
    monkeypatch.setattr("app.worker.execution.amp_agent.AMP_BIN", sys.executable)
    result = await AmpCodingAgent.run_process(
        ["-c", "f=open('attachment', 'wb'); f.write(b'x' * 4096); f.close()"], cwd=tmp_path,
        max_file_bytes=1025, timeout=5,
    )
    assert result.exit_code != 0
    assert (tmp_path / "attachment").stat().st_size == 1025
    assert not result.overflow and not result.timed_out
