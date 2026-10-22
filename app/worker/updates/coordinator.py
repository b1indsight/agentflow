"""Prepare a release, pause new assignments, then exit for an ordinary reconnect."""

import asyncio
import fcntl
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from app.releases import ReleaseError, ReleaseEvent, latest_release, release_for_sha, verify_release
from app.worker.transport.client import WorkerClient
from app.worker.updates.release import prepare, running_sha, validate_prepared
from app.worker.updates.state import LocalRequest, PreparedRelease, write_json


class UpdateCoordinator:
    """Only the runtime owning the executor and journal can authorize an update exit."""

    def __init__(
        self,
        root: Path,
        client: WorkerClient,
        *,
        apply: bool,
        stop: Callable[[], None],
    ) -> None:
        self.root = root.expanduser().resolve()
        self.client = client
        self.apply, self.stop = apply, stop

    def _managed(self) -> bool:
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
            )
        except (OSError, ValueError, KeyError, TypeError):
            return False

    async def run(self) -> None:
        """Serialize local requests and publication checks under the updater-root lock."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "runtime.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            last_notice: tuple[int, int] | None = None
            next_check = 0.0
            while True:
                request_path = self.root / "request.json"
                try:
                    if request_path.exists():
                        request = LocalRequest.model_validate_json(request_path.read_bytes())
                        request_path.unlink()
                        release = await asyncio.to_thread(release_for_sha, request.sha)
                        await self.update(release, apply=request.apply and self.apply)
                    elif time.monotonic() >= next_check:
                        next_check = time.monotonic() + 300
                        release = await asyncio.to_thread(latest_release)
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
                    logger.warning("worker update stopped; inspect private updater status file")
                await asyncio.sleep(1)

    async def update(self, release: ReleaseEvent, *, apply: bool) -> None:
        """Use an acknowledged connection pause; startup needs no upgrade handshake."""
        if running_sha() == release.sha:
            return
        pointer = self.root / "current.json"
        if pointer.exists():
            current = PreparedRelease.model_validate_json(pointer.read_bytes())
            if release.run_number <= current.release.run_number:
                raise ReleaseError("release predates the installed publication")
        prepared = await asyncio.to_thread(prepare, self.root, release)
        if not apply or not self._managed():
            write_json(
                self.root / "status.json", {"state": "checked", **prepared.model_dump(mode="json")}
            )
            return
        channel = self.client.availability
        epoch: str | None = None
        try:
            async with asyncio.timeout(1800):
                while True:
                    if channel.epoch is None:
                        await asyncio.sleep(1)
                        continue
                    if channel.epoch != epoch:
                        # Remember the attempt before sending: a lost ACK can still
                        # mean this connection has been paused on the Server.
                        epoch = channel.epoch
                        try:
                            await channel.set_paused(True)
                        except ConnectionError:
                            if channel.epoch == epoch:
                                raise
                            continue
                    if not self.client.idle_for_update():
                        await asyncio.sleep(1)
                        continue
                    await asyncio.to_thread(verify_release, release)
                    if channel.epoch != epoch or not self.client.idle_for_update():
                        continue
                    if not self._managed():
                        raise ReleaseError("local launcher ownership changed")
                    # No await from the final epoch/idle check through pointer write
                    # and cooperative stop: a new assignment cannot slip between them.
                    validate_prepared(self.root, prepared)
                    write_json(pointer, prepared.model_dump(mode="json"))
                    self.stop()
                    return
        except BaseException:
            if epoch is not None and channel.epoch == epoch:
                try:
                    await channel.set_paused(False)
                except (ConnectionError, TimeoutError):
                    # There is no durable pause to recover. Closing this socket
                    # restores normal availability through the existing reconnect.
                    if channel.epoch == epoch:
                        await channel.close()
            raise
