"""Codex coding execution and read-only native rollout history."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import stat
import tempfile
import threading
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from loguru import logger

from app.contracts.catalog import CatalogModel
from app.contracts.conversation import (
    ConversationContent,
    ConversationFailure,
    HistoryItem,
    HistoryPage,
    HistoryQuery,
)
from app.domain.models import (
    CodingExecutionContext,
    CodingExecutionResult,
    SessionUnavailable,
)
from app.worker.coding_agent import CodingAgent
from app.worker.execution.credentials import (
    credential_values,
    git_credential_environment,
    inherited_agent_environment,
)
from app.worker.execution.gh import github_environment
from app.worker.execution.idle import wait_for_inactivity
from app.worker.execution.input_images import write_images
from app.worker.execution.permissions import (
    FULL_ACCESS,
    PermissionProfile,
    require_supported_permissions,
)
from app.worker.execution.process_groups import process_groups, signal_groups
from app.worker.execution.prompts import history_text
from app.worker.model_catalog import read_model_catalog

CODEX_BIN = "codex"
MAX_LINE = 256 * 1024
MAX_SCAN = 8 * 1024 * 1024
MAX_PAGE = 256 * 1024


class _OutputLimit(Exception):
    pass


def _codex_environment(
    source: Mapping[str, str] | None = None,
    context: CodingExecutionContext | None = None,
) -> dict[str, str]:
    """Inherit native CLI/MCP configuration, then inject only current task capabilities."""

    environment = inherited_agent_environment(os.environ if source is None else source)
    if context is not None:
        environment.update(git_credential_environment(context.github.token if context.github else None))
        if context.mcp_token is not None:
            environment["AGENTFLOW_MCP_TOKEN"] = context.mcp_token.get_secret_value()
    return environment


def _mcp_args(context: CodingExecutionContext | None) -> list[str]:
    """Replace saved flow MCP configuration with this run's capability or disable it."""
    url = context.mcp_url if context is not None else None
    token = context.mcp_token if context is not None else None
    if (url is None) != (token is None):
        raise ValueError("worker MCP requires an endpoint and scoped token together")
    if url is None:
        return ["-c", 'mcp_servers.flow={url="http://127.0.0.1", enabled=false}']
    config = (
        f'{{url={json.dumps(url)}, bearer_token_env_var="AGENTFLOW_MCP_TOKEN", '
        'enabled_tools=["open_pr","request_user_input","search_flows"], required=true, default_tools_approval_mode="auto"}'
    )
    return ["-c", f"mcp_servers.flow={config}"]


def _start_args(
    task: str,
    cwd: Path,
    *,
    trusted_host: bool,
    context: CodingExecutionContext | None = None,
) -> list[str]:
    """Build a full-access start command only after the operator's explicit opt-in."""

    if not trusted_host:
        raise RuntimeError(
            "Codex trusted-host full-access execution requires "
            "AGENTFLOW_TRUSTED_HOST_EXECUTION=true"
        )
    args = [
        CODEX_BIN,
        "exec",
        task,
        "--json",
        "-C",
        str(cwd),
        "-s",
        "danger-full-access",
        "--skip-git-repo-check",
    ]
    args.extend(_mcp_args(context))
    args.extend(["-c", 'shell_environment_policy.inherit="all"'])
    return args


def _parse_thread_id(stdout: bytearray) -> str | None:
    for line in BytesIO(stdout):
        if not line.strip():
            continue
        try:
            evt = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(evt, dict) and evt.get("type") == "thread.started":
            sid = evt.get("thread_id")
            if isinstance(sid, str) and sid.strip():
                return sid
    return None


class CodexAgent(CodingAgent):
    """Adapt Codex coding requests and native history to the worker CodingAgent contract."""

    backend = "codex"

    def __init__(self, *, trusted_host: bool = False, timeout: float = 1200.0,
                 max_output_bytes: int = 8 * 1024 * 1024, permissions: PermissionProfile = FULL_ACCESS):
        require_supported_permissions(permissions, agent="Codex")
        self.permissions = permissions
        self.trusted_host = trusted_host
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self._history = CodexHistory()

    async def discover_models(self) -> list[CatalogModel]:
        def parse(row: dict) -> CatalogModel | None:
            if row["visibility"] != "list":
                return None
            levels = row["supported_reasoning_levels"]
            if not isinstance(levels, list):
                raise TypeError
            return CatalogModel(id=row["slug"], displayName=row["display_name"],
                                efforts=[level["effort"] for level in levels])

        # Omit --bundled to use Codex's native refresh policy.
        return await read_model_catalog([CODEX_BIN, "debug", "models"], _codex_environment(), parse)

    def read_history(self, query: HistoryQuery) -> HistoryPage:
        """Read native rollout history independently of coding authorization."""
        return self._history.query(query)

    async def _run(
        self, prompt: str, cwd: Path, *, session_id: str | None = None,
        output_schema: Path | None = None, context: CodingExecutionContext | None = None,
        model: str | None = None, effort: str | None = None, max_output_bytes: int,
    ) -> CodingExecutionResult:
        """Supervise Codex's native stream and bounded final-message file."""
        if not self.trusted_host:
            raise RuntimeError(
                "Codex trusted-host full-access execution requires "
                "AGENTFLOW_TRUSTED_HOST_EXECUTION=true"
            )
        if session_id is None:
            args = _start_args(prompt, cwd, trusted_host=self.trusted_host, context=context)
        else:
            # CLI 0.153.4 accepts config overrides on resume, but not start's -C/-s flags.
            # Replace saved capabilities for every process, including recovered sessions.
            args = [
                CODEX_BIN, "exec", "resume", session_id, prompt,
                "--json", "--skip-git-repo-check",
                "-c", f"cwd={json.dumps(str(cwd))}",
                "-c", 'sandbox_mode="danger-full-access"',
                *_mcp_args(context),
                "-c", 'shell_environment_policy.inherit="all"',
            ]
        if model is not None:
            args.extend(["--model", model])
        if effort is not None:
            args.extend(["-c", f"model_reasoning_effort={json.dumps(effort)}"])
        with tempfile.TemporaryDirectory(prefix="agentflow-coding-") as directory:
            run_directory = Path(directory)
            if context:
                for path in write_images(context.images, run_directory):
                    args.extend(["--image", str(path)])
            environment = _codex_environment(context=context)
            if context is not None and context.github is not None:
                environment.update(github_environment(context.github, run_directory, environment))
            last_file = run_directory / "last-message.txt"
            args += ["-o", str(last_file)]
            if output_schema is not None:
                args += ["--output-schema", str(output_schema)]

            logger.info("codex run started: resume={} output_schema={}", session_id is not None, output_schema is not None)
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                cwd=cwd,
                start_new_session=True,
            )
            stdout, stderr = bytearray(), bytearray()
            loop = asyncio.get_running_loop()
            last_activity = loop.time()

            async def read(stream: asyncio.StreamReader | None, buffer: bytearray) -> None:
                nonlocal last_activity
                assert stream is not None
                while chunk := await stream.read(65536):
                    last_activity = loop.time()
                    available = 8 * 1024 * 1024 - len(buffer)
                    buffer.extend(chunk[:available])
                    if len(chunk) > available:
                        raise _OutputLimit

            tasks = [asyncio.create_task(read(proc.stdout, stdout)),
                     asyncio.create_task(read(proc.stderr, stderr)),
                     asyncio.create_task(proc.wait())]
            io = asyncio.gather(*tasks)

            async def cleanup() -> None:
                # While the CLI is still alive, its shell may own a separate group.
                groups = (await asyncio.to_thread(process_groups, proc.pid)
                          if proc.returncode is None else {proc.pid})
                signal_groups(groups, signal.SIGKILL)
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.gather(io, return_exceptions=True)

                async def discard(stream: asyncio.StreamReader | None) -> None:
                    assert stream is not None
                    while await stream.read(8192):
                        pass

                # As in the Amp backend, replace bounded readers with discard readers.
                # Overflow can pause a pipe, so waiting for exit alone can never finish.
                draining = asyncio.gather(proc.wait(), discard(proc.stdout), discard(proc.stderr))
                try:
                    await asyncio.wait_for(draining, timeout=5)
                except TimeoutError:
                    logger.warning("Codex cleanup exceeded its limit after termination; a descendant may hold a pipe")
                finally:
                    draining.cancel()
                    await asyncio.gather(draining, return_exceptions=True)

            try:
                await wait_for_inactivity(io, timeout=self.timeout, last_activity=lambda: last_activity)
            except (TimeoutError, _OutputLimit) as error:
                sid = session_id or _parse_thread_id(stdout)
                if isinstance(error, TimeoutError):
                    timeout_detail = f"Codex: no new stdout/stderr for {self.timeout:g}s; execution timed out"
                    logger.error(timeout_detail)
                    return CodingExecutionResult(session_id=sid, last_message="", exit_code=124,
                                                 failure_detail=timeout_detail, failure_kind="timeout")
                return CodingExecutionResult(session_id=sid, last_message="", exit_code=1,
                    failure_detail="Codex execution output exceeded byte limit", failure_kind="output_limit")
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

            sid = _parse_thread_id(stdout) or session_id
            raw = b""
            if last_file.exists():
                with last_file.open("rb") as stream:
                    # The common final check cannot bound disk reads after native execution.
                    raw = stream.read(max_output_bytes + 1)
            if len(raw) > max_output_bytes:
                return CodingExecutionResult(session_id=sid, last_message="", exit_code=1,
                    failure_detail="Codex final answer exceeded byte limit", failure_kind="output_limit")
            last_message = raw.decode()
            detail = stderr.decode(errors="replace") if proc.returncode else None
            for secret in credential_values(environment):
                last_message = last_message.replace(secret, "[REDACTED]")
                if detail:
                    detail = detail.replace(secret, "[REDACTED]")
            if proc.returncode != 0:
                logger.error("codex exit={}", proc.returncode)
                # Only a complete pre-execution rejection permits fallback. Generic errors or
                # partial output may follow edits or a push and must not replay execution.
                error_lines = stderr.decode(errors="replace").strip().splitlines()
                if (session_id and not stdout.strip() and not last_message.strip() and error_lines
                        and error_lines[-1] == "Error: thread/resume: thread/resume failed: "
                        f"no rollout found for thread id {session_id} (code -32600)"):
                    raise SessionUnavailable("Codex rejected the saved session before execution")
            return CodingExecutionResult(
                session_id=sid, last_message=last_message, exit_code=proc.returncode or 0,
                failure_detail=detail[-4000:] if detail else None, model=model,
            )


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
            for secret in credential_values(os.environ):
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,})",
                           "[REDACTED]", value)
            value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+", r"\1[REDACTED]", value)
            preview, clipped = history_text(value, user_message=kind == "message" and payload.get("role") == "user")
            truncated |= clipped
            return preview

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
