"""Bounded read-only projection of Amp's public thread export."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.contracts.conversation import (
    ConversationContent,
    ConversationFailure,
    HistoryItem,
    HistoryPage,
    HistoryQuery,
)
from app.worker.execution.amp_cli import amp_environment, run_amp, write_amp_settings

MAX_EXPORT = 2 * 1024 * 1024
MAX_ITEMS = 500
EXPORT_TIMEOUT = 20
CACHE_SIZE = 16
CACHE_SECONDS = 2
MAX_PAGE = 256 * 1024
_SECRET_PATTERN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,})"
)
_BEARER_PATTERN = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+")


@dataclass
class _Snapshot:
    items: list[HistoryItem]
    unsupported: bool
    fetched_at: float


class AmpHistory:
    """Fetch an Amp thread on demand without depending on Amp's local storage.

    Cursors hold a digest of every consumed public block. This lets an external
    Amp client append messages, while a changed earlier message invalidates a stale
    cursor rather than silently replaying a different history.
    """

    def __init__(self) -> None:
        self._reads = threading.BoundedSemaphore(2)
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, _Snapshot] = OrderedDict()

    def query(self, query: HistoryQuery) -> HistoryPage:
        """Synchronously export and page a thread; callers run this in ``to_thread``."""
        if not self._reads.acquire(blocking=False):
            raise ConversationFailure("query_busy")
        try:
            return self._page(query, self._refresh(query.session_id))
        except ConversationFailure:
            raise
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ConversationFailure("history_unreadable") from exc
        finally:
            self._reads.release()

    def _refresh(self, session_id: str) -> _Snapshot | None:
        """Cache only the public, redacted projection; refresh at most once per two seconds."""
        with self._lock:
            cached = self._cache.get(session_id)
            if cached is not None and time.monotonic() - cached.fetched_at < CACHE_SECONDS:
                self._cache.move_to_end(session_id)
                return cached
        with tempfile.TemporaryDirectory(prefix="flowtool-amp-export-") as directory:
            settings = write_amp_settings(Path(directory))
            result = asyncio.run(run_amp(
                ["threads", "export", session_id, "--settings-file", str(settings),
                 "--log-file", os.devnull],
                env=amp_environment(), timeout=EXPORT_TIMEOUT, max_stdout=MAX_EXPORT,
            ))
        if result.timed_out:
            raise ConversationFailure("query_timeout")
        if result.overflow:
            raise ConversationFailure("history_limit")
        if result.exit_code != 0:
            if result.stderr.strip() == f"Thread https://ampcode.com/threads/{session_id} does not exist.".encode():
                return None
            raise ConversationFailure("history_unreadable")
        if len(result.stdout) > MAX_EXPORT:
            raise ConversationFailure("history_limit")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or payload.get("id") != session_id:
            raise ConversationFailure("history_unreadable")
        messages = payload.get("messages")
        if not isinstance(messages, list) or len(messages) > MAX_EXPORT // 32:
            raise ConversationFailure("history_limit")
        if not all(isinstance(message, dict) for message in messages):
            raise ConversationFailure("history_unreadable")
        items: list[HistoryItem] = []
        unsupported = False
        for message in messages:
            public, unknown = self._items(message, session_id)
            items.extend(public)
            unsupported |= unknown
        current = _Snapshot(items, unsupported, time.monotonic())
        with self._lock:
            # A shorter authoritative snapshot may be an edit, not just replication
            # lag. Do not silently retain a deleted tail from a previous export.
            self._cache[session_id] = current
            self._cache.move_to_end(session_id)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return current

    def _page(self, query: HistoryQuery, snapshot: _Snapshot | None) -> HistoryPage:
        if snapshot is None:
            if query.cursor is not None:
                raise ConversationFailure("cursor_invalid")
            return HistoryPage(session_id=query.session_id, status="not_found")
        offset, previous_identity = query.position
        if offset > len(snapshot.items):
            raise ConversationFailure("cursor_invalid")
        if previous_identity is not None and previous_identity != self._identity(snapshot.items[:offset]):
            raise ConversationFailure("cursor_invalid")

        page = HistoryPage(session_id=query.session_id, status="unsupported" if snapshot.unsupported else "available")
        index = offset
        size = 0
        while index < len(snapshot.items):
            item = snapshot.items[index]
            item_size = len(item.model_dump_json().encode())
            if len(page.items) >= min(query.limit, MAX_ITEMS) or size + item_size > MAX_PAGE:
                break
            page.items.append(item)
            size += item_size
            index += 1
        page.has_more = index < len(snapshot.items)
        page.next_cursor = query.cursor_at(index, self._identity(snapshot.items[:index]))
        return page

    @staticmethod
    def _identity(items: list[HistoryItem]) -> str:
        canonical = json.dumps([item.model_dump() for item in items], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _items(self, message: dict[str, Any], session_id: str) -> tuple[list[HistoryItem], bool]:
        role = message.get("role")
        message_id = message.get("protocolMessageID")
        blocks = message.get("content")
        if role not in {"user", "assistant", "system", "developer"} or not isinstance(message_id, str):
            return [], True
        if not isinstance(blocks, list):
            return [], True
        timestamp = message.get("createdAt") if isinstance(message.get("createdAt"), str) else None
        items: list[HistoryItem] = []
        unsupported = False
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                unsupported = True
                continue
            kind = block.get("type")
            if kind == "thinking":
                continue
            item_id = f"{session_id}:{message_id}:{block_index}"
            if kind == "text" and isinstance(block.get("text"), str):
                text, truncated = self._text(block["text"])
                items.append(HistoryItem(id=item_id, timestamp=timestamp, role=role, kind="message",
                                         content=ConversationContent(text=text), truncated=truncated))
            elif kind == "tool_use" and isinstance(block.get("name"), str):
                arguments, truncated = self._text(json.dumps(block.get("input", {}), separators=(",", ":")))
                call_id = block.get("id")
                if not isinstance(call_id, str):
                    unsupported = True
                    continue
                items.append(HistoryItem(id=item_id, timestamp=timestamp, role="assistant", kind="tool_call",
                                         call_id=call_id, content=ConversationContent(
                                             name=block["name"], arguments=arguments), truncated=truncated))
            elif kind == "tool_result" and isinstance(block.get("toolUseID"), str):
                run = block.get("run")
                if not isinstance(run, dict):
                    unsupported = True
                    continue
                result = run.get("result", {})
                text, truncated = self._text(json.dumps(result, separators=(",", ":")))
                items.append(HistoryItem(id=item_id, timestamp=timestamp, role="tool", kind="tool_result",
                                         call_id=block["toolUseID"], content=ConversationContent(text=text),
                                         truncated=truncated))
            else:
                unsupported = True
        return items, unsupported

    @staticmethod
    def _text(value: str) -> tuple[str, bool]:
        for key in ("AMP_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
                    "FLOWTOOL_WORKER_TOKEN", "FLOWTOOL_PUSH_TOKEN", "FLOWTOOL_MCP_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            secret = os.environ.get(key)
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = _SECRET_PATTERN.sub("[REDACTED]", value)
        value = _BEARER_PATTERN.sub(r"\1[REDACTED]", value)
        return value[:8192], len(value) > 8192
