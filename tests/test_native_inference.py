"""Independent answer sessions through real Codex/Amp adapters and controlled CLI children."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from app.domain.inference_models import InferenceError, InferenceMessage, InferenceTask
from app.worker.execution.amp_agent import AmpCodingAgent
from app.worker.execution.codex import CodexAgent
from app.worker.execution.inference import NativeAgentInference
from tests.test_amp_driver import stream


@pytest.mark.parametrize("backend", ["codex", "amp"])
async def test_native_answers_have_fresh_workspaces_and_no_flow_capabilities(monkeypatch, tmp_path, backend):
    spawn = asyncio.create_subprocess_exec
    invocations = []

    async def cli(*args, **kwargs):
        directory = Path(kwargs["cwd"])
        invocations.append((args, kwargs, directory))
        assert not (directory / ".git").exists()
        assert not any(key.startswith(("FLOWTOOL_", "GIT_CONFIG_")) for key in kwargs["env"])
        if backend == "codex":
            assert "resume" not in args and "enabled=false" in " ".join(args)
            schema = Path(args[args.index("--output-schema") + 1])
            assert json.loads(schema.read_text())["type"] == "object"
            script = f"open({args[args.index('-o') + 1]!r},'w').write('{{\"caller_owned\":true}}')"
        else:
            assert "continue" not in args and "127.0.0.1:0/mcp" in " ".join(args)
            script = f"import sys; sys.stdin.read(); sys.stdout.buffer.write({stream()!r})"
        return await spawn(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cli)
    monkeypatch.setattr("app.worker.execution.inference.shutil.which", lambda _: "/controlled-cli")
    agent = (CodexAgent if backend == "codex" else AmpCodingAgent)(trusted_host=True)
    inference = NativeAgentInference(agent, root=tmp_path, backend=backend, timeout=10, enabled=True)
    task = InferenceTask(messages=[InferenceMessage(role="user", content="Make a summary")],
                         output_schema={"type": "object"})
    first, second = await inference.infer(task), await inference.infer(task)
    assert first.text == second.text == '{"caller_owned":true}'
    assert first.model is None and first.adapter_version == f"{backend}/v1"
    assert invocations[0][2] != invocations[1][2]
    assert all(not entry[2].exists() for entry in invocations)


@pytest.mark.parametrize("linked", [False, True])
def test_git_parent_or_symlink_cannot_be_an_inference_root(monkeypatch, tmp_path, linked):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    root = repository / "worker"
    if linked:
        link = tmp_path / "link"
        link.symlink_to(repository, target_is_directory=True)
        root = link / "worker"
    monkeypatch.setattr("app.worker.execution.inference.shutil.which", lambda _: "/controlled-cli")
    inference = NativeAgentInference(CodexAgent(trusted_host=True), root=root, backend="codex",
                                     timeout=10, enabled=True)
    assert inference.capability() is None


@pytest.mark.parametrize("backend", ["codex", "amp"])
@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_total_inference_budget_reaps_active_cli_before_deleting_workspace(
    monkeypatch, tmp_path, backend, stop,
):
    # Continuous CLI activity renews coding's idle timer, but cannot extend inference's total budget.
    spawn = asyncio.create_subprocess_exec
    children = []
    directories = []
    entered = asyncio.Event()

    async def cli(*args, **kwargs):
        directories.append(Path(kwargs["cwd"]))
        child = await spawn(sys.executable, "-c",
                            "import os,time\nwhile True: os.write(2,b'.'); time.sleep(.01)", **kwargs)
        children.append(child)
        entered.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cli)
    monkeypatch.setattr("app.worker.execution.inference.shutil.which", lambda _: "/controlled-cli")
    agent = (CodexAgent if backend == "codex" else AmpCodingAgent)(trusted_host=True, timeout=10)
    inference = NativeAgentInference(agent, root=tmp_path, backend=backend, timeout=10, enabled=True)
    running = asyncio.create_task(inference.infer(InferenceTask(
        messages=[InferenceMessage(role="user", content="Answer")],
        timeout_seconds=.15 if stop == "timeout" else 10,
    )))
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
    assert not directories[0].exists()
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
    inference = NativeAgentInference(agent, root=tmp_path, backend=backend, timeout=30, enabled=True)
    try:
        with pytest.raises(InferenceError, match="output_limit"):
            await inference.infer(InferenceTask(messages=[InferenceMessage(role="user", content="Answer")]))
    finally:
        for child in children:
            await asyncio.gather(child.stdout.read(), child.stderr.read(), child.wait())
    assert children and children[0].returncode is not None
    assert not list((tmp_path / "inference").iterdir())
