"""Worker-private file identities; paths never leave this registry."""

import os
import stat
from pathlib import Path
from threading import Lock
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from app.contracts.files import MAX_FILE_BYTES, FileFailure, FileId
from app.worker.state.files import write_private_record


class LocalFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    device: int
    inode: int


_RECORDS = TypeAdapter(dict[FileId, LocalFile])


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


class LocalFileLinks:
    """Persist explicit registrations independently of the current assignment.

    Links follow in-place edits, but replacement files require a new registration:
    reused workspaces must not silently repoint an old link to another Flow's file.
    """

    def __init__(self, worker_root: Path) -> None:
        self.path = worker_root / "file-links.json"
        self._lock = Lock()

    def _load(self) -> dict[str, LocalFile]:
        try:
            return _RECORDS.validate_json(self.path.read_bytes())
        except FileNotFoundError:
            return {}

    def register(self, path: Path) -> str:
        """Validate a readable regular file and durably allocate an opaque handle."""
        try:
            identity, _ = _read(path.resolve(strict=True))
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

    def read(self, handle: str) -> bytes:
        """Read only a registered identity, with a hard byte limit and safe errors."""
        try:
            with self._lock:
                record = self._load().get(handle)
            if record is None:
                raise FileFailure("file_not_found")
            return _read(record.path, record)[1]
        except FileNotFoundError:
            raise FileFailure("file_not_found") from None
        except (OSError, ValidationError):
            raise FileFailure("file_unreadable") from None
