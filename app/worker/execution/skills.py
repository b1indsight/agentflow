"""Worker-owned skill snapshots and the links installed for one Flow."""

import json
import re
import tempfile
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, TypeAdapter

from app.contracts.protocol import SkillRepositorySource
from app.worker.coding_agent import CodingAgent
from app.worker.execution.credentials import git_credential_environment
from app.worker.execution.vcs import VcsVerificationError, _required_stdout, _run, _vcs_environment
from app.worker.state.files import write_private_record

SkillName = Annotated[str, Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]
_LINKS = TypeAdapter(dict[SkillName, str])


class _Skill(BaseModel):
    name: SkillName
    description: str = Field(min_length=1)
    always_on: StrictBool = False


class _Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skills: list[str]


async def clone_skills(inference: Path, source: SkillRepositorySource) -> Path:
    """Publish by commit, so changing the configured repository never retargets live links."""
    if source.token is None:
        raise VcsVerificationError("bootstrap", "skill repository read credential unavailable")
    snapshots = inference / "skill-repositories"
    if snapshots.is_symlink():
        raise VcsVerificationError("bootstrap", "skill snapshot directory is a symlink")
    snapshots.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=snapshots) as temporary:
        checkout = Path(temporary) / "checkout"
        code, _, _ = await _run(
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "clone",
            "--depth=1",
            "--single-branch",
            "--",
            f"https://github.com/{source.repository}.git",
            str(checkout),
            cwd=Path(temporary),
            environment=_vcs_environment() | git_credential_environment(source.token),
            stage="bootstrap",
        )
        if code:
            raise VcsVerificationError("bootstrap", "skill repository clone failed")
        sha = await _required_stdout(
            "bootstrap", "git", "rev-parse", "--verify", "HEAD^{commit}", cwd=checkout
        )
        target = snapshots / sha
        if target.is_symlink():
            raise VcsVerificationError("bootstrap", "skill snapshot is a symlink")
        if not target.exists():
            checkout.rename(target)
        return target


def _catalog(checkout: Path) -> dict[str, tuple[Path, _Skill]]:
    nested = checkout / ".agents/skills"
    root = nested if nested.is_dir() else checkout
    if checkout.is_symlink() or (checkout / ".agents").is_symlink() or nested.is_symlink():
        raise VcsVerificationError("bootstrap", "skill directory is a symlink")
    catalog = {}
    for folder in sorted(root.iterdir()):
        path = folder / "SKILL.md"
        if folder.is_symlink() or path.is_symlink() or not path.is_file():
            continue
        try:
            header = re.split(r"^---[ \t]*$", path.read_text(), maxsplit=2, flags=re.MULTILINE)
            if len(header) != 3 or header[0]:
                raise ValueError
            skill = _Skill.model_validate(yaml.safe_load(header[1]))
            if skill.name != folder.name:
                raise ValueError
        except (ValueError, yaml.YAMLError) as exc:
            raise VcsVerificationError("bootstrap", "invalid skill frontmatter") from exc
        catalog[skill.name] = folder, skill
    return catalog


def _ignore_text(names: list[str]) -> str:
    return "# FlowTool workspace links\n/.gitignore\n" + "".join(f"/{name}\n" for name in names)


def clear_skill_links(workspace: Path, receipt: Path) -> None:
    """Before slot reuse, remove only entries still matching the worker's installation."""
    if not receipt.exists():
        return
    links = workspace / ".agents/skills"
    entries = _LINKS.validate_json(receipt.read_bytes())
    if not (workspace / ".agents").is_symlink() and not links.is_symlink():
        for name, target in entries.items():
            path = links / name
            if path.is_symlink() and str(path.readlink()) == target:
                path.unlink()
        ignore = links / ".gitignore"
        if not ignore.is_symlink() and ignore.is_file():
            # A crash may have recorded the optional choices before replacing the old ignore file.
            text = ignore.read_text()
            names = [line.removeprefix("/") for line in text.splitlines()[2:]]
            if set(names) <= entries.keys() and text == _ignore_text(names):
                ignore.unlink()
    receipt.unlink()


async def link_skills(
    checkout: Path,
    workspace: Path,
    task: str,
    agent: CodingAgent,
    model: str | None,
    effort: str | None,
    receipt: Path,
) -> None:
    """Link always-on skills before selection, and expose only candidate metadata to the selector."""
    catalog = _catalog(checkout)
    links = workspace / ".agents/skills"
    if (workspace / ".agents").is_symlink() or links.is_symlink():
        raise VcsVerificationError("bootstrap", "skill destination is a symlink")
    available = {
        name: skill
        for name, (_, skill) in catalog.items()
        if not (links / name).exists() and not (links / name).is_symlink()
    }
    installed: dict[str, str] = {}

    def install(names: list[str]) -> None:
        if not names:
            return
        links.mkdir(parents=True, exist_ok=True)
        ignore = links / ".gitignore"
        if ignore.is_symlink() or (
            ignore.exists()
            and (not installed or ignore.read_text() != _ignore_text(list(installed)))
        ):
            raise VcsVerificationError("bootstrap", "project owns the skill ignore file")
        installed.update((name, str(catalog[name][0])) for name in names)
        write_private_record(receipt, json.dumps(installed))
        write_private_record(ignore, _ignore_text(list(installed)))
        for name in names:
            (links / name).symlink_to(installed[name], target_is_directory=True)

    install([name for name, skill in available.items() if skill.always_on])
    candidates = {
        name: skill.description for name, skill in available.items() if not skill.always_on
    }
    if candidates:
        prompt = (
            "Select the optional skills needed for the task. Return their names, or an empty "
            "list if none apply. Use only the supplied descriptions; do not use tools.\n"
            + json.dumps({"task": task, "candidates": candidates}, ensure_ascii=False)
        )
        # Start a separate session without the coding assignment's GitHub or MCP credentials.
        run = await agent.start(
            prompt,
            checkout.parent.parent,
            output_schema=_Selection.model_json_schema(),
            model=model,
            effort=effort,
            max_output_bytes=16384,
        )
        try:
            chosen = _Selection.model_validate_json(run.last_message).skills
            if run.exit_code or run.failure_kind or not set(chosen) <= candidates.keys():
                raise ValueError
        except ValueError as exc:
            raise VcsVerificationError("bootstrap", "invalid skill selection") from exc
        if links.is_symlink() or (workspace / ".agents").is_symlink():
            raise VcsVerificationError("bootstrap", "skill destination is a symlink")
        install(list(dict.fromkeys(chosen)))
