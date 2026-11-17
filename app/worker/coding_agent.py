"""Worker-local execution, history and file contract with safe local-file defaults."""

import asyncio
import os
import stat
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from app.contracts.conversation import HistoryPage, HistoryQuery
from app.contracts.files import MAX_FILE_BYTES, FileFailure
from app.domain.models import CodingExecutionContext, CodingExecutionResult
from app.worker.state.file_links import FileLinks


class LocalFile(BaseModel):
    """An explicitly registered inode; in-place edits are allowed, replacement is not."""

    model_config = ConfigDict(extra="forbid")

    path: Path
    device: int
    inode: int


def _read(path: Path, expected: LocalFile | None = None) -> tuple[LocalFile, bytes]:
    # Opening nonblocking prevents a replaced file/FIFO from hanging a worker thread.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise FileFailure("file_unreadable")
        identity = LocalFile(path=path, device=metadata.st_dev, inode=metadata.st_ino)
        if expected is not None and identity != expected:
            raise FileFailure("file_changed")
        if metadata.st_size > MAX_FILE_BYTES:
            raise FileFailure("file_too_large")
        content = stream.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise FileFailure("file_too_large")
        return identity, content


class CodingAgent(Protocol):
    """Adapt a native coding backend and its history without owning business results.

    Coding requires the operator's trusted-host opt-in and external isolation.
    Reading explicit historical sessions is independent of coding authorization
    and the active assignment; callers offload the synchronous read to a thread.
    """

    backend: str

    async def start(
        self, task: str, cwd: Path, *, output_schema: Path | None = None,
        context: CodingExecutionContext | None = None, model: str | None = None,
        effort: str | None = None, max_output_bytes: int | None = None,
    ) -> CodingExecutionResult: ...

    async def resume(
        self, session_id: str, prompt: str, *, cwd: Path, output_schema: Path | None = None,
        context: CodingExecutionContext | None = None,
    ) -> CodingExecutionResult: ...

    def read_history(self, query: HistoryQuery) -> HistoryPage: ...

    def register_file(self, source: Path | str, files: FileLinks) -> str:
        """Register a local path; backends may additionally accept their native URI strings."""
        if not isinstance(source, Path):
            raise FileFailure("file_unreadable")
        try:
            identity, _ = _read(source.resolve(strict=True))
            return files.register(self.backend, identity.model_dump(mode="json"))
        except FileNotFoundError:
            raise FileFailure("file_not_found") from None
        except (OSError, ValueError, RuntimeError):
            raise FileFailure("file_unreadable") from None

    async def read_file(self, source: dict[str, JsonValue], *, root: Path) -> bytes:
        """Validate this backend's stored identity and read bounded bytes outside the driver."""
        try:
            record = LocalFile.model_validate(source)
            return (await asyncio.to_thread(_read, record.path, record))[1]
        except FileNotFoundError:
            raise FileFailure("file_not_found") from None
        except (OSError, ValidationError):
            raise FileFailure("file_unreadable") from None
