"""CodeAgent adapter for Codex CLI in a trusted Flow-owned repository."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from loguru import logger

from app.models import AgentRun, CodeAgentContext, CodexResult

CODEX_BIN = "codex"


_CODEX_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "CODEX_HOME",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)


def _codex_environment(
    source: Mapping[str, str] | None = None,
    context: CodeAgentContext | None = None,
) -> dict[str, str]:
    """Build a minimal environment with only the optional scoped push credential."""

    values = os.environ if source is None else source
    environment = {
        key: value for key, value in values.items() if key in _CODEX_ENVIRONMENT_KEYS
    }
    if context is not None and context.push_token is not None:
        environment.update(
            {
                "FLOWTOOL_PUSH_TOKEN": context.push_token.get_secret_value(),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "credential.https://github.com.username",
                "GIT_CONFIG_VALUE_0": "x-access-token",
                "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
                "GIT_CONFIG_VALUE_1": (
                    "!f() { echo password=$FLOWTOOL_PUSH_TOKEN; }; f"
                ),
            }
        )
    return environment


def _start_args(
    task: str,
    cwd: Path,
    *,
    trusted_host: bool,
    context: CodeAgentContext | None = None,
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
    if context is not None and context.mcp_socket is not None:
        overrides = {
            "mcp_servers.flow.command": sys.executable,
            "mcp_servers.flow.args": ["-m", "app.worker.tools"],
            "mcp_servers.flow.env.FLOWTOOL_MCP_SOCKET": str(context.mcp_socket),
            "mcp_servers.flow.env.FLOWTOOL_PUSH_TOKEN": "",
            "mcp_servers.flow.required": True,
            "mcp_servers.flow.enabled_tools": ["open_pr"],
            "mcp_servers.flow.default_tools_approval_mode": "auto",
        }
        for key, value in overrides.items():
            args.extend(["-c", f"{key}={json.dumps(value)}"])
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
    """实现 ports.CodeAgent。"""

    def __init__(self, *, trusted_host: bool = False, timeout: float = 1200.0):
        self.trusted_host = trusted_host
        self.timeout = timeout

    async def start(
        self,
        task: str,
        cwd: Path,
        *,
        output_schema: Path | None = None,
        context: CodeAgentContext | None = None,
    ) -> AgentRun:
        args = _start_args(task, cwd, trusted_host=self.trusted_host, context=context)
        return await self._run(args, output_schema, parse_thread=True, context=context)

    async def resume(
        self, session_id: str, prompt: str, *, output_schema: Path | None = None
    ) -> AgentRun:
        if not self.trusted_host:
            raise RuntimeError(
                "Codex trusted-host full-access execution requires "
                "FLOWTOOL_TRUSTED_HOST_EXECUTION=true"
            )
        # resume 不接受 -s / -C：cwd 与 sandbox 从会话继承
        args = [
            CODEX_BIN, "exec", "resume", session_id, prompt,
            "--json", "--skip-git-repo-check",
        ]
        return await self._run(args, output_schema, parse_thread=False, session_id=session_id)

    async def _run(
        self,
        args: list[str],
        output_schema: Path | None,
        *,
        parse_thread: bool,
        session_id: str | None = None,
        context: CodeAgentContext | None = None,
    ) -> AgentRun:
        with tempfile.NamedTemporaryFile(prefix="codex-last-", suffix=".txt", delete=False) as file:
            last_file = Path(file.name)
        args += ["-o", str(last_file)]
        if output_schema is not None:
            args += ["--output-schema", str(output_schema)]

        logger.info("codex run started: resume={} output_schema={}", not parse_thread, output_schema is not None)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,   # 必须：否则 codex 会挂等 stdin
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_codex_environment(context=context),
        )
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.error("codex run timed out after {}s", self.timeout)
            return AgentRun(session_id=session_id, last_message="", exit_code=124)

        jsonl = stdout.decode(errors="replace")
        sid = _parse_thread_id(jsonl) if parse_thread else session_id
        last_message = last_file.read_text() if last_file.exists() else ""

        output: CodexResult | None = None
        if output_schema is not None and last_message.strip():
            # 结构化产出：codex 把最终 JSON 写进 -o 文件（符合 --output-schema）
            try:
                output = CodexResult.model_validate_json(last_message)
            except Exception as e:  # noqa: BLE001
                logger.warning("failed to parse CodexResult: {}", e)  # TODO(verify) 产出落点

        if last_file.exists():
            last_file.unlink()
        if proc.returncode != 0:
            logger.error("codex exit={}", proc.returncode)

        return AgentRun(
            session_id=sid, last_message=last_message, exit_code=proc.returncode or 0, output=output
        )
