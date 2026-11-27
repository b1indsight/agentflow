"""Worker-owned, append-only public Amp transcript and atomic native snapshot repair."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import ijson
from ijson.backends import python as json_backend
from loguru import logger

from app.contracts.conversation import (
    AMP_THREAD_PATTERN,
    ConversationFailure,
    HistoryItem,
    HistoryPage,
    HistoryQuery,
)
from app.worker.state.files import write_private_record

MAX_PAGE = 256 * 1024
MAX_RECOVERY = 64 * 1024 * 1024
Project = Callable[[dict[str, Any], str, tuple[str, ...]], tuple[list[HistoryItem], bool]]
Export = Callable[[str, Callable[[bytes], None]], bool]


def record_key(item: HistoryItem) -> str:
    """Tool identity is shared across formats; only visible text participates in text alignment."""
    if item.call_id is None:
        return f"{item.role}:{item.content.text}"
    call = item.call_id
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,38}", call):
        # Keep unusual call IDs inside the wire ID length/alphabet without truncating them.
        call = "~" + uuid.uuid5(uuid.NAMESPACE_OID, call).hex
    return ("c-" if item.kind == "tool_call" else "r-") + call


class RecordIds:
    """Allocate new IDs without renumbering text already published in this segment."""

    def __init__(self, sid: str, anchors: list[tuple[str, str]], *, resume: bool) -> None:
        self.sid = sid
        self.used = {published for _, published in anchors}
        self.anchor = "start"
        if resume:
            self.anchor = next((k for k, _ in reversed(anchors) if k.startswith(("c-", "r-"))), "start")
        self.offset = 0

    def assign(self, item: HistoryItem, key: str, published: str | None = None) -> None:
        if item.call_id is not None:
            self.anchor, self.offset = key, 0
            new = f"{self.sid}:{key}"
        else:
            while (new := f"{self.sid}:{self.anchor}/{self.offset}") in self.used:
                self.offset += 1
        if published is None and new in self.used:
            raise ConversationFailure("history_unreadable")
        item.id = published or new
        self.used.add(item.id)


@dataclass
class Checkpoint:
    generation: str = field(default_factory=lambda: uuid.uuid4().hex)
    size: int = 0
    modified: int = 0
    complete: bool = False
    unsupported: bool = False
    # Ordered evidence of turns, independent of whether the JSONL survives.
    anchors: list[tuple[str, str]] = field(default_factory=list)
    # A later export cannot redact an expired assignment secret. Keep its public replacement.
    protected: dict[str, str] = field(default_factory=dict)

    def remember(self, key: str, item: HistoryItem) -> None:
        self.anchors.append((key, item.id))
        if item.truncated or "[REDACTED]" in item.content.text or "[REDACTED]" in (item.content.arguments or ""):
            self.protected[item.id] = item.model_dump_json()

    def matches(self, path: Path) -> bool:
        try:
            info = path.stat()
        except FileNotFoundError:
            return False
        return (info.st_size, info.st_mtime_ns) == (self.size, self.modified)

    def stamp(self, path: Path) -> None:
        info = path.stat()
        self.size, self.modified = info.st_size, info.st_mtime_ns


def align_text(known: list[tuple[str, str]], segment: list[tuple[str, HistoryItem]]) -> dict[int, str]:
    """A subsequence is unique iff its leftmost and rightmost placements are identical."""
    def locate(indices: range, reverse: bool) -> dict[int, str]:
        matches = {}
        cursor = iter(indices)
        for key, published in reversed(known) if reverse else known:
            for i in cursor:
                if key in (segment[i][0], segment[i][1].id):
                    matches[i] = published
                    break
            else:
                raise ConversationFailure("history_unreadable")
        return matches

    first = locate(range(len(segment)), False)
    if first != locate(range(len(segment) - 1, -1, -1), True):
        raise ConversationFailure("history_unreadable")
    return first


class AmpTranscript:
    """One store per locked worker root. Only known gaps trigger a native export."""

    def __init__(self, root: Path, project: Project, export: Export) -> None:
        self.root, self.project, self.export = root, project, export
        self.lock = threading.RLock()
        self.active: dict[str, Checkpoint] = {}
        self.repairing: set[str] = set()

    def path(self, session: str) -> Path:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / f"{session}.jsonl"
        if self.root.is_symlink() or path.is_symlink() or path.with_suffix(".json").is_symlink():
            raise ConversationFailure("history_unreadable")
        return path

    @staticmethod
    def load(path: Path) -> Checkpoint:
        try:
            state = Checkpoint(**json.loads(path.with_suffix(".json").read_bytes()))
        except FileNotFoundError:
            state = Checkpoint()
        if not state.complete and path.exists():
            # A killed process may have exposed whole records beyond its last checkpoint.
            with path.open("rb") as data:
                data.seek(state.size)
                while row := data.readline(MAX_PAGE + 1):
                    if not row.endswith(b"\n"):
                        break
                    try:
                        item = HistoryItem.model_validate_json(row)
                    except ValueError:
                        break
                    state.remember(record_key(item), item)
                state.size = data.tell()
        return state

    @staticmethod
    def save(path: Path, state: Checkpoint) -> None:
        write_private_record(path.with_suffix(".json"), json.dumps(asdict(state)))

    def query(self, query: HistoryQuery) -> HistoryPage:
        sid = query.session_id
        path = self.path(sid)
        with self.lock:
            state = self.active.get(sid) or self.load(path)
            if sid in self.active and not state.complete:
                raise ConversationFailure("query_busy")
            if sid in self.active or (state.complete and state.matches(path)):
                return self.page(query, path, state)
            if sid in self.repairing:
                raise ConversationFailure("query_busy")
            self.repairing.add(sid)
            prior = path.with_suffix(".json")
            stamp = prior.read_bytes() if prior.exists() else None
        try:
            with (
                tempfile.TemporaryDirectory(dir=self.root) as directory,
                tempfile.NamedTemporaryFile(dir=directory, delete=False) as candidate,
            ):
                replacement = Checkpoint(complete=True)
                ids = RecordIds(sid, state.anchors, resume=False)
                known_tools = {k for k, _ in state.anchors if k.startswith(("c-", "r-"))}
                seen_tools: set[str] = set()
                position = byte_count = 0
                segment: list[tuple[str, HistoryItem]] = []

                def save_item(key: str, item: HistoryItem, published: str | None) -> None:
                    native = item.id
                    # Export-only legacy sessions may already have native IDs in Control.
                    ids.assign(item, key, published or (native if not state.anchors else None))
                    if protected := state.protected.get(item.id):
                        item = HistoryItem.model_validate_json(protected)
                    replacement.remember(native if item.call_id is None else key, item)
                    candidate.write(item.model_dump_json().encode() + b"\n")
                    if candidate.tell() > MAX_RECOVERY:
                        raise ConversationFailure("history_limit")

                def flush(boundary: str | None) -> None:
                    """Between common tool anchors, earliest and latest ordered matches must agree."""
                    nonlocal position
                    end = position
                    while end < len(state.anchors) and state.anchors[end][0] not in known_tools:
                        end += 1
                    expected = state.anchors[end][0] if end < len(state.anchors) else None
                    if boundary != expected:
                        raise ConversationFailure("history_unreadable")
                    first = align_text(state.anchors[position:end], segment)
                    for i, (key, item) in enumerate(segment):
                        save_item(key, item, first.get(i))
                    segment.clear()
                    position = end

                def message(row: dict) -> None:
                    nonlocal position, byte_count
                    public, unknown = self.project(row, sid, ())
                    replacement.unsupported |= unknown
                    for item in public:
                        key = record_key(item)
                        byte_count += len(item.model_dump_json().encode())
                        if byte_count > MAX_RECOVERY:
                            raise ConversationFailure("history_limit")
                        if item.call_id is not None:
                            if key in seen_tools:
                                raise ConversationFailure("history_unreadable")
                            seen_tools.add(key)
                        if key in known_tools:
                            flush(key)
                            save_item(key, item, state.anchors[position][1])
                            position += 1
                        elif position == len(state.anchors):
                            save_item(key, item, None)
                        else:
                            segment.append((key, item))

                if not read_export(sid, self.export, message):
                    if stamp is not None or path.exists():
                        raise ConversationFailure("history_unreadable")
                    if query.cursor:
                        raise ConversationFailure("cursor_invalid")
                    return HistoryPage(session_id=sid, status="not_found")
                flush(None)
                candidate.flush()
                os.fsync(candidate.fileno())
                candidate.close()
                with self.lock:
                    if (
                        sid in self.active
                        or (prior.read_bytes() if prior.exists() else None) != stamp
                    ):
                        raise ConversationFailure("query_busy")
                    os.replace(candidate.name, path)
                    replacement.stamp(path)
                    self.save(path, replacement)
                    return self.page(query, path, replacement)
        finally:
            with self.lock:
                self.repairing.remove(sid)

    @staticmethod
    def page(query: HistoryQuery, path: Path, state: Checkpoint) -> HistoryPage:
        offset, generation = query.position
        if generation is not None and (generation != state.generation or offset > state.size):
            raise ConversationFailure("cursor_invalid")
        page = HistoryPage(
            session_id=query.session_id, status="unsupported" if state.unsupported else "available"
        )
        with path.open("rb") as data:
            if offset:
                data.seek(offset - 1)
                if data.read(1) != b"\n":
                    raise ConversationFailure("cursor_invalid")
            size = 0
            while offset < state.size and len(page.items) < query.limit:
                row = data.readline(MAX_PAGE + 1)
                if not row.endswith(b"\n") or len(row) > MAX_PAGE:
                    raise ConversationFailure("history_unreadable")
                if size + len(row) > MAX_PAGE:
                    break
                page.items.append(HistoryItem.model_validate_json(row))
                offset += len(row)
                size += len(row)
        page.has_more = offset < state.size
        page.next_cursor = query.cursor_at(offset, state.generation)
        return page


def read_export(session: str, export: Export, message: Callable[[dict], None]) -> bool:
    """Parse one native message at a time. Raw export bytes never touch disk."""
    events = ijson.sendable_list()
    # The C backend silently rewrites escaped unpaired surrogates in tool arguments.
    parser = json_backend.parse_coro(events, use_float=True)
    found_id = found_messages = False
    builder = None

    def consume(chunk: bytes) -> None:
        nonlocal found_id, found_messages, builder
        parser.send(chunk)
        for prefix, kind, value in events:
            if prefix == "id":
                if found_id or kind != "string" or value != session:
                    raise ValueError("invalid export session")
                found_id = True
            elif prefix == "messages" and kind == "start_array":
                if found_messages:
                    raise ValueError("duplicate export messages")
                found_messages = True
            elif prefix == "messages.item" and kind == "start_map":
                builder = ijson.ObjectBuilder()
            elif prefix == "messages.item" and kind not in ("end_map", "map_key"):
                raise ValueError("invalid export message")
            if builder is not None:
                builder.event(kind, value)
                if prefix == "messages.item" and kind == "end_map":
                    message(builder.value)
                    builder = None
        events.clear()

    try:
        if not export(session, consume):
            return False
        parser.close()
        if not (found_id and found_messages):
            raise ValueError("incomplete export")
        return True
    except ijson.JSONError:
        raise ConversationFailure("history_unreadable") from None
    finally:
        with suppress(ijson.JSONError):
            parser.close()


class Capture:
    """Append complete public events while a coding process owns its thread."""

    def __init__(
        self, store: AmpTranscript, expected: str | None, prompt: str, secrets: tuple[str, ...]
    ):
        self.store, self.expected, self.prompt, self.secrets = store, expected, prompt, secrets
        self.sid = ""
        self.pending = bytearray()
        self.file: BinaryIO | None = None
        self.clean = False
        self.failed = False
        self.echo = True
        self.last = ""
        self.ids: RecordIds

    def feed(self, chunk: bytes) -> None:
        if self.failed:
            return
        # Only scan new bytes; a multi-megabyte tool event may span many pipe reads.
        try:
            parts = chunk.split(b"\n")
            for part in parts[:-1]:
                self.pending.extend(part)
                if self.pending.strip():
                    self.event(json.loads(self.pending))
                self.pending.clear()
            self.pending.extend(parts[-1])
        except (
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
            OSError,
            RecursionError,
            ConversationFailure,
        ):
            self.failed = True
            logger.warning("Amp history capture interrupted; native repair required")

    def event(self, event: dict) -> None:
        with self.store.lock:
            kind = event.get("type")
            if not self.sid:
                sid = event.get("session_id")
                if (
                    kind != "system"
                    or event.get("subtype") != "init"
                    or not isinstance(sid, str)
                    or not re.fullmatch(AMP_THREAD_PATTERN, sid)
                    or self.expected not in (None, sid)
                ):
                    raise ValueError("invalid capture init")
                path = self.store.path(sid)
                if sid in self.store.active:
                    raise ValueError("duplicate active thread")
                state = self.store.load(path)
                self.clean = (
                    (state.complete and state.matches(path)) if self.expected else not path.exists()
                )
                state.complete = False
                self.store.save(path, state)
                state.complete = self.clean
                fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                self.file = os.fdopen(fd, "ab", buffering=0)
                self.sid = sid
                self.store.active[sid] = state
                self.ids = RecordIds(sid, state.anchors, resume=True)
                self.write("user", [{"type": "text", "text": self.prompt}])
            elif kind in ("user", "assistant"):
                message = event["message"]
                blocks = message["content"]
                if isinstance(blocks, str):
                    blocks = [{"type": "text", "text": blocks}]
                for block in blocks:
                    if (
                        kind == "user"
                        and self.echo
                        and block.get("type") == "text"
                        and block.get("text") == self.prompt
                    ):
                        self.echo = False
                        continue
                    self.echo = False
                    if block.get("type") == "tool_result":
                        content = block.get("content")
                        # The CLI serializes native run.result as JSON inside the stream block.
                        if isinstance(content, str):
                            with suppress(ValueError):
                                content = json.loads(content)
                        block = {
                            "type": "tool_result",
                            "toolUseID": block.get("tool_use_id"),
                            "run": {"result": content},
                        }
                    self.write(kind, [block])
                    if kind == "assistant" and block.get("type") == "text":
                        self.last = block["text"]
            elif kind == "result" and isinstance(answer := event.get("result"), str):
                if answer != self.last:
                    self.write("assistant", [{"type": "text", "text": answer}])

    def write(self, role: str, blocks: list) -> None:
        assert self.file is not None
        state = self.store.active[self.sid]
        items, unknown = self.store.project(
            {
                "role": role,
                "createdAt": datetime.now(UTC).isoformat(),
                "protocolMessageID": "stream",  # projection only; stream events have no native message ID
                "content": blocks,
            },
            self.sid,
            self.secrets,
        )
        state.unsupported |= unknown
        for item in items:
            key = record_key(item)
            self.ids.assign(item, key)
            state.remember(key, item)
            self.file.write(item.model_dump_json().encode() + b"\n")
        state.size = self.file.tell()

    def close(self, success: bool) -> None:
        with self.store.lock:
            if self.file is not None:
                self.file.close()
                path = self.store.path(self.sid)
                state = self.store.active.pop(self.sid)
                try:
                    state.stamp(path)
                    state.complete = self.clean and success and not self.failed
                    self.store.save(path, state)
                except OSError:
                    # The begin checkpoint is still dirty; don't turn an otherwise valid job into a failure.
                    logger.warning("Amp history checkpoint unavailable; native repair required")
