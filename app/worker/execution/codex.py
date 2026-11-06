"""CodingAgent adapter for Codex CLI in a trusted Flow-owned repository."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from collections.abc import Mapping
from pathlib import Path

from loguru import logger

from app.domain.models import (
    CodingExecutionContext,
    CodingExecutionResult,
    SessionUnavailable,
)
from app.worker.execution.credentials import git_credential_environment, inherited_agent_environment
from app.worker.execution.gh import github_environment
from app.worker.execution.idle import wait_for_inactivity

CODEX_BIN = "codex"


def _codex_environment(
    source: Mapping[str, str] | None = None,
    context: CodingExecutionContext | None = None,
) -> dict[str, str]:
    """Inherit native CLI/MCP configuration, then inject only current task capabilities."""

    environment = inherited_agent_environment(os.environ if source is None else source)
    if context is not None:
        environment.update(git_credential_environment(context.github.token if context.github else None))
        if context.mcp_token is not None:
            environment["FLOWTOOL_MCP_TOKEN"] = context.mcp_token.get_secret_value()
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
        f'{{url={json.dumps(url)}, bearer_token_env_var="FLOWTOOL_MCP_TOKEN", '
        'enabled_tools=["open_pr"], required=true, default_tools_approval_mode="auto"}'
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
            "FLOWTOOL_TRUSTED_HOST_EXECUTION=true"
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


def _parse_thread_id(jsonl_text: str) -> str | None:
    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "thread.started":
            return evt.get("thread_id")
    return None


class CodexAgent:
    """实现 ports.CodingAgent。"""

    def __init__(self, *, trusted_host: bool = False, timeout: float = 1200.0):
        self.trusted_host = trusted_host
        self.timeout = timeout

    async def start(
        self,
        task: str,
        cwd: Path,
        *,
        output_schema: Path | None = None,
        context: CodingExecutionContext | None = None,
    ) -> CodingExecutionResult:
        args = _start_args(task, cwd, trusted_host=self.trusted_host, context=context)
        return await self._run(args, output_schema, cwd=cwd, parse_thread=True, context=context)

    async def resume(
        self, session_id: str, prompt: str, *, cwd: Path, output_schema: Path | None = None,
        context: CodingExecutionContext | None = None,
    ) -> CodingExecutionResult:
        if not self.trusted_host:
            raise RuntimeError(
                "Codex trusted-host full-access execution requires "
                "FLOWTOOL_TRUSTED_HOST_EXECUTION=true"
            )
        # CLI 0.153.4 accepts config overrides on resume, but not start's -C/-s flags.
        # Override the saved broker/environment for every process, including recovered sessions.
        args = [
            CODEX_BIN, "exec", "resume", session_id, prompt,
            "--json", "--skip-git-repo-check",
            "-c", f"cwd={json.dumps(str(cwd))}",
            "-c", 'sandbox_mode="danger-full-access"',
            *_mcp_args(context),
            "-c", 'shell_environment_policy.inherit="all"',
        ]
        return await self._run(args, output_schema, cwd=cwd, parse_thread=False,
                               session_id=session_id, context=context)

    async def _run(
        self,
        args: list[str],
        output_schema: Path | None,
        *,
        cwd: Path,
        parse_thread: bool,
        session_id: str | None = None,
        context: CodingExecutionContext | None = None,
    ) -> CodingExecutionResult:
        with tempfile.TemporaryDirectory(prefix="flowtool-coding-") as directory:
            run_directory = Path(directory)
            environment = _codex_environment(context=context)
            if context is not None and context.github is not None:
                environment.update(github_environment(context.github, run_directory, environment))
            last_file = run_directory / "last-message.txt"
            args += ["-o", str(last_file)]
            if output_schema is not None:
                args += ["--output-schema", str(output_schema)]

            logger.info("codex run started: resume={} output_schema={}", not parse_thread, output_schema is not None)
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
                    buffer.extend(chunk)

            tasks = [asyncio.create_task(read(proc.stdout, stdout)),
                     asyncio.create_task(read(proc.stderr, stderr)),
                     asyncio.create_task(proc.wait())]
            io = asyncio.gather(*tasks)

            async def cleanup() -> None:
                # Reap the group before removing its wrapper/config, including surviving children.
                try:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    # Detached descendants may hold the pipes open even after the CLI exits.
                    await asyncio.wait_for(proc.wait(), timeout=5)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await asyncio.gather(io, return_exceptions=True)

            try:
                await wait_for_inactivity(io, timeout=self.timeout, last_activity=lambda: last_activity)
            except TimeoutError:
                timeout_detail = f"Codex: no new stdout/stderr for {self.timeout:g}s; execution timed out"
                logger.error(timeout_detail)
                return CodingExecutionResult(session_id=session_id, last_message="", exit_code=124,
                                             failure_detail=timeout_detail)
            finally:
                stopping = asyncio.create_task(cleanup())
                try:
                    await asyncio.shield(stopping)
                except asyncio.CancelledError:
                    await stopping
                    raise

            jsonl = stdout.decode(errors="replace")
            sid = _parse_thread_id(jsonl)
            last_message = last_file.read_text() if last_file.exists() else ""
            detail = stderr.decode(errors="replace") if proc.returncode else None
            if context is not None:
                tokens = [context.mcp_token, context.github.token if context.github else None]
                for token in tokens:
                    if token is not None:
                        last_message = last_message.replace(token.get_secret_value(), "[REDACTED]")
                        if detail:
                            detail = detail.replace(token.get_secret_value(), "[REDACTED]")
            if proc.returncode != 0:
                logger.error("codex exit={}", proc.returncode)
                # Only a complete pre-execution rejection permits fallback. Generic errors or
                # partial output may follow edits or a push and must not replay execution.
                error_lines = stderr.decode(errors="replace").strip().splitlines()
                if (session_id and not jsonl.strip() and not last_message.strip() and error_lines
                        and error_lines[-1] == "Error: thread/resume: thread/resume failed: "
                        f"no rollout found for thread id {session_id} (code -32600)"):
                    raise SessionUnavailable("Codex rejected the saved session before execution")
            return CodingExecutionResult(
                session_id=sid, last_message=last_message, exit_code=proc.returncode or 0,
                failure_detail=detail[-4000:] if detail else None,
            )
