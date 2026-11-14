"""Durable private worker records, published atomically under the worker root lock."""

import os
import tempfile
from pathlib import Path


def sync_directory(path: Path) -> None:
    """Persist directory entries after publishing, moving or removing private records."""
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_private_record(path: Path, payload: str) -> None:
    """Fsync both the replacement file and its parent before returning."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=f".{path.name}.",
                                         delete=False) as handle:
            temporary = handle.name
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
