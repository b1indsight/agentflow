"""Worker-private file identities; paths and remote credentials stay on the worker."""

import asyncio
import os
import stat
from pathlib import Path
from threading import Lock
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from app.contracts.files import MAX_FILE_BYTES, FileFailure, FileId
from app.worker.state.amp_attachments import AmpAttachment
from app.worker.state.files import write_private_record


class LocalFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    device: int
    inode: int


_RECORDS = TypeAdapter(dict[FileId, LocalFile | AmpAttachment])


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


class FileLinks:
    """Persist explicit registrations independently of the current assignment.

    Links follow in-place edits, but replacement files require a new registration:
    reused workspaces must not silently repoint an old link to another Flow's file.
    """

    def __init__(self, worker_root: Path) -> None:
        self.path = worker_root / "file-links.json"
        self._lock = Lock()

    def _load(self) -> dict[str, LocalFile | AmpAttachment]:
        try:
            return _RECORDS.validate_json(self.path.read_bytes())
        except FileNotFoundError:
            return {}

    def register(self, source: Path | AmpAttachment) -> str:
        """Validate a local file or Amp reference and durably allocate an opaque handle."""
        try:
            identity = source if isinstance(source, AmpAttachment) else _read(source.resolve(strict=True))[0]
            with self._lock:
                records = self._load()
                for handle, record in records.items():
                    if record == identity:
                        return handle
                handle = uuid4().hex
                records[handle] = identity
                write_private_record(self.path, _RECORDS.dump_json(records).decode())
                return handle
        except FileNotFoundError:
            raise FileFailure("file_not_found") from None
        except (OSError, ValueError, RuntimeError):
            raise FileFailure("file_unreadable") from None

    def _lookup(self, handle: str) -> LocalFile | AmpAttachment | None:
        with self._lock:
            return self._load().get(handle)

    async def read(self, handle: str) -> bytes:
        """Read registered bytes without blocking the driver; cancellation stops remote downloads."""
        try:
            record = await asyncio.to_thread(self._lookup, handle)
            if record is None:
                raise FileFailure("file_not_found")
            if isinstance(record, AmpAttachment):
                return await record.read(self.path.parent)
            return (await asyncio.to_thread(_read, record.path, record))[1]
        except FileNotFoundError:
            raise FileFailure("file_not_found") from None
        except (OSError, ValidationError):
            raise FileFailure("file_unreadable") from None
