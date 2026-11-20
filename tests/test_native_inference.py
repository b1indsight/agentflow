"""Public CodingAgent lifecycle, using controlled native children and no provider calls."""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.domain.models import CodingExecutionResult
from app.worker.execution.amp_agent import AmpCodingAgent
from app.worker.execution.codex import CodexAgent
from tests.test_amp_driver import THREAD, stream


@pytest.mark.parametrize("backend", ["codex", "amp"])
@pytest.mark.parametrize("resume", [False, True])
async def test_schema_calls_use_existing_cwd_and_clean_only_owned_files(monkeypatch, tmp_path, backend, resume):
    spawn = asyncio.create_subprocess_exec
    schemas = []
    existing = tmp_path / "output-schema.json"
    existing.write_text("caller-owned file")
    native = tmp_path / "native"
    native.mkdir()
    (native / "settings.json").write_text("caller-owned settings")

    async def cli(*args, **kwargs):
        directory = Path(kwargs["cwd"])
        assert directory == tmp_path
        assert {entry for entry in directory.iterdir() if entry.is_dir()} == {native}
        assert not any(key.startswith(("FLOWTOOL_", "GIT_CONFIG_")) for key in kwargs["env"])
        schema, = directory.glob("output-schema-*.json")
        schemas.append(schema)
        assert json.loads(schema.read_text())["type"] == "object"
        if backend == "codex":
            assert ("resume" in args) == resume
            assert "enabled=false" in " ".join(args)
            assert Path(args[args.index("--output-schema") + 1]) == schema
            script = f"open({args[args.index('-o') + 1]!r},'w').write('{{\"caller_owned\":true}}')"
        else:
            assert ("continue" in args) == resume
            assert "127.0.0.1:0/mcp" in " ".join(args)
            script = f"import sys; sys.stdin.read(); sys.stdout.buffer.write({stream()!r})"
        return await spawn(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cli)
    agent = (CodexAgent if backend == "codex" else AmpCodingAgent)(trusted_host=True)
    options = {"output_schema": {"type": "object"}, "max_output_bytes": 64, "timeout_seconds": 10}
    for _ in range(2):
        result = (await agent.resume(THREAD if backend == "amp" else "session-1", "Answer", cwd=tmp_path, **options)
                  if resume else await agent.start("Answer", tmp_path, **options))
        assert result.exit_code == 0 and result.last_message == '{"caller_owned":true}'
        assert result.backend == backend
    assert len(schemas) == 2 and schemas[0] != schemas[1]
    assert all(not schema.exists() for schema in schemas)
    assert set(tmp_path.iterdir()) == {existing, native}
    assert existing.read_text() == "caller-owned file"
    assert (native / "settings.json").read_text() == "caller-owned settings"


@pytest.mark.parametrize("backend", ["codex", "amp"])
@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_total_budget_joins_child_cleanup_before_deleting_schema(monkeypatch, tmp_path, backend, stop):
    # An active CLI continually renews its idle timer; the caller's total deadline must still win.
    spawn = asyncio.create_subprocess_exec
    children = []
    schemas = []
    entered = asyncio.Event()
    existing = tmp_path / "existing"
    existing.write_text("keep")

    async def cli(*args, **kwargs):
        assert Path(kwargs["cwd"]) == tmp_path
        schema, = tmp_path.glob("output-schema-*.json")
        schemas.append(schema)
        child = await spawn(sys.executable, "-c",
                            ("import os,time\nfrom pathlib import Path\nwhile True: "
                             f"assert Path({str(schema)!r}).is_file(); "
                             "os.write(2,b'.'); time.sleep(.01)"), **kwargs)
        children.append(child)
        entered.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cli)
    agent = (CodexAgent if backend == "codex" else AmpCodingAgent)(trusted_host=True, timeout=10)
    options = {"output_schema": {"type": "object"}, "timeout_seconds": .3 if stop == "timeout" else 10}
    running = asyncio.create_task(agent.start("Answer", tmp_path, **options))
    try:
        if stop == "cancel":
            await asyncio.wait_for(entered.wait(), 1)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            result = await asyncio.wait_for(running, 5)
            assert result.exit_code == 124 and result.failure_kind == "timeout"
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert children and children[0].returncode is not None
    assert all(not schema.exists() for schema in schemas)
    assert set(tmp_path.iterdir()) == {existing} and existing.read_text() == "keep"
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)


@pytest.mark.parametrize("backend", ["codex", "amp"])
async def test_flooded_native_pipe_is_drained_before_output_limit_is_reported(monkeypatch, tmp_path, backend):
    spawn = asyncio.create_subprocess_exec
    children = []

    async def cli(*args, **kwargs):
        child = await spawn(sys.executable, "-c",
                            "import os\nwhile True: os.write(2,b'x'*65536)", limit=1024, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cli)
    agent = (CodexAgent if backend == "codex" else AmpCodingAgent)(trusted_host=True)
    try:
        result = await asyncio.wait_for(agent.start(
            "Answer", tmp_path, output_schema={"type": "object"}, timeout_seconds=10,
        ), 15)
        assert result.failure_kind == "output_limit"
    finally:
        for child in children:
            await asyncio.gather(child.stdout.read(), child.stderr.read(), child.wait())
    assert children and children[0].returncode is not None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("backend", ["codex", "amp"])
async def test_final_budget_is_call_local_and_separate_from_native_streams(monkeypatch, tmp_path, backend):
    spawn = asyncio.create_subprocess_exec
    answer = json.dumps("答" * 16, ensure_ascii=False)  # 50 UTF-8 bytes, not 18 characters.

    async def cli(*args, **kwargs):
        if backend == "codex":
            script = (f"from pathlib import Path; Path({args[args.index('-o') + 1]!r}).write_text({answer!r}); "
                      "print('x'*4096)")
        else:
            events = stream(result=answer).split(b"\n")
            events.insert(1, json.dumps({"type": "assistant", "content": "x" * 4096}).encode())
            script = f"import sys; sys.stdin.read(); sys.stdout.buffer.write({b'\n'.join(events)!r})"
        return await spawn(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cli)
    agent = (CodexAgent if backend == "codex" else AmpCodingAgent)(trusted_host=True, max_output_bytes=32)
    output, small = await asyncio.gather(
        agent.start("Answer", tmp_path, output_schema={"type": "string"}, max_output_bytes=64),
        agent.start("Small coding answer", tmp_path, max_output_bytes=16),
    )
    assert output.last_message == answer and output.exit_code == 0
    assert small.failure_kind == "output_limit"
    default = await agent.start("Default budget", tmp_path)
    session = THREAD if backend == "amp" else "session-1"
    resumed = await agent.resume(session, "Resume budget", cwd=tmp_path)
    larger = await agent.resume(session, "Larger budget", cwd=tmp_path, max_output_bytes=64)
    assert default.failure_kind == resumed.failure_kind == "output_limit"
    assert larger.exit_code == 0 and larger.last_message == answer
    assert agent.max_output_bytes == 32
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("as_path", [False, True])
@pytest.mark.parametrize("answer,valid", [
    ("", False), (" ", False), ("not JSON", False),
    ('{"duplicate":1,"duplicate":2}', False), ("NaN", False), ("{} trailing", False),
    ('{"other":true}', True), ("null", True),
])
async def test_schema_calls_require_only_strict_json(monkeypatch, tmp_path, as_path, answer, valid):
    agent = CodexAgent(trusted_host=True)
    schema = {"type": "object", "required": ["caller_owned"]}
    existing = tmp_path / "caller-schema.json"
    existing.write_text(json.dumps(schema))
    seen = []

    async def run(*args, output_schema, **kwargs):
        seen.append(output_schema)
        assert output_schema.parent == tmp_path and json.loads(output_schema.read_text()) == schema
        return CodingExecutionResult(None, answer, 0)

    monkeypatch.setattr(agent, "_run", run)
    options = {"output_schema": existing if as_path else schema}
    result = await agent.start("Answer", tmp_path, **options)
    assert (result.exit_code == 0) == valid
    assert result.failure_kind == (None if valid else "invalid_response")
    if valid:
        assert result.last_message == answer  # Business schema interpretation belongs to the caller.
    else:
        assert result.last_message == "" and result.failure_detail == "Invalid final JSON response"
    assert existing.read_text() == json.dumps(schema)
    if as_path:
        assert seen == [existing]
    else:
        assert len(seen) == 1 and not seen[0].exists()
    assert list(tmp_path.iterdir()) == [existing]


@pytest.mark.parametrize("failure", ["backend", "oserror", "timeout_error", "cancel"])
async def test_failure_cleanup_and_safe_operational_errors(monkeypatch, tmp_path, failure):
    agent = CodexAgent(trusted_host=True)
    seen = []

    async def run(*args, output_schema, **kwargs):
        seen.append(output_schema)
        assert output_schema.is_file()
        if failure == "oserror":
            raise OSError("private credential from backend")
        if failure == "timeout_error":
            raise TimeoutError("private credential from backend")
        if failure == "cancel":
            raise asyncio.CancelledError
        return CodingExecutionResult(None, "", 1, failure_kind="backend_error")

    monkeypatch.setattr(agent, "_run", run)
    call = agent.start("Answer", tmp_path, output_schema={"type": "object"}, timeout_seconds=10)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        result = await call
        assert result.exit_code != 0
        assert result.failure_kind == ("configuration" if failure == "oserror" else "backend_error")
        assert "private" not in (result.failure_detail or "")
    assert seen and not seen[0].exists() and not list(tmp_path.iterdir())


@pytest.mark.parametrize("resume", [False, True])
async def test_generic_lifecycle_preserves_native_model_metadata(monkeypatch, tmp_path, resume):
    agent = CodexAgent(trusted_host=True)
    result = CodingExecutionResult("session-1", "answer", 0, model="observed-model")
    run = AsyncMock(return_value=result)
    monkeypatch.setattr(agent, "_run", run)
    actual = (await agent.resume("session-1", "Answer", cwd=tmp_path, model="requested-model", effort="high")
              if resume else await agent.start("Answer", tmp_path, model="requested-model", effort="high"))
    assert actual.backend == "codex" and actual.model == "observed-model"
    assert run.await_args.kwargs["model"] == "requested-model"
    assert run.await_args.kwargs["effort"] == "high"
