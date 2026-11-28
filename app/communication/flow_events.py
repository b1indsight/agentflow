"""Best-effort console notifications from local processes to a single serve process."""

import asyncio
import hashlib
import os
import stat
from collections import deque
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from select import PIPE_BUF
from tempfile import gettempdir
from time import time_ns
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, ValidationError


class DisplayEvent(BaseModel):
    event_type: str
    flow_id: int
    payload: dict[str, object]


def pipe_path(namespace: str) -> Path:
    # Separate installations sharing a host without exposing the database URL.
    key = hashlib.sha256(namespace.encode()).hexdigest()[:24]
    return Path(gettempdir()) / f"agentflow-{os.getuid()}-{key}.fifo"


def _open_fifo(path: Path, mode: int) -> int:
    fd = os.open(path, mode | os.O_NONBLOCK | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise PermissionError(f"expected a private FIFO owned by this user: {path}")
    return fd


def publish(path: Path, events: list[DisplayEvent]) -> None:
    """Never block the committing process; no reader or a full pipe means a missed UI update."""
    try:
        fd = _open_fifo(path, os.O_WRONLY)
        try:
            for event in events:
                frame = event.model_dump_json().encode() + b"\n"
                # One atomic write per notice so concurrent producers cannot interleave records.
                if len(frame) <= PIPE_BUF:
                    os.write(fd, frame)
        finally:
            os.close(fd)
    except OSError:
        return


REPLAY_LIMIT = 256


class FlowEventStream:
    """The sole FIFO reader assigns IDs and shares one bounded replay window across tabs."""

    def __init__(self) -> None:
        self._server = uuid4()
        self._ms = self._seq = 0
        self._recent: deque[tuple[str, DisplayEvent | None]] = deque(
            [(self._identity(), None)], maxlen=REPLAY_LIMIT + 1,
        )
        self._changed = asyncio.Event()
        self._fd: int | None = None
        self._tail = b""

    def _identity(self) -> str:
        return f"{self._ms:013d}-{self._seq:019d}-{self._server}"

    def start(self, path: Path) -> None:
        with suppress(FileExistsError):
            os.mkfifo(path, 0o600)
        # Holding the write side too prevents EOF between short-lived CLI writers.
        fd = _open_fifo(path, os.O_RDWR)
        try:
            asyncio.get_running_loop().add_reader(fd, self._receive, fd)
        except BaseException:
            os.close(fd)
            raise
        self._path, self._fd = path, fd

    def close(self) -> None:
        if self._fd is not None:
            asyncio.get_running_loop().remove_reader(self._fd)
            self._path.unlink(missing_ok=True)
            os.close(self._fd)
            self._fd = None

    def _receive(self, fd: int) -> None:
        lines = (self._tail + os.read(fd, 65536)).split(b"\n")
        self._tail = lines.pop()
        for line in lines:
            try:
                event = DisplayEvent.model_validate_json(line)
            except ValidationError:
                logger.warning("Ignoring invalid console event on local FIFO")
                continue
            self._ms = max(self._ms, time_ns() // 1_000_000)
            self._seq += 1
            self._recent.append((self._identity(), event))
            self._changed.set()

    async def after(self, cursor: str | None) -> AsyncIterator[tuple[str, DisplayEvent | None]]:
        """Yield the suffix after a known cursor, or a ready watermark to refresh the board."""
        while True:
            head = self._recent[-1][0]
            if cursor == head:
                self._changed.clear()
                await self._changed.wait()
                continue
            # Copy before yielding: the FIFO can advance while a tab renders its projection.
            batch = list(self._recent)
            for i, (identity, _) in enumerate(batch):
                if identity == cursor:
                    for entry in batch[i + 1:]:
                        yield entry
                    break
            else:
                yield head, None
            cursor = head
