"""Prepare first, fence both sides, then cooperatively hand startup to the launcher."""

import asyncio
import fcntl
import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from loguru import logger

from app.contracts.updates import ReleaseEvent, UpdateAction, UpdateIdentity, WorkerMaintenance
from app.releases import ReleaseError, release_for_sha, verify_release
from app.worker.transport.client import WorkerClient
from app.worker.updates.channel import UpdateChannel
from app.worker.updates.release import prepare, running_sha, validate_prepared
from app.worker.updates.state import LocalRequest, PreparedRelease, UpdateOperation, write_json


class UpdateCoordinator:
    """Only the runtime owning the executor and journal can authorize an update exit."""

    def __init__(
        self,
        root: Path,
        client: WorkerClient,
        channel: UpdateChannel,
        *,
        apply: bool,
        stop: Callable[[], None],
    ) -> None:
        self.root = root.expanduser().resolve()
        self.client, self.channel = client, channel
        self.apply, self.stop = apply, stop
        self.instance = uuid4().hex
        self.operation_path = self.root / "operation.json"

    def identity(self) -> UpdateIdentity:
        # Loading validates journal semantics and the actual root's device/inode.
        self.client.session.load()
        journal = self.client.session.path
        root = journal.parent.resolve()
        stat = root.stat()
        return UpdateIdentity(
            worker_id=self.client.worker_id,
            root=(str(root), stat.st_dev, stat.st_ino),
            home=str(Path.home().resolve()),
            journal=hashlib.sha256(journal.read_bytes()).hexdigest() if journal.exists() else None,
        )

    def _managed(self, prepared: PreparedRelease | None = None) -> bool:
        """Require the real parent launcher and an installed, immutable-path interpreter."""
        try:
            pointer = PreparedRelease.model_validate_json((self.root / "current.json").read_bytes())
            validate_prepared(self.root, pointer)
            claim = json.loads((self.root / "launch.json").read_bytes())
            return (
                sys.platform == "darwin"
                and claim["pid"] == os.getppid()
                and claim["token"] == os.environ.get("AGENTFLOW_LAUNCH_TOKEN")
                and claim["sha"] == running_sha() == pointer.release.sha
                and Path(sys.prefix).resolve() == pointer.path / ".venv"
                and Path(__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
                and (prepared is None or pointer == prepared)
            )
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def validate_startup(self) -> None:
        """An incomplete operation is never treated as an ordinary process restart."""
        if not self.operation_path.exists():
            return
        operation = UpdateOperation.model_validate_json(self.operation_path.read_bytes())
        if operation.phase in {"complete", "aborted"}:
            return
        if (
            operation.phase != "cutover"
            or running_sha() != operation.prepared.release.sha
            or not self._managed(operation.prepared)
            or self.identity() != operation.identity
        ):
            raise ReleaseError("incomplete update requires manual inspection; pause retained")
        claim = json.loads((self.root / "launch.json").read_bytes())
        if claim["operation"] != operation.operation_id:
            raise ReleaseError("startup authorization does not match the cutover")

    async def run(self) -> None:
        """One coordinator per updater root; prepare failures never acquire a pause."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "runtime.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.operation_path.exists():
                operation = UpdateOperation.model_validate_json(self.operation_path.read_bytes())
                if operation.phase == "cutover":
                    await self._finish(operation)
            last_notice: tuple[int, int] | None = None
            while True:
                request_path = self.root / "request.json"
                try:
                    if request_path.exists():
                        request = LocalRequest.model_validate_json(request_path.read_bytes())
                        request_path.unlink()
                        release = await asyncio.to_thread(release_for_sha, request.sha)
                        await self.update(release, apply=request.apply and self.apply)
                    elif not self.channel.notices.empty():
                        release = self.channel.notices.get_nowait()
                        key = release.run_number, release.attempt
                        if last_notice is None or key > last_notice:
                            last_notice = key
                            await self.update(release, apply=self.apply)
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    # Do not emit remote bodies, package output, env or credentials.
                    write_json(
                        self.root / "status.json",
                        {"state": "failed", "error": type(error).__name__},
                    )
                    logger.warning(
                        "worker update stopped; inspect private updater operation/status files"
                    )
                await asyncio.sleep(1)

    async def update(self, release: ReleaseEvent, *, apply: bool) -> None:
        """No await occurs between the final local idle check, pointer write and exit."""
        if running_sha() == release.sha:
            return
        if self.operation_path.exists():
            previous = UpdateOperation.model_validate_json(self.operation_path.read_bytes())
            if previous.phase not in {"complete", "aborted"}:
                raise ReleaseError("an incomplete operation already owns this updater")
            if (
                previous.phase == "complete"
                and release.run_number <= previous.prepared.release.run_number
            ):
                raise ReleaseError("release predates the adopted publication")
        prepared = await asyncio.to_thread(prepare, self.root, release)
        if not apply or not self._managed():
            write_json(
                self.root / "status.json", {"state": "checked", **prepared.model_dump(mode="json")}
            )
            return
        while self.channel.epoch is None:
            await asyncio.sleep(1)
        sha = running_sha()
        if sha is None:
            raise ReleaseError("running process has no installed release identity")
        operation = UpdateOperation(
            operation_id=uuid4().hex,
            phase="paused",
            source_instance=self.instance,
            source_epoch=self.channel.epoch,
            source_sha=sha,
            prepared=prepared,
            identity=self.identity(),
        )
        self._save(operation)  # A lost pause reply must still leave its owning operation on disk.
        try:
            async with asyncio.timeout(1800):
                while True:
                    operation.identity = self.identity()
                    self._save(operation)
                    reply = await self.channel.request(self._message(operation, "pause"))
                    if not reply.accepted:
                        raise ReleaseError("target maintenance pause was rejected")
                    if reply.idle and self.client.idle_for_update():
                        break
                    await asyncio.sleep(1)
                await asyncio.to_thread(verify_release, release)
                if not self.client.idle_for_update() or self.identity() != operation.identity:
                    raise ReleaseError("local execution changed during idle confirmation")
                # Write intent before even attempting the remote cutover transition. A lost
                # reply or pointer failure thereafter requires inspection, never auto-abort.
                operation.phase = "cutover"
                self._save(operation)
                reply = await self.channel.request(self._message(operation, "commit"))
                if not reply.accepted or not reply.idle:
                    raise ReleaseError("control-plane cutover was not confirmed")
                if (
                    not self.client.idle_for_update()
                    or self.identity() != operation.identity
                    or not self._managed()
                ):
                    raise ReleaseError("local cutover ownership changed")
                validate_prepared(self.root, prepared)
                write_json(self.root / "current.json", prepared.model_dump(mode="json"))
                self.stop()
        except BaseException:
            if operation.phase == "paused" and self.channel.epoch == operation.source_epoch:
                try:
                    reply = await self.channel.request(self._message(operation, "abort"))
                    if reply.accepted:
                        operation.phase = "aborted"
                        self._save(operation)
                except (ConnectionError, RuntimeError, TimeoutError):
                    pass  # The durable paused record remains the recovery authority.
            raise

    async def _finish(self, operation: UpdateOperation) -> None:
        self.validate_startup()
        while True:
            if self.channel.epoch is not None and self.client.idle_for_update():
                if self.identity() != operation.identity:
                    raise ReleaseError("new instance did not preserve root/HOME/journal identity")
                try:
                    reply = await self.channel.request(self._message(operation, "resume"))
                except (ConnectionError, TimeoutError):
                    await asyncio.sleep(1)
                    continue
                if reply.accepted:
                    operation.phase = "complete"
                    self._save(operation)
                    write_json(
                        self.root / "status.json", {"state": "complete", "sha": running_sha()}
                    )
                    return
            await asyncio.sleep(1)

    def _save(self, operation: UpdateOperation) -> None:
        write_json(self.operation_path, operation.model_dump(mode="json"))

    def _message(self, operation: UpdateOperation, action: UpdateAction) -> WorkerMaintenance:
        sha, epoch = running_sha(), self.channel.epoch
        if sha is None or epoch is None:
            raise ConnectionError("update requires a registered installed release")
        return WorkerMaintenance(
            action=action,
            request_id=uuid4().hex,
            operation_id=operation.operation_id,
            instance_id=self.instance,
            connection_epoch=epoch,
            running_sha=sha,
            release=operation.prepared.release,
            identity=self.identity(),
        )
