import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.communication.inprocess import in_process_transport_pair
from app.communication.session import Session
from app.contracts.protocol import Register, parse_worker_message
from app.contracts.validation import parse_worker_input
from app.controler.workers.transport import control_ack
from app.domain.models import CodingExecutionResult
from app.persistence.settings import SettingsStore, TextSetting
from app.worker.config import WorkerSettings
from app.worker.execution.skills import clear_skill_links, link_skills
from app.worker.execution.vcs import VcsVerificationError, _required_stdout, _vcs_environment
from app.worker.runtime import WorkerRuntime
from tests.github import RecordingGitHub
from tests.test_vcs import _git, _git_fixture
from tests.test_workspace_dispatch import wait_until


async def test_registration_and_refresh_clone_without_retargeting_old_snapshot(
    tmp_path,
    monkeypatch,
    server_runtime,
):
    publisher, _ = _git_fixture(tmp_path)
    config = tmp_path / "gitconfig"
    config.write_text(
        f'[url "{publisher.as_uri()}"]\n insteadOf = https://github.com/owner/skills.git\n'
    )
    monkeypatch.setattr(
        "app.worker.execution.skills._vcs_environment",
        lambda: _vcs_environment() | {"GIT_CONFIG_GLOBAL": str(config)},
    )
    monkeypatch.setattr(
        RecordingGitHub, "repository_read_token", AsyncMock(return_value=SecretStr("test-read"))
    )
    server = server_runtime()
    settings = SettingsStore(server.database)
    settings.set_value(TextSetting.SKILL_REPOSITORY, "owner/skills", expected=None)
    worker = WorkerRuntime(
        WorkerSettings(
            worker_id="worker-1",
            worker_token=SecretStr("test"),
            worker_root=tmp_path / "worker",
            serve_ws_url="wss://unused",
        )
    )
    host, peer = in_process_transport_pair()
    client = asyncio.create_task(worker._serve_connection(peer))
    register = parse_worker_message(await host.receive_text())
    assert isinstance(register, Register)
    session = Session(host, parse=parse_worker_input, make_ack=control_ack, epoch="epoch")
    session.register_control(register)
    session.start()
    serving = asyncio.create_task(
        server.serve_worker_session(register.worker_id, register, session)
    )
    try:
        await wait_until(lambda: worker.workspaces.skill_checkout is not None)
        first = worker.workspaces.skill_checkout
        assert first is not None
        original = await first
        assert original.is_relative_to(worker.workspaces.inference_path)
        assert (original / "README.md").read_text() == "base\n"
        (publisher / "README.md").write_text("new\n")
        _git("commit", "-am", "update skills", cwd=publisher)
        (callbacks,) = server.worker_sessions
        callbacks.skills_dirty = True
        server.gateway.wake_all()
        await wait_until(lambda: worker.workspaces.skill_checkout is not first)
        refreshed = worker.workspaces.skill_checkout
        assert refreshed is not None
        assert ((await refreshed) / "README.md").read_text() == "new\n"
        assert (original / "README.md").read_text() == "base\n"
        settings.set_value(TextSetting.SKILL_REPOSITORY, None, expected="owner/skills")
        callbacks.skills_dirty = True
        server.gateway.wake_all()
        await wait_until(lambda: worker.workspaces.skill_checkout is None)
    finally:
        await host.close()
        await asyncio.gather(client, serving, return_exceptions=True)


async def test_flow_links_only_always_on_and_selected_skills(tmp_path: Path):
    workspace, _ = _git_fixture(tmp_path)
    checkout = tmp_path / "snapshot"
    for name in ("style", "database", "ui"):
        skill = checkout / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: For {name} tasks\nalways_on: {str(name == 'style').lower()}\n---\nPRIVATE BODY"
        )
    links = workspace / ".agents/skills"

    async def choose(prompt, cwd, **kwargs):
        assert (links / "style").resolve() == checkout / "style"
        assert "For database tasks" in prompt and "PRIVATE BODY" not in prompt
        assert "For style tasks" not in prompt
        return CodingExecutionResult("selection", json.dumps({"skills": ["database"]}), 0)

    agent = AsyncMock()
    agent.start.side_effect = choose
    receipt = tmp_path / "receipt"
    await link_skills(checkout, workspace, "Change the schema", agent, None, None, receipt)
    assert (links / "database").resolve() == checkout / "database"
    assert not (links / "ui").exists()
    assert not await _required_stdout("workspace", "git", "status", "--porcelain", cwd=workspace)
    clear_skill_links(workspace, receipt)
    assert not list(links.iterdir())

    # A model response is data, not a filesystem path supplied to symlink().
    agent.start.side_effect = None
    agent.start.return_value = CodingExecutionResult("selection", '{"skills":["../outside"]}', 0)
    with pytest.raises(VcsVerificationError):
        await link_skills(checkout, workspace, "New task", agent, None, None, receipt)
