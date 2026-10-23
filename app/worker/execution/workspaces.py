"""Worker-local bounded repository locations with durable ownership and release fencing.

The runtime holds WorkerRootLock for this component's entire lifetime. An assignment ending
does not free a location: archive or a stopped execution must present the journaled binding.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.worker.execution.vcs import (
    VcsVerificationError,
    _required_stdout,
    align_repository,
    clean_repository,
    ensure_seed,
    filesystem_operation,
    require_idle_repository,
    require_repository,
    worktree_registered,
)
from app.worker.state.files import write_private_record

Cleanup = Literal["released", "removed", "already_absent"]


class WorkspaceBinding(BaseModel):
    """Non-secret allocation fence carried by the assignment journal."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    repository: str
    slot_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    flow_id: int = Field(gt=0)
    generation: int = Field(gt=0)


class WorkspaceSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding: WorkspaceBinding
    relative_path: str
    kind: Literal["copy", "worktree"]
    state: Literal["preparing", "occupied", "idle", "releasing", "absent", "quarantined"]


class WorkspaceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    worker_id: str
    root_identity: tuple[str, int, int]
    slots: list[WorkspaceSlot] = Field(default_factory=list)


def validate_repository(repository: str) -> tuple[str, str]:
    """Accept only an unambiguous GitHub owner/repository, never a path or URL."""
    parts = repository.split("/")
    if (len(parts) != 2 or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part)
        or part in {".", ".."} or "--flow-" in part for part in parts
    )):
        raise VcsVerificationError("binding", "invalid repository identity")
    return parts[0], parts[1]


class RepositoryWorkspaces:
    """Keep one warm copy and temporary overflow worktrees per repository.

    `acquire` publishes ownership before preparing a path. Process-loss preparation states are
    deliberately not adopted. `release` is idempotent only for the very same allocation fence.
    """

    def __init__(self, root: Path, worker_id: str, *, limit: int = 2):
        if limit < 1:
            raise ValueError("workspace limit must be positive")
        self.root = root.absolute()
        self.worker_id = worker_id
        self.limit = limit
        self.manifest_path = self.root / "workspace-pool.json"
        self._lock = asyncio.Lock()

    def _managed(self, relative: Path) -> Path:
        if relative.is_absolute() or ".." in relative.parts:
            raise VcsVerificationError("workspace", "workspace path escapes worker root")
        cursor = self.root
        for component in (".", *relative.parts):
            cursor /= component
            if cursor.is_symlink() or (cursor.exists() and not cursor.is_dir()):
                raise VcsVerificationError("workspace", "repository path is a file or symlink")
        return cursor

    def _identity(self) -> tuple[str, int, int]:
        self._managed(Path("."))
        self.root.mkdir(parents=True, exist_ok=True)
        stat = self.root.stat()
        return str(self.root.resolve()), stat.st_dev, stat.st_ino

    def _load(self) -> WorkspaceManifest:
        identity = self._identity()
        if self.manifest_path.is_symlink():
            raise VcsVerificationError("workspace", "workspace manifest is a symlink")
        if not self.manifest_path.exists():
            locations = self._managed(Path("workspaces"))
            if locations.exists() and any(locations.iterdir()):
                raise VcsVerificationError("workspace", "workspace manifest missing; refusing adoption")
            return WorkspaceManifest(worker_id=self.worker_id, root_identity=identity)
        try:
            manifest = WorkspaceManifest.model_validate_json(self.manifest_path.read_text())
        except (OSError, ValueError, ValidationError) as error:
            raise VcsVerificationError("workspace", "invalid workspace manifest") from error
        if (manifest.worker_id, manifest.root_identity) != (self.worker_id, identity):
            raise VcsVerificationError("workspace", "workspace manifest belongs to another worker/root")
        ids = [slot.binding.slot_id for slot in manifest.slots]
        if len(ids) != len(set(ids)):
            raise VcsVerificationError("workspace", "duplicate workspace manifest locations")
        for slot in manifest.slots:
            expected = Path("workspaces", *validate_repository(slot.binding.repository),
                            slot.binding.slot_id)
            if slot.relative_path != str(expected):
                raise VcsVerificationError("workspace", "invalid workspace manifest path")
        return manifest

    def _write(self, manifest: WorkspaceManifest) -> None:
        write_private_record(self.manifest_path, manifest.model_dump_json())

    def path(self, binding: WorkspaceBinding) -> Path:
        return self._managed(Path("workspaces", *validate_repository(binding.repository),
                                  binding.slot_id))

    def _owned(self, manifest: WorkspaceManifest, binding: WorkspaceBinding) -> WorkspaceSlot:
        for slot in manifest.slots:
            if slot.binding.slot_id == binding.slot_id and slot.binding == binding:
                return slot
        raise VcsVerificationError("workspace", "workspace ownership or allocation generation mismatch")

    def lookup(self, repository: str, flow_id: int) -> WorkspaceBinding:
        """Look up only an existing live binding; revisions never clone or switch paths."""
        slots = [slot for slot in self._load().slots
                 if (slot.binding.repository, slot.binding.flow_id) == (repository, flow_id)]
        if len(slots) != 1 or slots[0].state != "occupied":
            raise VcsVerificationError("workspace", "original workspace binding is missing or unsettled")
        require_repository(self.path(slots[0].binding))
        return slots[0].binding

    def assert_unallocated(self, flow_id: int) -> None:
        """A missing journal binding must not hide an unfinished pool allocation."""
        if any(slot.binding.flow_id == flow_id and slot.state not in {"absent", "idle"}
               for slot in self._load().slots):
            raise VcsVerificationError("workspace", "unjournaled workspace requires manual recovery")

    async def acquire(
        self, repository: str, flow_id: int, *, base_branch: str, remote: str,
        push_token: SecretStr | None,
    ) -> WorkspaceBinding:
        """Reserve a bounded location and prepare it without discarding unknown contents."""
        owner, name = validate_repository(repository)
        if flow_id <= 0 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", base_branch):
            raise VcsVerificationError("bootstrap", "invalid Flow or base branch")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", remote):
            raise VcsVerificationError("bootstrap", "invalid task remote")
        async with self._lock:
            manifest = self._load()
            slots = [slot for slot in manifest.slots if slot.binding.repository == repository]
            directory = self._managed(Path("workspaces", owner, name))
            known = {slot.binding.slot_id for slot in slots}
            if directory.exists() and any(path.name not in known for path in directory.iterdir()):
                raise VcsVerificationError("workspace", "unregistered repository workspace; refusing adoption")
            original = [slot for slot in slots if slot.binding.flow_id == flow_id]
            if original and original[0].state not in {"absent", "idle"}:
                if len(original) != 1 or original[0].state != "occupied":
                    state = original[0].state
                    raise VcsVerificationError("workspace", f"original workspace is {state}")
                require_repository(self.path(original[0].binding))
                return original[0].binding
            slot = (original[0] if original and original[0].state == "idle"
                    else next((slot for slot in slots if slot.state == "idle"), None))
            if slot is not None:
                try:
                    await require_idle_repository(self.path(slot.binding))
                except VcsVerificationError:
                    slot.state = "quarantined"
                    self._write(manifest)
                    raise
            if slot is None:
                if sum(slot.state != "absent" for slot in slots) >= self.limit:
                    raise VcsVerificationError("workspace", "repository workspace capacity exhausted")
                slot = next((slot for slot in slots if slot.state == "absent"), None)
                if slot is not None and self.path(slot.binding).exists():
                    slot.state = "quarantined"
                    self._write(manifest)
                    raise VcsVerificationError("workspace", "absent workspace unexpectedly exists")
            if slot is None:
                binding = WorkspaceBinding(repository=repository, slot_id=uuid4().hex,
                                           flow_id=flow_id, generation=1)
                slot = WorkspaceSlot(
                    binding=binding, relative_path=str(Path("workspaces", owner, name, binding.slot_id)),
                    kind="worktree" if any(s.kind == "copy" for s in slots) else "copy",
                    state="preparing",
                )
                manifest.slots.append(slot)
            else:
                slot.binding = slot.binding.model_copy(update={
                    "flow_id": flow_id, "generation": slot.binding.generation + 1,
                })
                slot.state = "preparing"
            self._write(manifest)
            copying = False
            try:
                seed = self._managed(Path("repos", owner, name))
                seed.parent.mkdir(parents=True, exist_ok=True)
                await ensure_seed(seed, repository, base_branch, remote, push_token)
                path = self.path(slot.binding)
                path.parent.mkdir(parents=True, exist_ok=True)
                if slot.kind == "copy":
                    if not path.exists():
                        await require_idle_repository(seed)
                        if (seed / ".git").is_file():
                            raise VcsVerificationError("workspace", "linked seed cannot be fully copied")
                        copying = True
                        await filesystem_operation(shutil.copytree, seed, path, symlinks=True)
                    if (path / ".git").is_file():
                        raise VcsVerificationError("workspace", "full copy has external Git metadata")
                    await align_repository(path, seed, base_branch)
                else:
                    if not (seed / ".git").is_dir():
                        raise VcsVerificationError("workspace", "overflow requires a standalone Git seed")
                    if path.exists():
                        await _required_stdout("workspace", "git", "checkout", "--no-overwrite-ignore",
                                               "--detach", f"refs/heads/{base_branch}", cwd=path)
                    else:
                        await _required_stdout("workspace", "git", "worktree", "add", "--detach",
                                               str(path), f"refs/heads/{base_branch}", cwd=seed)
                slot.state = "occupied"
                self._write(manifest)
                return slot.binding
            except BaseException:
                # Ownership persists even if a command was interrupted after touching the path.
                slot.state = "quarantined"
                try:
                    path = self.path(slot.binding)
                    if copying and path.exists():
                        await filesystem_operation(shutil.rmtree, path)
                    if slot.kind == "copy" and not path.exists():
                        slot.state = "absent"  # No agent ran and no checkout remains: safe to retry.
                finally:
                    self._write(manifest)
                raise

    async def release(self, binding: WorkspaceBinding, *, recycle: bool = False) -> Cleanup:
        """Release an archive, or clean and retain a stopped execution's directory for reuse."""
        async with self._lock:
            manifest = self._load()
            slot = self._owned(manifest, binding)
            if slot.state == "idle":
                require_repository(self.path(binding))
                return "released"
            if slot.state == "absent":
                if self.path(binding).exists():
                    raise VcsVerificationError("workspace", "released workspace unexpectedly exists")
                return "already_absent"
            if slot.state not in {"occupied", "releasing"}:
                raise VcsVerificationError("workspace", f"workspace is {slot.state}; manual recovery required")
            try:
                path = self.path(binding)
                seed = self._managed(Path("repos", *validate_repository(binding.repository)))
                if not path.exists():
                    if (recycle or slot.state != "releasing" or slot.kind != "worktree"
                            or await worktree_registered(seed, path)):
                        raise VcsVerificationError("workspace", "repository missing without release evidence")
                    slot.state = "absent"
                    self._write(manifest)
                    return "already_absent"
                if recycle:
                    require_repository(path)
                else:
                    await require_idle_repository(path)
                if slot.kind == "copy" and (path / ".git").is_file():
                    raise VcsVerificationError("workspace", "full copy has external Git metadata")
                if slot.kind == "worktree":
                    common = await _required_stdout(
                        "workspace", "git", "rev-parse", "--path-format=absolute", "--git-common-dir",
                        cwd=path,
                    )
                    if Path(common) != seed / ".git" or not await worktree_registered(seed, path):
                        raise VcsVerificationError("workspace", "worktree ownership does not match seed")
                slot.state = "releasing"
                self._write(manifest)
                if recycle:
                    await clean_repository(path)
                if slot.kind == "copy" or recycle:
                    slot.state = "idle"
                    cleanup: Cleanup = "released"
                else:
                    # Cleanliness was checked separately; --force permits deleting ignored caches.
                    await _required_stdout("workspace", "git", "worktree", "remove", "--force",
                                           str(path), cwd=seed)
                    slot.state = "absent"
                    cleanup = "removed"
                self._write(manifest)
                return cleanup
            except VcsVerificationError:
                if slot.state != "releasing":
                    slot.state = "quarantined"
                    self._write(manifest)
                raise
