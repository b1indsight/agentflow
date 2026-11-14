"""Path-free file-link identifiers, transfer limits and operational errors."""

from typing import Annotated, Literal

from pydantic import Field

# Base64 plus the envelope stays below the worker WebSocket's 1 MiB frame limit.
MAX_FILE_BYTES = 512 * 1024
MAX_FILE_BASE64 = 4 * ((MAX_FILE_BYTES + 2) // 3)
FileId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
FileError = Literal[
    "file_not_found", "file_unreadable", "file_changed", "file_too_large",
    "worker_unavailable", "query_busy", "query_timeout", "invalid_response",
    "registration_unavailable",
]


class FileFailure(Exception):
    """Safe to expose at the HTTP boundary; never contains a filesystem exception."""

    def __init__(self, code: FileError) -> None:
        self.code = code
        super().__init__(code)
