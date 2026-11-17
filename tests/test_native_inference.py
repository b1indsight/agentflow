"""Independent answer sessions through real Codex/Amp adapters and controlled CLI children."""

import asyncio
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import AsyncMock

import pytest

from app.domain.inference_models import InferenceError, InferenceMessage, InferenceTask
from app.domain.models import CodingExecutionResult
from app.worker.execution.amp_agent import AmpCodingAgent
from app.worker.execution.codex import CodexAgent
from app.worker.execution.inference import capability, infer
from tests.test_amp_driver import THREAD, stream


@pytest.mark.parametrize("backend", ["codex", "amp"])
async def test_native_answers_start_fresh_sessions_in_existing_root(monkeypatch, tmp_path, backend):
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
        assert not (directory / ".git").exists()
        assert not any(key.startswith(("FLOWTOOL_", "GIT_CONFIG_")) for key in kwargs["env"])
        schema, = directory.glob("output-schema-*.json")
        schemas.append(schema)
        assert json.loads(schema.read_text())["type"] == "object"
        if backend == "codex":
            assert "resume" not in args and "enabled=false" in " ".join(args)
            assert Path(args[args.index("--output-schema") + 1]) == schema
            script = f"open({args[args.index('-o') + 1]!r},'w').write('{{\"caller_owned\":true}}')"
        else:
            assert "continue" not in args and "127.0.0.1:0/mcp" in " ".join(args)
            script = f"import sys; sys.stdin.read(); sys.stdout.buffer.write({stream()!r})"
        return await spawn(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cli)
    monkeypatch.setattr("app.worker.execution.inference.shutil.which", lambda _: "/controlled-cli")
    agent = (CodexAgent if backend == "codex" else AmpCodingAgent)(trusted_host=True)
    original_limit = agent.max_output_bytes
    task = InferenceTask(messages=[InferenceMessage(role="user", content="Make a summary")],
                         output_schema={"type": "object"}, max_output_bytes=64)
    first = await infer(agent, task, root=tmp_path, backend=backend, timeout=10, enabled=True)
    second = await infer(agent, task, root=tmp_path, backend=backend, timeout=10, enabled=True)
    assert first.text == second.text == '{"caller_owned":true}'
    assert first.model is None and first.adapter_version == f"{backend}/v1"
    assert first.request_id == task.request_id and first.request_digest == task.digest
    assert agent.max_output_bytes == original_limit
    assert len(schemas) == 2 and schemas[0] != schemas[1]
    assert all(not schema.exists() for schema in schemas)
    assert set(tmp_path.iterdir()) == {existing, native}
    assert existing.read_text() == "caller-owned file"
    assert (native / "settings.json").read_text() == "caller-owned settings"


@pytest.mark.parametrize("linked", [False, True])
def test_git_parent_or_symlink_cannot_be_an_inference_root(monkeypatch, tmp_path, linked):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    root = repository / "worker"
    root.mkdir()
    if linked:
        link = tmp_path / "link"
        link.symlink_to(repository, target_is_directory=True)
        root = link / "worker"
    monkeypatch.setattr("app.worker.execution.inference.shutil.which", lambda _: "/controlled-cli")
    assert capability(root=root, backend="codex", enabled=True) is None


@pytest.mark.parametrize("writable", [False, True])
def test_capability_creates_and_cleans_only_its_own_file(monkeypatch, tmp_path, writable):
    existing = tmp_path / "existing"
    existing.write_text("keep")
    probes = []

    @contextmanager
    def create(**kwargs):
        if not writable:
            raise PermissionError("read-only root")
        with NamedTemporaryFile(**kwargs) as file:
            path = Path(file.name)
            assert path.parent == tmp_path and path.is_file()
            assert set(tmp_path.iterdir()) == {existing, path}
            probes.append(path)
            yield file

    monkeypatch.setattr("app.worker.execution.inference.shutil.which", lambda _: "/controlled-cli")
    monkeypatch.setattr("app.worker.execution.inference.NamedTemporaryFile", create)
    available = capability(root=tmp_path, backend="codex", enabled=True)
    assert (available is not None) == writable
    assert len(probes) == int(writable)
    assert set(tmp_path.iterdir()) == {existing} and existing.read_text() == "keep"


@pytest.mark.parametrize("unavailable", ["disabled", "missing_cli", "missing_root", "bare_repo"])
def test_capability_keeps_local_prerequisites(monkeypatch, tmp_path, unavailable):
    root = tmp_path
    if unavailable == "missing_root":
        root = tmp_path / "uninitialized"
    elif unavailable == "bare_repo":
        subprocess.run(["git", "init", "--bare", str(root)], check=True, capture_output=True)
    monkeypatch.setattr("app.worker.execution.inference.shutil.which", lambda _: (
        None if unavailable == "missing_cli" else "/controlled-cli"
    ))
    assert capability(root=root, backend="codex", enabled=unavailable != "disabled") is None
    if unavailable == "missing_root":
        assert not root.exists()


async def test_codex_input_budget_rejects_before_start(monkeypatch, tmp_path):
    monkeypatch.setattr("app.worker.execution.inference.shutil.which", lambda _: "/controlled-cli")
    monkeypatch.setattr("app.worker.execution.inference.inherited_agent_environment", lambda _: {})
    monkeypatch.setattr("app.worker.execution.inference.os.sysconf", lambda _: 32_768 + 256)
    available = capability(root=tmp_path, backend="codex", enabled=True)
    assert available is not None and available.max_input_bytes == 256
    agent = CodexAgent(trusted_host=True)
    start = AsyncMock()
    monkeypatch.setattr(agent, "start", start)
    with pytest.raises(InferenceError, match="configuration"):
        await infer(agent, InferenceTask(messages=[InferenceMessage(role="user", content="x" * 256)]),
                    root=tmp_path, backend="codex", timeout=10, enabled=True)
    start.assert_not_called()
    monkeypatch.setattr("app.worker.execution.inference.os.sysconf", lambda _: 32_768)
    assert capability(root=tmp_path, backend="codex", enabled=True) is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("backend", ["codex", "amp"])
@pytest.mark.parametrize("stop", ["task_timeout", "worker_timeout", "cancel"])
async def test_total_inference_budget_reaps_cli_before_deleting_schema(
    monkeypatch, tmp_path, backend, stop,
):
    # Continuous CLI activity renews coding's idle timer, but cannot extend inference's total budget.
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
        # The child observes schema lifetime through termination, not just before spawning.
        child = await spawn(sys.executable, "-c",
                            ("import os,time\nfrom pathlib import Path\nwhile True: "
                             f"assert Path({str(schema)!r}).is_file(); "
                             "os.write(2,b'.'); time.sleep(.01)"), **kwargs)
        children.append(child)
        entered.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cli)
    monkeypatch.setattr("app.worker.execution.inference.shutil.which", lambda _: "/controlled-cli")
    agent = (CodexAgent if backend == "codex" else AmpCodingAgent)(trusted_host=True, timeout=10)
    running = asyncio.create_task(infer(agent, InferenceTask(
        messages=[InferenceMessage(role="user", content="Answer")],
        output_schema={"type": "object"},
        timeout_seconds=.3 if stop == "task_timeout" else 10,
    ), root=tmp_path, backend=backend, timeout=.3 if stop == "worker_timeout" else 10, enabled=True))
    try:
        if stop == "cancel":
            await asyncio.wait_for(entered.wait(), 1)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            with pytest.raises(InferenceError, match="execution_timeout"):
                await running
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
    monkeypatch.setattr("app.worker.execution.inference.shutil.which", lambda _: "/controlled-cli")
    agent = (CodexAgent if backend == "codex" else AmpCodingAgent)(trusted_host=True)
    try:
        with pytest.raises(InferenceError, match="output_limit"):
            await infer(agent, InferenceTask(messages=[InferenceMessage(role="user", content="Answer")]),
                        root=tmp_path, backend=backend, timeout=30, enabled=True)
    finally:
        for child in children:
            await asyncio.gather(child.stdout.read(), child.stderr.read(), child.wait())
    assert children and children[0].returncode is not None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("backend", ["codex", "amp"])
async def test_final_budget_is_call_local_and_does_not_limit_events(monkeypatch, tmp_path, backend):
    spawn = asyncio.create_subprocess_exec
    answer = "答" * 16  # 48 bytes: above coding's configured default but within this inference call.

    async def cli(*args, **kwargs):
        if backend == "codex":
            script = (f"from pathlib import Path; Path({args[args.index('-o') + 1]!r}).write_text({answer!r}); "
                      "print('x'*4096)")
        else:
            script = f"import sys; sys.stdin.read(); sys.stdout.buffer.write({stream(result=answer)!r})"
        return await spawn(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cli)
    monkeypatch.setattr("app.worker.execution.inference.shutil.which", lambda _: "/controlled-cli")
    agent = (CodexAgent if backend == "codex" else AmpCodingAgent)(trusted_host=True, max_output_bytes=32)
    task = InferenceTask(messages=[InferenceMessage(role="user", content="Answer")], max_output_bytes=64)
    output, small = await asyncio.gather(
        infer(agent, task, root=tmp_path, backend=backend, timeout=10, enabled=True),
        agent.start("Small answer", tmp_path, max_output_bytes=16),
    )
    assert output.text == answer and small.failure_kind == "output_limit"
    default = await agent.start("Default budget", tmp_path)
    resumed = await agent.resume(THREAD if backend == "amp" else "session-1", "Resume budget", cwd=tmp_path)
    assert default.failure_kind == resumed.failure_kind == "output_limit"
    assert agent.max_output_bytes == 32
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("outcome,code", [
    (CodingExecutionResult(None, "", 124, failure_kind="timeout"), "execution_timeout"),
    (CodingExecutionResult(None, "", 1, failure_kind="output_limit"), "output_limit"),
    (CodingExecutionResult(None, "", 1), "backend_error"),
    (CodingExecutionResult(None, "多" * 22, 0), "output_limit"),
    (CodingExecutionResult(None, " ", 0), "invalid_response"),
    (CodingExecutionResult(None, '{"duplicate":1,"duplicate":2}', 0), "invalid_response"),
    (CodingExecutionResult(None, "not JSON", 0), "invalid_response"),
    # Schema interpretation stays with callers; inference only enforces strict JSON.
    (CodingExecutionResult(None, '{"other":true}', 0), None),
    (OSError("failed to execute"), "configuration"),
    (TimeoutError("backend timeout before total deadline"), "backend_error"),
    (ValueError("invalid backend output"), "invalid_response"),
])
async def test_failure_mapping_and_json_contract_clean_schema(monkeypatch, tmp_path, outcome, code):
    existing = tmp_path / "output-schema.json"
    existing.write_text("keep")
    monkeypatch.setattr("app.worker.execution.inference.shutil.which", lambda _: "/controlled-cli")
    agent = CodexAgent(trusted_host=True)
    original_limit = agent.max_output_bytes
    start = (AsyncMock(side_effect=outcome) if isinstance(outcome, Exception)
             else AsyncMock(return_value=outcome))
    monkeypatch.setattr(agent, "start", start)
    task = InferenceTask(
        messages=[InferenceMessage(role="system", content="Answer in JSON"),
                  InferenceMessage(role="user", content="Summarize")],
        output_schema={"type": "object", "required": ["caller_owned"]}, max_output_bytes=64,
    )
    if code is not None:
        with pytest.raises(InferenceError, match=f"^{code}$"):
            await infer(agent, task, root=tmp_path, backend="codex", timeout=10, enabled=True)
    else:
        output = await infer(agent, task, root=tmp_path, backend="codex", timeout=10, enabled=True)
        assert output.text == outcome.last_message
        assert output.request_id == task.request_id and output.request_digest == task.digest
    args, kwargs = start.await_args
    assert args[1] == tmp_path
    assert json.loads(args[0].splitlines()[-1]) == [message.model_dump() for message in task.messages]
    schema = kwargs["output_schema"]
    assert schema.parent == tmp_path and schema != existing and not schema.exists()
    assert kwargs["context"] is None and kwargs["max_output_bytes"] == task.max_output_bytes
    assert agent.max_output_bytes == original_limit
    assert set(tmp_path.iterdir()) == {existing} and existing.read_text() == "keep"
