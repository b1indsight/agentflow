"""Read Codex rollout JSONL on demand, without executing Codex or retaining history."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path
from typing import BinaryIO

from app.contracts.conversation import (
    ConversationContent,
    ConversationFailure,
    HistoryItem,
    HistoryPage,
    HistoryQuery,
)

MAX_LINE = 256 * 1024
MAX_SCAN = 8 * 1024 * 1024
MAX_PAGE = 256 * 1024


class CodexHistory:
    """Read only explicit session IDs under the same CODEX_HOME used by Codex exec.

    Rollout response_item records are canonical; event_msg duplicates and reasoning
    records are deliberately excluded. Each page scans at most 8 MiB of complete lines.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))).resolve()
        self._reads = threading.BoundedSemaphore(2)

    def query(self, query: HistoryQuery) -> HistoryPage:
        """Read one validated session query without blocking the event loop's caller."""
        # Hold the permit in the thread: cancelling to_thread cannot stop file I/O.
        if not self._reads.acquire(blocking=False):
            raise ConversationFailure("query_busy")
        try:
            return self._read(query)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ConversationFailure("history_unreadable") from exc
        finally:
            self._reads.release()

    def _path(self, session_id: str) -> Path | None:
        # Prefer the active copy during archival; opening validates the candidate.
        count = 0
        for directory in (self.root / "sessions", self.root / "archived_sessions"):
            if not directory.exists():
                continue
            if directory.is_symlink():
                # followlinks=False does not protect the walk's starting directory.
                raise ConversationFailure("history_unreadable")
            for parent, dirs, names in os.walk(directory, followlinks=False, onerror=self._walk_error):
                count += len(dirs) + len(names)
                if count > 50000:
                    raise ConversationFailure("history_limit")
                for name in names:
                    if not name.startswith("rollout-") or not name.endswith(".jsonl"):
                        continue
                    if name[-42:-6] != session_id:
                        continue
                    return Path(parent) / name
        return None

    @staticmethod
    def _walk_error(error: OSError) -> None:
        raise error

    def _read(self, query: HistoryQuery) -> HistoryPage:
        """Locate the source, validate its position, and read one bounded page."""
        path = self._path(query.session_id)
        if path is None:
            if query.cursor is not None:
                raise ConversationFailure("cursor_invalid")
            return HistoryPage(session_id=query.session_id, status="not_found")
        with self._open(path) as file:
            identity = self._seek_history(file, query)
            return self._read_page(file, query, identity)

    @staticmethod
    def _seek_history(file: BinaryIO, query: HistoryQuery) -> str:
        """Validate the source and cursor, seek to the first record, and return its identity."""
        offset, previous_identity = query.position
        file_stat = os.fstat(file.fileno())
        # Report a stale cursor before parsing metadata, even if the file was emptied.
        if previous_identity is not None and offset > file_stat.st_size:
            raise ConversationFailure("cursor_invalid")
        meta_line = file.readline(MAX_LINE + 1)
        if len(meta_line) > MAX_LINE or not meta_line.endswith(b"\n"):
            raise ConversationFailure("history_unreadable")
        meta = json.loads(meta_line)
        if meta.get("type") != "session_meta" or meta.get("payload", {}).get("id") != query.session_id:
            raise ConversationFailure("history_unreadable")
        identity = f"{file_stat.st_dev}:{file_stat.st_ino}:{hashlib.sha256(meta_line).hexdigest()}"
        if previous_identity is None:
            return identity
        # Query validation cannot detect file replacement or a cursor inside a record.
        if identity != previous_identity or offset < len(meta_line):
            raise ConversationFailure("cursor_invalid")
        file.seek(offset - 1)
        if file.read(1) != b"\n":
            raise ConversationFailure("cursor_invalid")
        return identity

    def _read_page(self, file: BinaryIO, query: HistoryQuery, identity: str) -> HistoryPage:
        """Scan complete records, retaining the offset of any unconsumed record."""
        page = HistoryPage(session_id=query.session_id)
        offset = file.tell()
        scanned = size = 0
        while True:
            raw = file.readline(MAX_LINE + 1)
            if len(raw) > MAX_LINE:
                raise ConversationFailure("history_limit")
            if not raw.endswith(b"\n"):
                # Leave a partially written final record for the next poll.
                break
            scanned += len(raw)
            item, unsupported = self._item(json.loads(raw), query.session_id, offset)
            if unsupported:
                page.status = "unsupported"
            if item is not None:
                item_size = len(item.model_dump_json().encode())
                if len(page.items) >= query.limit or size + item_size > MAX_PAGE:
                    # Keep offset before this record: it belongs to the next page.
                    page.has_more = True
                    break
                page.items.append(item)
                size += item_size
            offset = file.tell()
            if scanned >= MAX_SCAN:
                page.has_more = self._has_complete_line(file)
                break
        page.next_cursor = query.cursor_at(offset, identity)
        return page

    def _open(self, path: Path) -> BinaryIO:
        # Resolve each component with no-follow opens: checking resolve() alone has a
        # race if a writable directory is replaced by a symlink between check and open.
        parts = path.relative_to(self.root).parts
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ConversationFailure("history_unreadable")
        return os.fdopen(descriptor, "rb")

    @staticmethod
    def _has_complete_line(file: BinaryIO) -> bool:
        return file.readline(MAX_LINE + 1).endswith(b"\n")

    @staticmethod
    def _item(row: dict, session_id: str, offset: int) -> tuple[HistoryItem | None, bool]:
        if row.get("type") != "response_item":
            return None, False
        payload = row.get("payload", {})
        kind = payload.get("type")
        if kind == "reasoning" or payload.get("phase") == "analysis" or payload.get("channel") == "analysis":
            return None, False
        data = {"id": f"{session_id}:{offset}", "timestamp": row.get("timestamp")}
        truncated = False

        def text(value: str) -> str:
            nonlocal truncated
            if not isinstance(value, str):
                raise TypeError("non-text history field")
            # Known runtime credentials and common token forms must not leak via tool text.
            for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "FLOWTOOL_WORKER_TOKEN"):
                secret = os.environ.get(key)
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,})",
                           "[REDACTED]", value)
            value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+", r"\1[REDACTED]", value)
            truncated |= len(value) > 8192
            return value[:8192]

        if kind == "message":
            role = payload.get("role")
            if role not in {"user", "assistant", "system", "developer"}:
                return None, True
            blocks = payload.get("content", [])
            unsupported = any(block.get("type") not in {"input_text", "output_text"} for block in blocks)
            content = ConversationContent(text=text("\n".join(
                block["text"] for block in blocks if block.get("type") in {"input_text", "output_text"}
            )))
            data.update(role=role, kind="message", content=content)
        elif kind in {"function_call", "custom_tool_call"}:
            unsupported = False
            content = ConversationContent(name=payload["name"], arguments=text(
                payload["arguments"] if kind == "function_call" else payload["input"],
            ))
            data.update(role="assistant", kind="tool_call", call_id=payload["call_id"], content=content)
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            unsupported = False
            output = payload["output"]
            if isinstance(output, list):
                unsupported = any(block.get("type") not in {"input_text", "output_text"} for block in output)
                output = "\n".join(block["text"] for block in output
                                   if block.get("type") in {"input_text", "output_text"})
            data.update(role="tool", kind="tool_result", call_id=payload["call_id"],
                        content=ConversationContent(text=text(output)))
        else:
            return None, True
        return HistoryItem.model_validate({**data, "truncated": truncated}), unsupported
