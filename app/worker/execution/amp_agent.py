"""Worker CodingAgent adapter for locally supervised Amp threads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from loguru import logger
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError, field_validator

from app.contracts.catalog import CatalogModel
from app.contracts.conversation import (
    ConversationContent,
    ConversationFailure,
    HistoryItem,
    HistoryPage,
    HistoryQuery,
    SessionId,
)
from app.contracts.files import MAX_FILE_BYTES, FileFailure
from app.domain.models import CodingExecutionContext, CodingExecutionResult
from app.worker.coding_agent import CodingAgent
from app.worker.execution.credentials import git_credential_environment, inherited_agent_environment
from app.worker.execution.gh import github_environment
from app.worker.execution.git_identity import disables_amp_coauthor
from app.worker.execution.idle import wait_for_inactivity
from app.worker.execution.input_images import write_images
from app.worker.model_catalog import read_model_catalog
from app.worker.state.file_links import FileLinks

_SESSION_ID = TypeAdapter(SessionId)
AMP_BIN = "amp"
TERMINATION_GRACE = 3.0
MAX_EXPORT = 2 * 1024 * 1024
EXPORT_TIMEOUT = 20
CACHE_SIZE = 16
CACHE_SECONDS = 2
MAX_PAGE = 256 * 1024
_SECRET_PATTERN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sgamp_[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{16,})"
)
_BEARER_PATTERN = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+")
# Escaped quotes cannot open a JSON literal; skip them when an unterminated string fails.
_JSON_STRING = re.compile(
    r'(?<!\\)"[^"\\\x00-\x1f]*(?:\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})[^"\\\x00-\x1f]*)*"'
)


def redact_amp_text(value: str, secrets: Iterable[str] = ()) -> str:
    """Redact plaintext and escaped JSON strings without parsing entire documents."""
    secrets = tuple(secret for secret in secrets if secret)

    def literal(match: re.Match[str]) -> str:
        raw = match.group()
        if "\\" not in raw:
            return raw
        decoded: str = json.loads(raw)
        redacted = redact_amp_text(decoded, secrets)
        return raw if redacted == decoded else json.dumps(redacted)

    # Decode string boundaries before plaintext rules, including escaped Bearer slashes.
    # Only replaced literals are rewritten; non-sensitive formatting stays unchanged.
    value = _JSON_STRING.sub(literal, value)
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    value = _SECRET_PATTERN.sub("[REDACTED]", value)
    return _BEARER_PATTERN.sub(r"\1[REDACTED]", value)


def redact_amp_json(value: JsonValue, secrets: Iterable[str] = ()) -> str:
    """Redact parsed tool payloads and serialize once for public history."""
    secrets = tuple(secret for secret in secrets if secret)

    def walk(item: JsonValue) -> JsonValue:
        if isinstance(item, str):
            return redact_amp_text(item, secrets)
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, dict):
            return {redact_amp_text(key, secrets): walk(child) for key, child in item.items()}
        return item

    return json.dumps(walk(value), separators=(",", ":"))


def amp_environment(
    source: Mapping[str, str] | None = None,
    context: CodingExecutionContext | None = None,
) -> dict[str, str]:
    """Inherit native CLI/MCP configuration, then inject only current task capabilities."""
    environment = inherited_agent_environment(os.environ if source is None else source)
    environment["AMP_SKIP_UPDATE_CHECK"] = "1"
    if context is not None:
        environment.update(git_credential_environment(context.github.token if context.github else None))
        if context.mcp_token is not None:
            environment["AGENTFLOW_MCP_TOKEN"] = context.mcp_token.get_secret_value()
    return environment


@dataclass(frozen=True)
class AmpProcessResult:
    stdout: bytes
    stderr: bytes
    exit_code: int
    timed_out: bool = False
    overflow: bool = False


class _OutputLimit(Exception):
    pass


def _descendant_groups(pid: int) -> set[int]:
    """Snapshot before signalling: orphaned shells otherwise lose their parent identity."""
    try:
        snapshot = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True,
            check=True, timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("Amp descendant snapshot unavailable; cleanup may leave detached processes")
        return set()
    rows = [tuple(map(int, row.split())) for row in snapshot.stdout.splitlines()]
    descendants = {pid}
    while True:
        children = {child for child, parent, _ in rows if parent in descendants}
        if children <= descendants:
            break
        descendants.update(children)
    return {group for child, _, group in rows if child in descendants
            and group > 1 and group != os.getpgrp()}


def _signal_groups(groups: set[int], sig: signal.Signals) -> None:
    for group in groups:
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            pass


async def _stop_amp(proc: asyncio.subprocess.Process) -> None:
    """Bound cleanup before propagating cancellation; arbitrary daemons need external isolation."""
    groups = await asyncio.to_thread(_descendant_groups, proc.pid)
    groups.add(proc.pid)
    _signal_groups({proc.pid}, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=TERMINATION_GRACE)
    except TimeoutError:
        logger.warning("Amp forced termination; detached-process cleanup is best effort")
    finally:
        # Include separate native shell PGIDs captured while the CLI still owned them.
        _signal_groups(groups, signal.SIGTERM)
        _signal_groups(groups, signal.SIGKILL)
        await asyncio.wait_for(proc.wait(), timeout=5)


def _outcome(process: AmpProcessResult, expected: str | None, timeout: float) -> CodingExecutionResult:
    """Only an intact matching init/final pair can carry a successful final answer."""
    session_id = expected
    timeout_detail = f"Amp: no new stdout/stderr for {timeout:g}s; execution timed out"
    try:
        init: dict[str, Any] | None = None
        final: dict[str, Any] | None = None
        for line in process.stdout.split(b"\n"):
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or final is not None:
                raise ValueError("invalid Amp event order")
            kind = event.get("type")
            if not isinstance(kind, str):
                raise TypeError("invalid Amp event type")
            if init is None and (kind != "system" or event.get("subtype") != "init"):
                raise ValueError("Amp stream must begin with initialization")
            if kind == "system" and event.get("subtype") == "init":
                if init is not None:
                    raise ValueError("duplicate Amp initialization")
                thread_id = _SESSION_ID.validate_python(event.get("session_id"), strict=True)
                tools = event.get("tools")
                if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
                    raise ValueError("invalid Amp initialization fields")
                init = event
                if not thread_id.startswith("T-") or (expected and thread_id != expected):
                    raise ValueError("Amp returned a different thread")
                session_id = thread_id
                if "shell_command" not in tools:
                    raise ValueError("Amp thread has no native shell; cannot execute coding tasks")
            elif kind == "result":
                _SESSION_ID.validate_python(event.get("session_id"), strict=True)
                if (not isinstance(event.get("subtype"), str)
                        or type(event.get("is_error")) is not bool
                        or (event.get("result") is not None and not isinstance(event["result"], str))
                        or not isinstance(event.get("error", ""), str)):
                    raise ValueError("invalid Amp result fields")
                final = event
            elif kind not in {"system", "user", "assistant"}:
                raise ValueError("unknown Amp stream event")
        if init is None or final is None or final["session_id"] != session_id:
            raise ValueError("incomplete or mismatched Amp result")
        if process.timed_out:
            raise ValueError(timeout_detail)
        if process.overflow:
            raise ValueError("Amp execution output exceeded byte limit")
        if process.exit_code or final["is_error"] or final["subtype"] != "success":
            return CodingExecutionResult(
                session_id=session_id, last_message="", exit_code=process.exit_code or 1,
                failure_detail=final.get("error") or "Amp execution failed",
            )
        answer = final.get("result")
        if not isinstance(answer, str):
            raise TypeError("Amp success has no final answer")
        return CodingExecutionResult(session_id=session_id, last_message=answer, exit_code=0)
    except (ValueError, TypeError, ValidationError, UnicodeDecodeError) as error:
        # Do not expose validation inputs or raw JSON: they may contain thinking or credentials.
        detail = (timeout_detail if process.timed_out else
                  "Amp execution output exceeded byte limit" if process.overflow else
                  str(error) if type(error) is ValueError else "invalid Amp event stream")
        if init is None and not process.timed_out and process.stderr.strip():
            detail += ": " + process.stderr.decode(errors="replace").strip()
        return CodingExecutionResult(
            session_id=session_id, last_message="", exit_code=124 if process.timed_out else process.exit_code or 1,
            failure_detail=detail,
            failure_kind="timeout" if process.timed_out else "output_limit" if process.overflow else None,
        )


class AmpCodingAgent(CodingAgent):
    """Adapt coding requests to Amp threads; executor retains schema and VCS authority."""

    backend = "amp"

    def __init__(self, *, trusted_host: bool = False, timeout: float = 1200.0,
                 max_output_bytes: int = 8 * 1024 * 1024):
        self.trusted_host = trusted_host
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self._history = AmpHistory()

    async def discover_models(self) -> list[CatalogModel]:
        def parse(row: dict) -> CatalogModel | None:
            caps = row["capabilities"]
            if not isinstance(caps["tools"], bool):
                raise TypeError
            if not caps["tools"]:
                return None
            return CatalogModel(id=row["id"], displayName=row["displayName"], efforts=caps["efforts"])

        command = [AMP_BIN, "plugins", "show-agent-options", "--json", "--log-file", os.devnull]
        return await read_model_catalog(command, amp_environment(), parse)

    def read_history(self, query: HistoryQuery) -> HistoryPage:
        """Read a native thread without granting permission to execute coding tasks."""
        return self._history.query(query)

    def register_file(self, source: Path | str, files: FileLinks) -> str:
        """Register native attachments lazily; local paths use the common identity checks."""
        if isinstance(source, Path):
            return super().register_file(source, files)
        try:
            attachment = AmpAttachment(url=source)
            return files.register(self.backend, attachment.model_dump(mode="json"))
        except ValidationError:
            raise FileFailure("file_unreadable") from None

    async def read_file(self, source: dict[str, JsonValue], *, root: Path) -> bytes:
        """Read a native attachment with this backend's sign-in, or use the local default."""
        if "url" not in source:
            return await super().read_file(source, root=root)
        try:
            attachment = AmpAttachment.model_validate(source)
        except ValidationError:
            raise FileFailure("file_unreadable") from None
        return await attachment.read(root)

    @staticmethod
    async def run_process(
        args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        input_text: str | None = None, timeout: float = 1200,
        max_stdout: int = 8 * 1024 * 1024,
        timeout_kind: Literal["total", "idle"] = "total",
        max_file_bytes: int | None = None,
    ) -> AmpProcessResult:
        """Supervise coding, read-only exports and downloads with bounded pipes and cleanup.

        Prompts travel on stdin, never argv. Coding selects idle time since the
        last nonempty stdout/stderr read; read-only calls retain total deadlines.
        Downloads impose a kernel file-size limit before exec, without an unsafe
        preexec_fn in this multithreaded worker. Coding authorization stays in _run.
        """
        command = [AMP_BIN, *args]
        if max_file_bytes is not None:
            command = [
                sys.executable, "-c",
                ("import os,resource,sys; n=int(sys.argv[1]); "
                "resource.setrlimit(resource.RLIMIT_FSIZE,(n,n)); "
                "os.execvpe(sys.argv[2],sys.argv[2:],os.environ)"),
                str(max_file_bytes), *command,
            ]
        proc = await asyncio.create_subprocess_exec(
            *command, cwd=cwd, env=env if env is not None else amp_environment(),
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        stdout, stderr = bytearray(), bytearray()
        loop = asyncio.get_running_loop()
        last_activity = loop.time()

        async def read(stream: asyncio.StreamReader | None, buffer: bytearray) -> None:
            nonlocal last_activity
            assert stream is not None
            while chunk := await stream.read(65536):
                last_activity = loop.time()
                available = max_stdout - len(buffer)
                buffer.extend(chunk[:available])
                if len(chunk) > available:
                    raise _OutputLimit

        async def feed() -> None:
            if proc.stdin is not None:
                try:
                    proc.stdin.write((input_text or "").encode())
                    await proc.stdin.drain()
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Early CLI rejections can close stdin before consuming the prompt.

        tasks = [asyncio.create_task(read(proc.stdout, stdout)),
                 asyncio.create_task(read(proc.stderr, stderr)),
                 asyncio.create_task(feed()), asyncio.create_task(proc.wait())]
        io = asyncio.gather(*tasks)
        timed_out = overflow = False

        async def cleanup() -> None:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(io, return_exceptions=True)

            async def discard(stream: asyncio.StreamReader | None) -> None:
                assert stream is not None
                while await stream.read(8192):
                    pass

            # Like CodexAgent.cleanup, drain after overflow without retaining any more bytes.
            draining = asyncio.gather(discard(proc.stdout), discard(proc.stderr))
            try:
                await _stop_amp(proc)
                try:
                    await asyncio.wait_for(draining, timeout=5)
                except TimeoutError:
                    logger.warning("Amp cleanup exceeded its pipe-drain limit after termination")
            finally:
                draining.cancel()
                await asyncio.gather(draining, return_exceptions=True)

        try:
            if timeout_kind == "idle":
                await wait_for_inactivity(io, timeout=timeout, last_activity=lambda: last_activity)
            else:
                await asyncio.wait_for(asyncio.shield(io), timeout=timeout)
        except TimeoutError:
            timed_out = True
        except _OutputLimit:
            overflow = True
        finally:
            stopping = asyncio.create_task(cleanup())
            cancelled = False
            while not stopping.done():
                try:
                    await asyncio.shield(stopping)
                except asyncio.CancelledError:
                    cancelled = True
            stopping.result()
            if cancelled:
                raise asyncio.CancelledError
        return AmpProcessResult(bytes(stdout), bytes(stderr), proc.returncode or 0, timed_out, overflow)

    async def _run(
        self, prompt: str, cwd: Path, *, session_id: str | None = None,
        output_schema: Path | None = None, context: CodingExecutionContext | None = None,
        model: str | None = None, effort: str | None = None, max_output_bytes: int,
    ) -> CodingExecutionResult:
        """Adapt a native thread while the common lifecycle owns final-answer validation."""
        if session_id is not None:
            # Never select a recent thread or silently migrate another backend's session.
            try:
                thread_id = _SESSION_ID.validate_python(session_id, strict=True)
            except ValidationError:
                thread_id = ""
            if not thread_id.startswith("T-"):
                raise ValueError("resume requires an explicit Amp thread ID")
        if not self.trusted_host:
            raise RuntimeError("Amp trusted-host execution requires AGENTFLOW_TRUSTED_HOST_EXECUTION=true")
        url = context.mcp_url if context else None
        token = context.mcp_token if context else None
        if (url is None) != (token is None):
            raise ValueError("worker MCP requires an endpoint and scoped token together")
        # Port zero replaces any saved/user flow endpoint without exposing an old
        # broker or its token; it preserves the existing no-broker behavior.
        mcp = {"flow": ({"url": url, "headers": {"Authorization": "Bearer ${AGENTFLOW_MCP_TOKEN}"}}
                        if url is not None else {"url": "http://127.0.0.1:0/mcp"})}
        if output_schema is not None:
            prompt += ("\n\nReturn exactly one JSON value matching this JSON Schema as your final answer. "
                       "Do not wrap it in Markdown or add explanatory text.\n" + output_schema.read_text())
        with tempfile.TemporaryDirectory(prefix="agentflow-amp-") as directory:
            run_directory = Path(directory)
            if context and context.images:
                paths = write_images(context.images, run_directory)
                prompt += "\n\nView the attached task images with view_media:\n" + "\n".join(map(str, paths))
            environment = amp_environment(context=context)
            if context and context.github:
                environment.update(github_environment(context.github, run_directory, environment))
                if await disables_amp_coauthor(cwd):
                    environment["AMP_DISABLE_AMP_COAUTHOR_TRAILER"] = "1"
            args = (["threads", "continue", session_id] if session_id else []) + [
                "--mcp-config", json.dumps(mcp), "--executor", "local",
                "--visibility", "private", "--no-ide", "--no-notifications",
                "--execute", "--stream-json", "--no-archive-after-execute", "--log-file", os.devnull,
            ]
            # Native continuation owns its model; only new threads use current settings.
            if session_id is None:
                args.extend(["--model", model] if model is not None else ["--mode", "low"])
                if effort is not None:
                    args.extend(["--effort", effort])
            process = await AmpCodingAgent.run_process(args, cwd=cwd, env=environment, input_text=prompt,
                                                       timeout=self.timeout, timeout_kind="idle")
            result = _outcome(process, session_id, self.timeout)
            # No SessionUnavailable fallback: the observed CLI stderr alone has not
            # proven that every missing-thread rejection precedes all execution.
            secrets = [value for key, value in environment.items()
                       if key in {"AMP_API_KEY", "AGENTFLOW_MCP_TOKEN", "AGENTFLOW_PUSH_TOKEN", "GH_TOKEN"} and value]
            result.last_message = redact_amp_text(result.last_message, secrets)
            if result.failure_detail:
                result.failure_detail = redact_amp_text(result.failure_detail, secrets)[-4000:]
            return result


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
            with tempfile.TemporaryDirectory(prefix=".amp-attachment-", dir=worker_root) as directory:
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
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
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
        result = asyncio.run(AmpCodingAgent.run_process(
            ["threads", "export", session_id, "--log-file", os.devnull],
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
            if len(page.items) >= query.limit or size + item_size > MAX_PAGE:
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
        # Amp delivers automated events as user messages, but they are not human input.
        meta = message.get("meta")
        automated = role == "user" and isinstance(meta, dict) and meta.get("fromAutomation") is True
        timestamp = message.get("createdAt") if isinstance(message.get("createdAt"), str) else None
        items: list[HistoryItem] = []
        unsupported = False
        secrets = tuple(os.environ.get(key, "") for key in (
            "AMP_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
            "AGENTFLOW_WORKER_TOKEN", "AGENTFLOW_PUSH_TOKEN", "AGENTFLOW_MCP_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
        ))
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                unsupported = True
                continue
            kind = block.get("type")
            if kind == "thinking":
                continue
            item_id = f"{session_id}:{message_id}:{block_index}"
            if kind == "text" and isinstance(block.get("text"), str):
                text, truncated = self._text(redact_amp_text(block["text"], secrets))
                items.append(HistoryItem(id=item_id, timestamp=timestamp,
                                         role="system" if automated else role, kind="message",
                                         content=ConversationContent(text=text), truncated=truncated))
            elif kind == "tool_use" and isinstance(block.get("name"), str):
                arguments, truncated = self._text(redact_amp_json(block.get("input", {}), secrets))
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
                text, truncated = self._text(redact_amp_json(result, secrets))
                items.append(HistoryItem(id=item_id, timestamp=timestamp, role="tool", kind="tool_result",
                                         call_id=block["toolUseID"], content=ConversationContent(text=text),
                                         truncated=truncated))
            else:
                unsupported = True
        return items, unsupported

    @staticmethod
    def _text(value: str) -> tuple[str, bool]:
        return value[:8192], len(value) > 8192
