"""Worker-local bounded repository locations with durable ownership and release fencing.

The runtime holds WorkerRootLock for this component's entire lifetime. An assignment ending
does not free a location: archive or a stopped execution must present the journaled binding.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from app.domain.models import WorkspaceInventory
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
from app.worker.state.files import sync_directory, write_private_record

Cleanup = Literal["released", "removed", "already_absent"]


class WorkspaceBinding(BaseModel):
    """Non-secret allocation fence carried by the assignment journal."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    repository: str
    slot_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    flow_id: int = Field(gt=0)
    generation: int = Field(gt=0)


class WorkspaceSlot(BaseModel):
    """A concrete inventory location, optionally reserved by a real Flow."""

    model_config = ConfigDict(extra="forbid")
    slot_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    repository: str
    generation: int = Field(gt=0)
    binding: WorkspaceBinding | None = None
    relative_path: str
    kind: Literal["copy", "worktree"]
    state: Literal["preparing", "occupied", "idle", "releasing", "absent", "quarantined"]
    directory_identity: tuple[int, int] | None = None
    release_job_id: int | None = None
    release_assignment_token: str | None = None
    release_recycle: bool | None = None
    settled_job_id: int | None = None
    settled_assignment_token: str | None = None
    settled_generation: int | None = None
    settled_flow_id: int | None = None

    @model_validator(mode="after")
    def _fences(self) -> Self:
        if self.binding is not None and (
            self.binding.slot_id, self.binding.repository, self.binding.generation
        ) != (self.slot_id, self.repository, self.generation):
            raise ValueError("workspace binding does not match inventory")
        if self.state in {"occupied", "releasing"} and self.binding is None:
            raise ValueError("active workspace has no Flow binding")
        if (self.release_job_id is None) != (self.release_assignment_token is None):
            raise ValueError("incomplete release receipt fence")
        if (self.settled_job_id is None) != (self.settled_assignment_token is None):
            raise ValueError("incomplete settled receipt")
        return self


class InferenceWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    directory_identity: tuple[int, int] | None = None


class WorkspaceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[2] = 2
    worker_id: str
    root_identity: tuple[str, int, int]
    inference: InferenceWorkspace | None = None
    slots: list[WorkspaceSlot] = Field(default_factory=list)


class _LegacySlot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding: WorkspaceBinding
    relative_path: str
    kind: Literal["copy", "worktree"]
    state: Literal["preparing", "occupied", "idle", "releasing", "absent", "quarantined"]


class _LegacyManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    worker_id: str
    root_identity: tuple[str, int, int]
    slots: list[_LegacySlot]


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
    """Own registered directories on one event loop under the runtime's root lock.

    Provisioning needs no credentials or Flow. Claiming never selects a replacement location.
    Manifest transactions contain no awaits; only seed/worktree operations share a repository
    lock. Cleanup and its ACK are separate durable facts, fenced by allocation and execution.
    """

    def __init__(self, root: Path, worker_id: str, *, limit: int = 2):
        if limit < 1:
            raise ValueError("workspace limit must be positive")
        self.root = root.absolute()
        self.worker_id = worker_id
        self.limit = limit
        self.manifest_path = self.root / "workspace-pool.json"
        self._repository_locks: dict[str, asyncio.Lock] = {}
        self._release_locks: dict[str, asyncio.Lock] = {}

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
            for relative in ("workspaces", "inference"):
                locations = self._managed(Path(relative))
                if locations.exists() and any(locations.iterdir()):
                    raise VcsVerificationError("workspace", "workspace manifest missing; refusing adoption")
            return WorkspaceManifest(worker_id=self.worker_id, root_identity=identity)
        try:
            raw = self.manifest_path.read_text()
            try:
                manifest = WorkspaceManifest.model_validate_json(raw)
            except ValidationError:
                old = _LegacyManifest.model_validate_json(raw)
                manifest = WorkspaceManifest(
                    worker_id=old.worker_id, root_identity=old.root_identity,
                    slots=[WorkspaceSlot(
                        slot_id=s.binding.slot_id, repository=s.binding.repository,
                        generation=s.binding.generation, binding=s.binding,
                        relative_path=s.relative_path, kind=s.kind, state=s.state,
                    ) for s in old.slots],
                )
        except (OSError, ValueError, ValidationError) as error:
            raise VcsVerificationError("workspace", "invalid workspace manifest") from error
        if (manifest.worker_id, manifest.root_identity) != (self.worker_id, identity):
            raise VcsVerificationError("workspace", "workspace manifest belongs to another worker/root")
        ids = [slot.slot_id for slot in manifest.slots]
        if manifest.inference is not None:
            ids.append(manifest.inference.slot_id)
        if len(ids) != len(set(ids)):
            raise VcsVerificationError("workspace", "duplicate workspace manifest locations")
        flows = [slot.binding.flow_id for slot in manifest.slots if slot.binding is not None]
        if len(flows) != len(set(flows)):
            raise VcsVerificationError("workspace", "Flow has multiple workspace bindings")
        for slot in manifest.slots:
            expected = Path("workspaces", *validate_repository(slot.repository), slot.slot_id)
            if slot.relative_path != str(expected):
                raise VcsVerificationError("workspace", "invalid workspace manifest path")
        return manifest

    def _write(self, manifest: WorkspaceManifest) -> None:
        write_private_record(self.manifest_path, manifest.model_dump_json())

    @staticmethod
    def _directory_identity(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return stat.st_dev, stat.st_ino

    def initialize(self) -> WorkspaceInventory:
        """Create the real default workspace once, or verify its recorded physical identity."""
        manifest = self._load()
        if manifest.inference is None:
            directory = self._managed(Path("inference"))
            if directory.exists() and any(directory.iterdir()):
                raise VcsVerificationError("workspace", "unregistered inference workspace")
            manifest.inference = InferenceWorkspace(slot_id=uuid4().hex)
            self._write(manifest)
        inference = manifest.inference
        path = self._managed(Path("inference", inference.slot_id))
        if inference.directory_identity is None:
            # An interruption after mkdir but before recording identity cannot authorize adoption.
            if path.exists():
                raise VcsVerificationError("workspace", "inference initialization requires recovery")
            path.mkdir(parents=True, mode=0o700)
            sync_directory(path.parent)
            inference.directory_identity = self._directory_identity(path)
            self._write(manifest)
        self._verify_inference(manifest)
        return WorkspaceInventory(workspace=inference.slot_id, repository=None,
                                  generation=1, state="idle", flow_id=None)

    def _verify_inference(self, manifest: WorkspaceManifest) -> Path:
        inference = manifest.inference
        if inference is None:
            raise VcsVerificationError("workspace", "default workspace has not been initialized")
        path = self._managed(Path("inference", inference.slot_id))
        if not path.exists() or self._directory_identity(path) != inference.directory_identity:
            raise VcsVerificationError("workspace", "default workspace identity is missing or changed")
        return path

    @property
    def inference_path(self) -> Path:
        return self._verify_inference(self._load())

    def _verify_slot(self, slot: WorkspaceSlot) -> Path:
        path = self._managed(Path(slot.relative_path))
        if not path.exists():
            raise VcsVerificationError("workspace", "repository workspace is missing")
        if (slot.directory_identity is not None
                and self._directory_identity(path) != slot.directory_identity):
            raise VcsVerificationError("workspace", "workspace directory identity changed")
        return path

    def inventory(self) -> list[WorkspaceInventory]:
        """Report all evidence; cleaned but unacknowledged locations remain unavailable."""
        manifest = self._load()
        self._verify_inference(manifest)
        assert manifest.inference is not None
        items = [WorkspaceInventory(workspace=manifest.inference.slot_id, repository=None,
                                    generation=1, state="idle", flow_id=None)]
        changed = False
        for slot in manifest.slots:
            if slot.state not in {"quarantined", "preparing", "releasing"}:
                try:
                    if slot.state == "absent":
                        if self._managed(Path(slot.relative_path)).exists():
                            raise VcsVerificationError("workspace", "absent workspace unexpectedly exists")
                    else:
                        self._verify_slot(slot)
                except VcsVerificationError:
                    slot.state = "quarantined"
                    changed = True
            state = slot.state
            if slot.binding is not None and state in {"idle", "absent"}:
                state = "releasing"
            items.append(WorkspaceInventory(
                workspace=slot.slot_id, repository=slot.repository, generation=slot.generation,
                state=state, flow_id=slot.binding.flow_id if slot.binding is not None else None,
                settled_job_id=slot.settled_job_id,
                settled_assignment_token=slot.settled_assignment_token,
            ))
        if changed:
            self._write(manifest)
        return items

    async def provision(self, repository: str) -> list[WorkspaceInventory]:
        """Register empty concrete directories up to this repository's configured quota."""
        owner, name = validate_repository(repository)
        self.initialize()
        manifest = self._load()
        slots = [s for s in manifest.slots if s.repository == repository]
        directory = self._managed(Path("workspaces", owner, name))
        known = {s.slot_id for s in slots}
        if directory.exists() and any(p.name not in known for p in directory.iterdir()):
            raise VcsVerificationError("workspace", "unregistered repository workspace; refusing adoption")
        # No awaits in this transaction: concurrent demand cannot over-provision the quota.
        while sum(s.state != "absent" for s in slots) < self.limit:
            slot = next((s for s in slots if s.state == "absent" and s.binding is None), None)
            if slot is None:
                slot_id = uuid4().hex
                slot = WorkspaceSlot(
                    slot_id=slot_id, repository=repository, generation=1,
                    relative_path=str(Path("workspaces", owner, name, slot_id)),
                    kind="worktree" if any(s.kind == "copy" for s in slots) else "copy",
                    state="preparing",
                )
                slots.append(slot)
                manifest.slots.append(slot)
            path = self._managed(Path(slot.relative_path))
            if path.exists():
                slot.state = "quarantined"
                self._write(manifest)
                raise VcsVerificationError("workspace", "absent workspace unexpectedly exists")
            slot.state = "preparing"
            self._write(manifest)
            path.mkdir(parents=True, mode=0o700)
            sync_directory(path.parent)
            slot.directory_identity = self._directory_identity(path)
            slot.state = "idle"
            self._write(manifest)
        return self.inventory()

    def path(self, binding: WorkspaceBinding) -> Path:
        return self._managed(Path("workspaces", *validate_repository(binding.repository),
                                  binding.slot_id))

    def _owned(self, manifest: WorkspaceManifest, binding: WorkspaceBinding) -> WorkspaceSlot:
        for slot in manifest.slots:
            if slot.slot_id == binding.slot_id and slot.binding == binding:
                return slot
        raise VcsVerificationError("workspace", "workspace ownership or allocation generation mismatch")

    def lookup(self, repository: str, flow_id: int) -> WorkspaceBinding:
        """Look up only an existing live binding; revisions never clone or switch paths."""
        slots = [slot for slot in self._load().slots
                 if slot.binding is not None
                 and (slot.repository, slot.binding.flow_id) == (repository, flow_id)]
        if len(slots) != 1 or slots[0].state != "occupied":
            raise VcsVerificationError("workspace", "original workspace binding is missing or unsettled")
        require_repository(self._verify_slot(slots[0]))
        assert slots[0].binding is not None
        return slots[0].binding

    def assert_unallocated(self, flow_id: int) -> None:
        """A missing journal binding must not hide an unfinished pool allocation."""
        if any(slot.binding is not None and slot.binding.flow_id == flow_id
               for slot in self._load().slots):
            raise VcsVerificationError("workspace", "unjournaled workspace requires manual recovery")

    def _repository_lock(self, repository: str) -> asyncio.Lock:
        return self._repository_locks.setdefault(repository, asyncio.Lock())

    async def claim(
        self, repository: str, flow_id: int, workspace: str, generation: int,
        *, base_branch: str, remote: str,
        push_token: SecretStr | None,
    ) -> WorkspaceBinding:
        """Claim exactly the advertised fence, then prepare it outside the manifest transaction."""
        owner, name = validate_repository(repository)
        if flow_id <= 0 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", base_branch):
            raise VcsVerificationError("bootstrap", "invalid Flow or base branch")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", remote):
            raise VcsVerificationError("bootstrap", "invalid task remote")
        manifest = self._load()
        slot = next((s for s in manifest.slots if s.slot_id == workspace), None)
        if slot is None or (slot.repository, slot.generation) != (repository, generation):
            raise VcsVerificationError("workspace", "assigned workspace identity or generation mismatch")
        binding = WorkspaceBinding(repository=repository, slot_id=workspace,
                                   flow_id=flow_id, generation=generation)
        if slot.binding == binding and slot.state == "occupied":
            require_repository(self._verify_slot(slot))
            return binding
        if slot.binding is not None or slot.state != "idle":
            raise VcsVerificationError("workspace", f"assigned workspace is {slot.state} or still bound")
        if any(s.binding is not None and s.binding.flow_id == flow_id for s in manifest.slots):
            raise VcsVerificationError("workspace", "Flow already owns another workspace")
        path = self._verify_slot(slot)
        slot.binding, slot.state = binding, "preparing"
        self._write(manifest)
        # The seed and its worktree registry are shared; the manifest and independent repos are not.
        try:
            async with self._repository_lock(repository):
                await self._prepare(binding, slot.kind, path, owner, name,
                                    base_branch, remote, push_token)
            manifest = self._load()
            slot = self._owned(manifest, binding)
            if slot.state != "preparing":
                raise VcsVerificationError("workspace", "workspace preparation fence changed")
            slot.directory_identity = self._directory_identity(self.path(binding))
            slot.state = "occupied"
            self._write(manifest)
            return binding
        except BaseException:
            manifest = self._load()
            slot = self._owned(manifest, binding)
            if slot.state == "preparing":
                slot.state = "quarantined"
                self._write(manifest)
            raise

    async def _prepare(
        self, binding: WorkspaceBinding, kind: Literal["copy", "worktree"], path: Path,
        owner: str, name: str, base_branch: str, remote: str, push_token: SecretStr | None,
    ) -> None:
        seed = self._managed(Path("repos", owner, name))
        seed.parent.mkdir(parents=True, exist_ok=True)
        await ensure_seed(seed, binding.repository, base_branch, remote, push_token)
        slot = self._owned(self._load(), binding)
        self._verify_slot(slot)
        empty = not any(path.iterdir())
        if kind == "copy":
            copying = False
            try:
                if empty:
                    await require_idle_repository(seed)
                    if (seed / ".git").is_file():
                        raise VcsVerificationError("workspace", "linked seed cannot be fully copied")
                    copying = True
                    await filesystem_operation(shutil.copytree, seed, path,
                                               symlinks=True, dirs_exist_ok=True)
                    copying = False
                if (path / ".git").is_file():
                    raise VcsVerificationError("workspace", "full copy has external Git metadata")
                await align_repository(path, seed, base_branch)
            except BaseException:
                if copying:
                    self._verify_slot(self._owned(self._load(), binding))
                    await filesystem_operation(shutil.rmtree, path)
                # Failed/unknown preparation stays quarantined, even if no checkout remains.
                raise
        else:
            if not (seed / ".git").is_dir():
                raise VcsVerificationError("workspace", "overflow requires a standalone Git seed")
            if empty:
                await _required_stdout("workspace", "git", "worktree", "add", "--detach",
                                       str(path), f"refs/heads/{base_branch}", cwd=seed)
            else:
                await require_idle_repository(path)
                await _required_stdout("workspace", "git", "checkout", "--no-overwrite-ignore",
                                       "--detach", f"refs/heads/{base_branch}", cwd=path)

    async def release(
        self, binding: WorkspaceBinding, *, job_id: int, token: str, recycle: bool = False,
    ) -> Cleanup:
        """Clean only this execution's allocation; its matching ACK later enables reuse.

        The receipt fence is required because an earlier job's ACK can have the same binding.
        A retry after a failed archive may replace it only after that failure was acknowledged.
        """
        if job_id <= 0 or not token:
            raise ValueError("release requires a job and assignment token")
        async with self._release_locks.setdefault(binding.slot_id, asyncio.Lock()):
            manifest = self._load()
            cleaned = next((s for s in manifest.slots if s.slot_id == binding.slot_id), None)
            if (not recycle and cleaned is not None and cleaned.binding is None
                    and cleaned.state == "idle" and cleaned.repository == binding.repository
                    and cleaned.generation == binding.generation
                    and cleaned.settled_generation == binding.generation - 1
                    and cleaned.settled_flow_id == binding.flow_id
                    and cleaned.settled_job_id is not None and job_id > cleaned.settled_job_id):
                # A merge may arrive during stop settlement. Only the same Flow's
                # acknowledged, physically unchanged directory can resume archive cleanup.
                require_repository(self._verify_slot(cleaned))
                cleaned.binding, cleaned.state = binding, "occupied"
                self._write(manifest)
            slot = self._owned(manifest, binding)
            if ((slot.settled_generation, slot.settled_flow_id) == (binding.generation, binding.flow_id)
                    and slot.settled_job_id is not None
                    and job_id < slot.settled_job_id):
                raise VcsVerificationError("workspace", "stale cleanup execution")
            previous_release = (slot.release_job_id, slot.release_assignment_token)
            if previous_release not in {(None, None), (job_id, token)} and previous_release != (
                slot.settled_job_id, slot.settled_assignment_token
            ):
                raise VcsVerificationError("workspace", "release receipt does not match pending cleanup")
            if slot.release_recycle is not None and slot.release_recycle != recycle:
                raise VcsVerificationError("workspace", "release mode does not match pending cleanup")
            if slot.state not in {"occupied", "releasing", "idle", "absent"}:
                raise VcsVerificationError("workspace", f"workspace is {slot.state}; manual recovery required")
            was_releasing = slot.state == "releasing"
            slot.release_job_id, slot.release_assignment_token = job_id, token
            slot.release_recycle = recycle
            self._write(manifest)
            if slot.state in {"idle", "absent"}:
                if slot.state == "idle":
                    require_repository(self._verify_slot(slot))
                    return "released"
                if self.path(binding).exists():
                    raise VcsVerificationError("workspace", "released workspace unexpectedly exists")
                return "already_absent"
            try:
                async with self._repository_lock(binding.repository):
                    return await self._clean(binding, recycle=recycle, was_releasing=was_releasing)
            except VcsVerificationError:
                manifest = self._load()
                slot = self._owned(manifest, binding)
                if slot.state != "releasing":
                    slot.state = "quarantined"
                    self._write(manifest)
                raise

    async def _clean(
        self, binding: WorkspaceBinding, *, recycle: bool, was_releasing: bool,
    ) -> Cleanup:
        slot = self._owned(self._load(), binding)
        path = self.path(binding)
        seed = self._managed(Path("repos", *validate_repository(binding.repository)))
        cleanup: Cleanup
        if not path.exists():
            if (recycle or not was_releasing or slot.kind != "worktree"
                    or await worktree_registered(seed, path)):
                raise VcsVerificationError("workspace", "repository missing without release evidence")
            cleanup = "already_absent"
        else:
            self._verify_slot(slot)
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
            manifest = self._load()
            slot = self._owned(manifest, binding)
            self._verify_slot(slot)
            slot.state = "releasing"
            self._write(manifest)
            if recycle:
                await clean_repository(path)
            if slot.kind == "copy" or recycle:
                cleanup = "released"
            else:
                # Cleanliness was checked separately; --force permits deleting ignored caches.
                await _required_stdout("workspace", "git", "worktree", "remove", "--force",
                                       str(path), cwd=seed)
                cleanup = "removed"
        manifest = self._load()
        slot = self._owned(manifest, binding)
        if slot.state != "releasing":
            raise VcsVerificationError("workspace", "workspace release fence changed")
        slot.state = "idle" if cleanup == "released" else "absent"
        self._write(manifest)
        return cleanup

    @staticmethod
    def _settle(slot: WorkspaceSlot, binding: WorkspaceBinding, job_id: int, token: str) -> None:
        if job_id <= 0 or not token:
            raise ValueError("acknowledgement requires a job and assignment token")
        if ((slot.settled_generation, slot.settled_flow_id) == (binding.generation, binding.flow_id)
                and slot.settled_job_id is not None and (
                    job_id < slot.settled_job_id
                    or (job_id == slot.settled_job_id and token != slot.settled_assignment_token)
                )):
            raise VcsVerificationError("workspace", "stale execution acknowledgement")
        slot.settled_job_id, slot.settled_assignment_token = job_id, token
        slot.settled_generation, slot.settled_flow_id = binding.generation, binding.flow_id

    def acknowledge_target(
        self, repository: str, flow_id: int, workspace: str, generation: int, job_id: int, token: str,
    ) -> None:
        """Settle a journal's exact target, including failure before claim/bind completed.

        An unbound location may record a receipt, never acquire a Flow or advance a generation.
        If claim did establish ownership, use the identical binding fence, including quarantine.
        A location owned by another Flow or generation is never changed by this reconciliation.
        """
        validate_repository(repository)
        binding = WorkspaceBinding(repository=repository, flow_id=flow_id,
                                   slot_id=workspace, generation=generation)
        manifest = self._load()
        slot = next((s for s in manifest.slots if s.slot_id == workspace), None)
        if slot is None or slot.repository != repository:
            raise VcsVerificationError("workspace", "acknowledged workspace identity mismatch")
        if slot.binding is not None or slot.generation != generation:
            self.acknowledge(binding, job_id, token)
            return
        if slot.state not in {"idle", "absent", "quarantined"}:
            raise VcsVerificationError("workspace", "unbound workspace is not settled")
        self._settle(slot, binding, job_id, token)
        self._write(manifest)

    def acknowledge(self, binding: WorkspaceBinding, job_id: int, token: str) -> None:
        """Record a journal-validated ACK; retire only successfully cleaned allocations."""
        if job_id <= 0 or not token:
            raise ValueError("acknowledgement requires a job and assignment token")
        manifest = self._load()
        slot = next((s for s in manifest.slots if s.slot_id == binding.slot_id), None)
        if (slot is not None and slot.binding is None
                and slot.repository == binding.repository
                and slot.generation == binding.generation + 1
                and slot.settled_flow_id == binding.flow_id
                and (slot.settled_job_id, slot.settled_assignment_token) == (job_id, token)):
            return  # Replayed ACK after an interrupted caller-side retirement.
        slot = self._owned(manifest, binding)
        if slot.release_job_id is not None and (
            slot.release_job_id, slot.release_assignment_token
        ) != (job_id, token):
            raise VcsVerificationError("workspace", "acknowledgement does not match release receipt")
        self._settle(slot, binding, job_id, token)
        if slot.state in {"idle", "absent"}:
            if slot.release_job_id is None:
                raise VcsVerificationError("workspace", "cleanup has no execution receipt fence")
            if slot.state == "idle":
                self._verify_slot(slot)
            elif self.path(binding).exists():
                raise VcsVerificationError("workspace", "released workspace unexpectedly exists")
            slot.generation += 1
            slot.binding = None
            slot.release_job_id = slot.release_assignment_token = None
            slot.release_recycle = None
        self._write(manifest)
