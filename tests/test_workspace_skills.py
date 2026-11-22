import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.domain.models import CodingExecutionResult
from app.worker.execution.skills import clear_skill_links, link_skills
from app.worker.execution.vcs import VcsVerificationError, _required_stdout
from tests.test_vcs import _git_fixture


async def test_flow_links_only_always_on_and_selected_skills(tmp_path: Path):
    workspace, _ = _git_fixture(tmp_path)
    checkout = tmp_path / "snapshot"
    for name in ("style", "database", "ui"):
        skill = checkout / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: For {name} tasks & reviews\nallowed-tools: Read Grep\n"
            f"always_on: {str(name == 'style').lower()}\nmetadata:\n  author: PRIVATE AUTHOR\n---\nPRIVATE BODY"
        )
    links = workspace / ".agents/skills"

    async def choose(prompt, cwd, **kwargs):
        assert (links / "style").resolve() == checkout / "style"
        assert "<name>database</name><description>For database tasks &amp; reviews</description>" in prompt
        assert "<allowed-tools>Read Grep</allowed-tools>" in prompt and "PRIVATE" not in prompt
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
