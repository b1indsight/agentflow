"""Worker-private file identities; paths and remote credentials stay on the worker."""

from pathlib import Path
from threading import Lock
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter

from app.contracts.files import FileFailure, FileId
from app.worker.state.files import write_private_record

_RECORDS = TypeAdapter(dict[FileId, dict[str, JsonValue]])


class FileLinks:
    """Persist explicit registrations independently of the current assignment.

    Links follow in-place edits, but replacement files require a new registration:
    reused workspaces must not silently repoint an old link to another Flow's file.
    """

    def __init__(self, worker_root: Path) -> None:
        self.path = worker_root / "file-links.json"
        self._lock = Lock()

    def _load(self) -> dict[str, dict[str, JsonValue]]:
        try:
            return _RECORDS.validate_json(self.path.read_bytes())
        except FileNotFoundError:
            return {}

    def register(self, backend: str, source: dict[str, JsonValue]) -> str:
        """Persist an agent-validated identity, deduplicating across local backends."""
        try:
            with self._lock:
                records = self._load()
                for handle, record in records.items():
                    if record.get("source", record) == source:
                        return handle
                handle = uuid4().hex
                records[handle] = {"backend": backend, "source": source}
                write_private_record(self.path, _RECORDS.dump_json(records).decode())
                return handle
        except (OSError, ValueError, RuntimeError):
            raise FileFailure("file_unreadable") from None

    def lookup(self, handle: str) -> tuple[str | None, dict[str, JsonValue]]:
        """Recover ownership and opaque source data; old records have no ownership metadata."""
        try:
            with self._lock:
                record = self._load().get(handle)
            if record is None:
                raise FileFailure("file_not_found")
            if "source" not in record:
                return None, record
            backend, source = record.get("backend"), record["source"]
            if not isinstance(backend, str) or not isinstance(source, dict):
                raise FileFailure("file_unreadable")
            return backend, source
        except (OSError, ValueError):
            raise FileFailure("file_unreadable") from None
