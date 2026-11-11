"""Worker-owned directories; the controller assigns generations and the runtime fences writers.

The runtime holds WorkerRootLock and proves the previous executor has stopped before accepting
a newer allocation. Filesystem failures are operation results, never persistent slot states.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from app.contracts.protocol import ProvisionError
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


class RepositoryProvisionError(VcsVerificationError):
    """A local capacity refusal that does not invalidate the shared worker manifest."""

    def __init__(self, code: ProvisionError, message: str) -> None:
        super().__init__("workspace", message)
        self.code = code


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
    directory_identity: tuple[int, int] | None = None
    # Exact destructive-operation intent, persisted only after physical ownership checks.
    release_job_id: int | None = None
    release_assignment_token: str | None = None

    @model_validator(mode="after")
    def _fences(self) -> Self:
        if self.binding is not None and (
            self.binding.slot_id, self.binding.repository, self.binding.generation
        ) != (self.slot_id, self.repository, self.generation):
            raise ValueError("workspace binding does not match inventory")
        if (self.release_job_id is None) != (self.release_assignment_token is None):
            raise ValueError("incomplete release receipt fence")
        return self


class InferenceWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    directory_identity: tuple[int, int] | None = None


class WorkspaceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[3] = 3
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
    lock. Business acknowledgements never modify directories or allocation fences.
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
                        relative_path=s.relative_path, kind=s.kind,
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
                                  generation=1, flow_id=None)

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
        if (slot.directory_identity is None
                or self._directory_identity(path) != slot.directory_identity):
            raise VcsVerificationError("workspace", "workspace directory identity changed")
        return path

    def inventory(self) -> list[WorkspaceInventory]:
        """Report registered identities without turning transient IO failures into pool state.

        A removed archive still has an allocation record. Only an unfinished provisioning
        attempt with no binding or proven directory is omitted; it is retried in place.
        """
        manifest = self._load()
        self._verify_inference(manifest)
        assert manifest.inference is not None
        items = [WorkspaceInventory(workspace=manifest.inference.slot_id, repository=None,
                                    generation=1, flow_id=None)]
        for slot in manifest.slots:
            if slot.directory_identity is None and slot.binding is None:
                continue
            items.append(WorkspaceInventory(
                workspace=slot.slot_id, repository=slot.repository, generation=slot.generation,
                flow_id=slot.binding.flow_id if slot.binding is not None else None,
            ))
        return items

    def _materialize(self, slot: WorkspaceSlot) -> Path:
        """Create only an explicitly unmaterialized location, never adopt existing contents."""
        if slot.directory_identity is not None:
            return self._verify_slot(slot)
        path = self._managed(Path(slot.relative_path))
        if path.exists():
            raise VcsVerificationError("workspace", "unregistered workspace unexpectedly exists")
        path.mkdir(parents=True, mode=0o700)
        slot.directory_identity = self._directory_identity(path)
        sync_directory(path.parent)
        return path

    async def provision(self, repository: str) -> list[WorkspaceInventory]:
        """Register empty concrete directories up to this repository's configured quota."""
        try:
            owner, name = validate_repository(repository)
        except VcsVerificationError as error:
            raise RepositoryProvisionError(ProvisionError.INVALID_REPOSITORY, str(error)) from error
        self.initialize()
        manifest = self._load()
        # Shared root/manifest failures remain fatal. Only this repository's operations
        # below may become an ordinary capacity result on the shared connection.
        self._managed(Path("workspaces"))
        slots = [s for s in manifest.slots if s.repository == repository]
        known = {s.slot_id for s in slots}
        try:
            directory = self._managed(Path("workspaces", owner, name))
            if directory.exists() and any(p.name not in known for p in directory.iterdir()):
                raise VcsVerificationError("workspace", "unregistered repository workspace; refusing adoption")
        except VcsVerificationError as error:
            raise RepositoryProvisionError(ProvisionError.WORKSPACE_CONFLICT, str(error)) from error
        except OSError as error:
            raise RepositoryProvisionError(ProvisionError.FILESYSTEM_ERROR, "repository directory unavailable") from error
        # Failed creation reserves its ID, not a permanent failure state or an extra slot.
        # No awaits in this transaction: concurrent demands cannot over-provision.
        for index in range(max(len(slots), self.limit)):
            if index == len(slots):
                slot_id = uuid4().hex
                slot = WorkspaceSlot(
                    slot_id=slot_id, repository=repository, generation=1,
                    relative_path=str(Path("workspaces", owner, name, slot_id)),
                    kind="worktree" if any(s.kind == "copy" for s in slots) else "copy",
                )
                slots.append(slot)
                manifest.slots.append(slot)
                self._write(manifest)
            else:
                slot = slots[index]
            if slot.binding is not None:
                continue
            try:
                self._materialize(slot)
            except VcsVerificationError as error:
                raise RepositoryProvisionError(ProvisionError.WORKSPACE_CONFLICT, str(error)) from error
            except OSError as error:
                raise RepositoryProvisionError(ProvisionError.FILESYSTEM_ERROR, "workspace creation failed") from error
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
        if len(slots) != 1:
            raise VcsVerificationError("workspace", "original workspace binding is missing")
        require_repository(self._verify_slot(slots[0]))
        assert slots[0].binding is not None
        return slots[0].binding

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
        if slot is None or slot.repository != repository or generation < slot.generation:
            raise VcsVerificationError("workspace", "assigned workspace identity or generation mismatch")
        binding = WorkspaceBinding(repository=repository, slot_id=workspace,
                                   flow_id=flow_id, generation=generation)
        if slot.binding is not None and slot.binding != binding and generation == slot.generation:
            raise VcsVerificationError("workspace", "assigned workspace is still bound")
        if slot.binding == binding and slot.directory_identity is not None:
            path = self._verify_slot(slot)
            if any(path.iterdir()):
                require_repository(path)
                return binding
        # A failed bootstrap can leave an owned empty directory or a removed partial copy.
        # Retry preparation in place; a populated existing repository keeps its Flow's edits.
        for previous in manifest.slots:
            if previous.slot_id != workspace and previous.binding is not None and previous.binding.flow_id == flow_id:
                # Runtime has joined/validated this Flow before accepting its new INITIAL.
                previous.binding = None
        if generation > slot.generation:
            slot.release_job_id = slot.release_assignment_token = None
        slot.binding, slot.generation = binding, generation
        self._write(manifest)
        path = self._materialize(slot)
        self._write(manifest)
        # The seed and its worktree registry are shared; the manifest and independent repos are not.
        async with self._repository_lock(repository):
            await self._prepare(binding, slot.kind, path, owner, name,
                                base_branch, remote, push_token)
        self._verify_slot(self._owned(self._load(), binding))
        return binding

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
                await clean_repository(path)
                await align_repository(path, seed, base_branch)
            except BaseException:
                if copying:
                    self._verify_slot(self._owned(self._load(), binding))
                    await filesystem_operation(shutil.rmtree, path)
                    manifest = self._load()
                    self._owned(manifest, binding).directory_identity = None
                    self._write(manifest)
                raise
        else:
            if not (seed / ".git").is_dir():
                raise VcsVerificationError("workspace", "overflow requires a standalone Git seed")
            if empty:
                await _required_stdout("workspace", "git", "worktree", "add", "--detach",
                                       str(path), f"refs/heads/{base_branch}", cwd=seed)
            else:
                await self._verify_worktree(path, seed)
                await clean_repository(path)
                await _required_stdout("workspace", "git", "checkout", "--no-overwrite-ignore",
                                       "--detach", f"refs/heads/{base_branch}", cwd=path)

    async def release(self, binding: WorkspaceBinding, *, job_id: int, token: str) -> Cleanup:
        """Perform the archive's cleanup without changing controller-owned allocation.

        The operation intent authorizes replay after worktree removal, including a crash
        before the result was journaled. It never authorizes reuse or clears a binding.
        """
        if job_id <= 0 or not token:
            raise ValueError("release requires a job and assignment token")
        async with (
            self._release_locks.setdefault(binding.slot_id, asyncio.Lock()),
            self._repository_lock(binding.repository),
        ):
            return await self._clean(binding, job_id, token)

    async def _verify_worktree(self, path: Path, seed: Path) -> None:
        common = await _required_stdout(
            "workspace", "git", "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path,
        )
        if Path(common) != seed / ".git" or not await worktree_registered(seed, path):
            raise VcsVerificationError("workspace", "worktree ownership does not match seed")

    async def _clean(self, binding: WorkspaceBinding, job_id: int, token: str) -> Cleanup:
        slot = self._owned(self._load(), binding)
        if slot.release_job_id is not None and (
            job_id < slot.release_job_id
            or (job_id == slot.release_job_id and token != slot.release_assignment_token)
        ):
            raise VcsVerificationError("workspace", "stale cleanup execution")
        path = self.path(binding)
        seed = self._managed(Path("repos", *validate_repository(binding.repository)))
        cleanup: Cleanup
        if not path.exists():
            if (slot.release_job_id is None or slot.kind != "worktree"
                    or await worktree_registered(seed, path)):
                raise VcsVerificationError("workspace", "repository missing without release evidence")
            cleanup = "already_absent"
        else:
            self._verify_slot(slot)
            await require_idle_repository(path)
            if slot.kind == "copy" and (path / ".git").is_file():
                raise VcsVerificationError("workspace", "full copy has external Git metadata")
            if slot.kind == "worktree":
                await self._verify_worktree(path, seed)
            manifest = self._load()
            slot = self._owned(manifest, binding)
            self._verify_slot(slot)
            slot.release_job_id, slot.release_assignment_token = job_id, token
            self._write(manifest)
            if slot.kind == "copy":
                cleanup = "released"
            else:
                # Cleanliness was checked separately; --force permits deleting ignored caches.
                await _required_stdout("workspace", "git", "worktree", "remove", "--force",
                                       str(path), cwd=seed)
                cleanup = "removed"
        manifest = self._load()
        slot = self._owned(manifest, binding)
        slot.release_job_id, slot.release_assignment_token = job_id, token
        if cleanup != "released":
            slot.directory_identity = None
        self._write(manifest)
        return cleanup
