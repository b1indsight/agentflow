"""Allowlisted private Amp attachments, fetched with the worker's native sign-in."""

import os
import re
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

from app.contracts.files import MAX_FILE_BYTES, FileFailure
from app.worker.execution.amp_agent import AmpCodingAgent, amp_environment


class AmpAttachment(BaseModel):
    """A remote file identity; media-source fragments never grant local file access."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str

    @field_validator("url")
    @classmethod
    def canonical_url(cls, value: str) -> str:
        url = value.split("#", 1)[0]
        match = re.fullmatch(
            r"https://ampcode\.com/user-content/attachments/[0-9a-f]{64}-"
            r"((?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+)", url,
        )
        if len(url) > 1024 or match is None:
            raise ValueError("invalid Amp attachment URL")
        name = unquote(match[1], errors="strict")
        if name in {".", ".."} or re.search(r"[/\\\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]", name):
            raise ValueError("invalid Amp attachment name")
        return url

    @property
    def name(self) -> str:
        return unquote(urlsplit(self.url).path.rsplit("/", 1)[1][65:])

    async def read(self, worker_root: Path) -> bytes:
        """Download on demand, bound disk writes before exec, and always remove the copy."""
        try:
            with TemporaryDirectory(prefix=".amp-attachment-", dir=worker_root) as directory:
                target = Path(directory) / "attachment"
                result = await AmpCodingAgent.run_process(
                    ["files", "get", self.url, "-o", str(target), "--log-file", os.devnull],
                    env=amp_environment(), timeout=5, max_stdout=65536,
                    # One sentinel byte distinguishes oversize writes from an exact-limit file.
                    max_file_bytes=MAX_FILE_BYTES + 1,
                )
                # Amp stages downloads next to the destination before renaming them.
                if any(path.is_file() and path.stat().st_size > MAX_FILE_BYTES
                       for path in Path(directory).rglob("*")):
                    raise FileFailure("file_too_large")
                if result.timed_out:
                    raise FileFailure("query_timeout")
                if result.exit_code or result.overflow:
                    raise FileFailure("file_unreadable")
                if not stat.S_ISREG(target.lstat().st_mode):
                    raise FileFailure("file_unreadable")
                with target.open("rb") as stream:
                    content = stream.read(MAX_FILE_BYTES + 1)
                if len(content) > MAX_FILE_BYTES:
                    raise FileFailure("file_too_large")
                return content
        except OSError:
            raise FileFailure("file_unreadable") from None
