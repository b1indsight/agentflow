"""CodingAgent adapter for the pinned, locally supervised Amp CLI."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, ValidationError

from app.contracts.conversation import SessionId
from app.domain.models import CodingExecutionContext, CodingExecutionResult
from app.worker.execution.amp_cli import (
    AMP_TOOLS,
    AmpProcessResult,
    amp_environment,
    run_amp,
    write_amp_settings,
)
from app.worker.execution.gh import github_environment


class _Init(BaseModel):
    model_config = ConfigDict(extra="ignore")
    session_id: SessionId
    tools: list[StrictStr]


class _Result(BaseModel):
    model_config = ConfigDict(extra="ignore")
    session_id: SessionId
    subtype: StrictStr
    is_error: StrictBool
    result: StrictStr | None = None
    error: StrictStr = ""


def _workspace_settings(cwd: Path) -> None:
    """Refuse repository settings that take precedence over our invocation policy.

    Managed Amp policy still belongs to the operator. CLI settings/tool lists do
    not isolate a full-access agent from its account, filesystem or network.
    """
    for directory in (cwd, *cwd.parents):
        if any((directory / ".amp" / name).exists() or (directory / ".amp" / name).is_symlink()
               for name in ("settings.json", "settings.jsonc")):
            raise ValueError("workspace Amp settings override worker policy; use an isolated worker workspace")
        if (directory / ".git").exists():
            break


def _outcome(process: AmpProcessResult, expected: str | None) -> CodingExecutionResult:
    """Only an intact matching init/final pair can carry a successful final answer."""
    session_id = expected
    try:
        init: _Init | None = None
        final: _Result | None = None
        for line in process.stdout.decode().splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or final is not None:
                raise ValueError("invalid Amp event order")
            kind = event.get("type")
            if init is None and (kind != "system" or event.get("subtype") != "init"):
                raise ValueError("Amp stream must begin with initialization")
            if kind == "system" and event.get("subtype") == "init":
                if init is not None:
                    raise ValueError("duplicate Amp initialization")
                init = _Init.model_validate(event)
                if not init.session_id.startswith("T-") or (expected and init.session_id != expected):
                    raise ValueError("Amp returned a different thread")
                session_id = init.session_id
                if not set(init.tools) <= AMP_TOOLS | {"mcp__flow__open_pr"}:
                    raise ValueError("Amp effective tool set exceeds worker policy")
                if "shell_command" not in init.tools:
                    raise ValueError("Amp thread has no native shell; cannot execute coding tasks")
            elif kind == "result":
                final = _Result.model_validate(event)
            elif kind not in {"system", "user", "assistant"}:
                raise ValueError("unknown Amp stream event")
        if init is None or final is None or final.session_id != session_id:
            raise ValueError("incomplete or mismatched Amp result")
        if process.timed_out:
            raise ValueError("Amp execution timed out")
        if process.overflow:
            raise ValueError("Amp execution output exceeded byte limit")
        if process.exit_code or final.is_error or final.subtype != "success":
            return CodingExecutionResult(
                session_id=session_id, last_message="", exit_code=process.exit_code or 1,
                failure_detail=final.error or "Amp execution failed",
            )
        if final.result is None:
            raise ValueError("Amp success has no final answer")
        return CodingExecutionResult(session_id=session_id, last_message=final.result, exit_code=0)
    except (ValueError, ValidationError, UnicodeDecodeError) as error:
        # Do not expose validation inputs or raw JSON: they may contain thinking or credentials.
        detail = ("Amp execution timed out" if process.timed_out else
                  "Amp execution output exceeded byte limit" if process.overflow else
                  str(error) if type(error) is ValueError else "invalid Amp event stream")
        return CodingExecutionResult(
            session_id=session_id, last_message="", exit_code=124 if process.timed_out else process.exit_code or 1,
            failure_detail=detail,
        )


class AmpAgent:
    """Execute/resume a private local thread; executor retains schema and VCS authority."""

    def __init__(self, *, trusted_host: bool = False, timeout: float = 1200.0):
        self.trusted_host = trusted_host
        self.timeout = timeout

    async def start(
        self, task: str, cwd: Path, *, output_schema: Path | None = None,
        context: CodingExecutionContext | None = None,
    ) -> CodingExecutionResult:
        return await self._run(task, cwd, output_schema=output_schema, context=context)

    async def resume(
        self, session_id: str, prompt: str, *, cwd: Path, output_schema: Path | None = None,
        context: CodingExecutionContext | None = None,
    ) -> CodingExecutionResult:
        # Never select a recent thread or silently migrate another backend's session.
        if not re.fullmatch(r"T-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", session_id):
            raise ValueError("resume requires an explicit Amp thread ID")
        return await self._run(prompt, cwd, session_id=session_id, output_schema=output_schema, context=context)

    async def _run(
        self, prompt: str, cwd: Path, *, session_id: str | None = None,
        output_schema: Path | None = None, context: CodingExecutionContext | None = None,
    ) -> CodingExecutionResult:
        if not self.trusted_host:
            raise RuntimeError("Amp trusted-host execution requires FLOWTOOL_TRUSTED_HOST_EXECUTION=true")
        _workspace_settings(cwd)
        url = context.mcp_url if context else None
        token = context.mcp_token if context else None
        if (url is None) != (token is None):
            raise ValueError("worker MCP requires an endpoint and scoped token together")
        if output_schema is not None:
            prompt += ("\n\nReturn exactly one JSON value matching this JSON Schema as your final answer. "
                       "Do not wrap it in Markdown or add explanatory text.\n" + output_schema.read_text())
        with tempfile.TemporaryDirectory(prefix="flowtool-amp-") as directory:
            run_directory = Path(directory)
            settings = write_amp_settings(run_directory, mcp_url=url)
            environment = amp_environment(context=context)
            if context and context.github:
                environment.update(github_environment(context.github, run_directory, environment))
            args = (["threads", "continue", session_id] if session_id else []) + [
                "--settings-file", str(settings), "--executor", "local", "--mode", "low",
                "--visibility", "private", "--no-ide", "--no-notifications",
                "--execute", "--stream-json", "--no-archive-after-execute", "--log-file", os.devnull,
            ]
            process = await run_amp(args, cwd=cwd, env=environment, input_text=prompt, timeout=self.timeout)
            result = _outcome(process, session_id)
            # No SessionUnavailable fallback: the observed CLI stderr alone has not
            # proven that every missing-thread rejection precedes all execution.
            secrets = [value for key, value in environment.items()
                       if key in {"AMP_API_KEY", "FLOWTOOL_MCP_TOKEN", "FLOWTOOL_PUSH_TOKEN", "GH_TOKEN"} and value]
            for secret in secrets:
                result.last_message = result.last_message.replace(secret, "[REDACTED]")
                if result.failure_detail:
                    result.failure_detail = result.failure_detail.replace(secret, "[REDACTED]")
            if result.failure_detail:
                result.failure_detail = result.failure_detail[-4000:]
            return result
