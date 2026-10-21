"""Invocation-owned Amp settings, environment and bounded local process supervision."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from app.domain.models import CodingExecutionContext
from app.worker.execution.credentials import git_credential_environment

AMP_BIN = "amp"
TERMINATION_GRACE = 3.0
AMP_TOOLS = frozenset({
    "apply_patch", "create_file", "edit_file", "Read", "shell_command",
    "shell_command_kill", "shell_command_status", "read_web_page", "web_search", "view_media",
})
_ENVIRONMENT_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR",
    "AMP_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
    "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
})
_SECRET_PATTERN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sgamp_[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{16,})"
)
_BEARER_PATTERN = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+")


def redact_amp_text(value: str, secrets: Iterable[str] = ()) -> str:
    """Redact current credentials and tokens introduced by other clients before retaining output."""
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = _SECRET_PATTERN.sub("[REDACTED]", value)
    return _BEARER_PATTERN.sub(r"\1[REDACTED]", value)


def amp_environment(
    source: Mapping[str, str] | None = None,
    context: CodingExecutionContext | None = None,
) -> dict[str, str]:
    """Keep native login/HOME, never inherit control or ambient GitHub credentials."""
    values = os.environ if source is None else source
    environment = {key: value for key, value in values.items() if key in _ENVIRONMENT_KEYS}
    environment["AMP_SKIP_UPDATE_CHECK"] = "1"
    if context is not None:
        environment.update(git_credential_environment(context.github.token if context.github else None))
        if context.mcp_token is not None:
            environment["FLOWTOOL_MCP_TOKEN"] = context.mcp_token.get_secret_value()
    return environment


def write_amp_settings(directory: Path, *, mcp_url: str | None = None) -> Path:
    """Write temporary settings with this round's broker only; these are not a sandbox.

    Keep the native tool policy identical at creation and resume: Amp threads can
    retain their creation-time tool set. The shell executor also requires its
    internal async alias (calibrated against the pinned CLI).
    """
    tools = sorted(AMP_TOOLS | ({"mcp__flow__open_pr"} if mcp_url else set()))
    rules = ([{"matches": {"url": mcp_url}, "action": "allow"}] if mcp_url else [])
    rules.extend([
        {"matches": {"command": "*"}, "action": "reject"},
        {"matches": {"url": "*"}, "action": "reject"},
    ])
    settings = {
        "amp.updates.mode": "disabled",
        "amp.notifications.enabled": False,
        "amp.remoteThreadCreation.enabled": False,
        "amp.runner.env.enabled": False,
        "amp.skills.disableClaudeCodeSkills": True,
        "amp.skills.disableGlobalAgentsSkills": True,
        "amp.tools.enable": tools + ["async_shell_command"],
        "amp.tools.disable": [
            "ask_user_choice", "create_thread", "find_thread", "finder", "get_thread_status",
            "librarian", "list_agent_modes", "oracle", "painter", "read_mcp_resource", "read_thread",
            "send_thread_message", "skill", "Task", "wait_for_threads", "tool_search", "code_exec",
            "load_plugin", "reload_mcp", "reload_plugins", "reload_skills",
            "download_thread_changes", "download_thread_file", "upload_thread_file",
        ],
        "amp.mcpPermissions": rules,
        "amp.mcpServers": {"flow": ({
            "url": mcp_url,
            "headers": {"Authorization": "Bearer ${FLOWTOOL_MCP_TOKEN}"},
        } if mcp_url else {"url": "http://127.0.0.1:1/mcp", "disabled": True})},
    }
    path = directory / "amp-settings.json"
    with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
        json.dump(settings, handle)
    return path


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


async def run_amp(
    args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
    input_text: str | None = None, timeout: float = 1200,
    max_stdout: int = 8 * 1024 * 1024,
) -> AmpProcessResult:
    """Run Amp with bounded concurrent pipes and TERM-first cancellation/timeout cleanup.

    ``args`` excludes the executable. Prompts travel on stdin, never argv. Both
    streams have the byte bound; an overflow fails the invocation, never truncates
    it into an apparent success. The same supervisor serves read-only exports.
    """
    proc = await asyncio.create_subprocess_exec(
        AMP_BIN, *args, cwd=cwd, env=env if env is not None else amp_environment(),
        stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    stdout, stderr = bytearray(), bytearray()

    async def read(stream: asyncio.StreamReader | None, buffer: bytearray) -> None:
        assert stream is not None
        while chunk := await stream.read(65536):
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
    try:
        await asyncio.wait_for(asyncio.shield(io), timeout=timeout)
    except TimeoutError:
        timed_out = True
    except _OutputLimit:
        overflow = True
    finally:
        cleanup = asyncio.create_task(_stop_amp(proc))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(io, return_exceptions=True)
    return AmpProcessResult(bytes(stdout), bytes(stderr), proc.returncode or 0, timed_out, overflow)
