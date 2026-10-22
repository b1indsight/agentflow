"""Completed release metadata, separate from the assignment journal."""

import json
import os
import tempfile
from pathlib import Path

from app.contracts.protocol_base import ProtocolModel
from app.releases import ReleaseEvent


class PreparedRelease(ProtocolModel):
    release: ReleaseEvent
    path: Path


def write_json(path: Path, value: object) -> None:
    """Replace a private file only after flushing content, then flush the directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
