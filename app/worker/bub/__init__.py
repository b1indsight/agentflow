"""One isolated bub completion; credentials never enter generic task or result models."""

import asyncio
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory

from loguru import logger
from pydantic import BaseModel, Field, JsonValue, SecretStr

from app.contracts.catalog import CatalogModel
from app.contracts.conversation import HistoryPage, HistoryQuery
from app.domain.inference_models import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    InferenceFailure,
    InferenceMessage,
)
from app.domain.json import strict_json
from app.domain.models import CodingExecutionContext, CodingExecutionResult, SessionUnavailable
from app.worker.coding_agent import AgentPrompt, CodingAgent


class Completion(BaseModel, extra="forbid"):
    """Private child input. Job identity and deadlines belong to the parent worker."""

    messages: list[InferenceMessage] = Field(min_length=1, max_length=64)
    output_schema: dict[str, JsonValue] | None
    max_output_bytes: int = Field(ge=0, le=MAX_OUTPUT_BYTES)


class OutputLimitError(Exception):
    pass


class BubCodingAgent(CodingAgent):
    """Stateless, tool-free backend. Only its isolated child loads the model SDK."""

    backend = "bub"
    trusted_host = None
    adapter_version = "bub-0.4.1/v1"
    max_output_bytes = MAX_OUTPUT_BYTES

    def __init__(self, *, model: str = "", api_key: SecretStr | None = None,
                 api_base: str | None = None) -> None:
        self.model, self.api_key, self.api_base = model.strip(), api_key, api_base

    @property
    def configured(self) -> bool:
        """Any supplied setting enables preflight; only a complete configuration can register."""
        return bool(self.model or (self.api_key and self.api_key.get_secret_value().strip())
                    or (self.api_base and self.api_base.strip()))

    def configuration_failure(self) -> InferenceFailure | None:
        parts = self.model.split(":", 1)
        if (len(parts) != 2 or not all(part.strip() for part in parts)
                or not self.api_key or not self.api_key.get_secret_value().strip()):
            return InferenceFailure(code="configuration")
        try:
            if version("bub") != "0.4.1":
                return InferenceFailure(code="configuration")
        except PackageNotFoundError:
            return InferenceFailure(code="configuration")
        return None

    async def discover_models(self) -> list[CatalogModel]:
        return []  # No model selection or discovery; the host pins the model at startup.

    def read_history(self, query: HistoryQuery) -> HistoryPage:
        return HistoryPage(session_id=query.session_id, status="unsupported")

    async def _run(
        self, prompt: AgentPrompt, cwd: Path, *, session_id: str | None,
        output_schema: Path | None, context: CodingExecutionContext | None,
        model: str | None, effort: str | None, max_output_bytes: int,
    ) -> CodingExecutionResult:
        if session_id is not None:
            raise SessionUnavailable("bub has no resumable sessions")
        if (context is not None or effort is not None or model not in (None, self.model)
                or self.configuration_failure() is not None):
            return CodingExecutionResult(None, "", 1, failure_kind="configuration")
        try:
            request = Completion.model_validate({
                "messages": [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt,
                "output_schema": strict_json(output_schema.read_bytes()) if output_schema else None,
                "max_output_bytes": min(max_output_bytes, MAX_OUTPUT_BYTES),
            }).model_dump_json().encode()
            if len(request) > MAX_INPUT_BYTES:
                return CodingExecutionResult(None, "", 1, failure_kind="configuration")
        except ValueError:
            return CodingExecutionResult(None, "", 1, failure_kind="configuration")
        assert self.api_key is not None
        env = {
            "PATH": os.defpath, "LANG": "C.UTF-8",
            "AGENTFLOW_JUDGE_MODEL": self.model,
            "AGENTFLOW_JUDGE_API_KEY": self.api_key.get_secret_value(),
        }
        if self.api_base:
            env["AGENTFLOW_JUDGE_API_BASE"] = self.api_base
        with TemporaryDirectory(prefix="agentflow-inference-", dir=cwd) as directory:
            env["BUB_HOME"] = directory
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-m", "app.worker.bub", env=env, cwd=directory,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out = await _exchange(proc, request)
            except TimeoutError:
                return CodingExecutionResult(None, "", 124, failure_kind="timeout")
            except OutputLimitError:
                return CodingExecutionResult(None, "", 1, failure_kind="output_limit")
            finally:
                cleanup = asyncio.create_task(_reap(proc))
                cancelled = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
                if cancelled:
                    raise asyncio.CancelledError
        if proc.returncode != 0:
            return CodingExecutionResult(None, "", 1, failure_kind="backend_error")
        try:
            data = strict_json(out)
            if isinstance(data, str) and data.strip():
                return CodingExecutionResult(None, data, 0, model=self.model)
            failure = InferenceFailure.model_validate(data)
            return CodingExecutionResult(None, "", 1, failure_kind=failure.code,
                                         retry_after_seconds=failure.retry_after_seconds)
        except ValueError:
            return CodingExecutionResult(None, "", 1, failure_kind="invalid_response")


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Kill and drain together: a paused pipe can block wait even after SIGKILL."""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    if proc.stdin is not None:
        proc.stdin.close()

    async def discard(stream: asyncio.StreamReader) -> None:
        while await stream.read(8192):
            pass

    try:
        async with asyncio.timeout(5):
            await asyncio.gather(proc.wait(), *(discard(stream) for stream in
                (proc.stdout, proc.stderr) if stream is not None))
    except TimeoutError:
        logger.warning("Inference cleanup exceeded its time limit after termination")


async def _exchange(proc: asyncio.subprocess.Process, request: bytes) -> bytes:
    """Bound concurrent pipes, allowing JSON escaping overhead on the answer envelope."""
    async def read(stream: asyncio.StreamReader, limit: int) -> bytes:
        chunks = bytearray()
        while chunk := await stream.read(8192):
            chunks.extend(chunk)
            if len(chunks) > limit:
                raise OutputLimitError
        return bytes(chunks)

    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    async def write() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(request)
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass

    stdout = asyncio.create_task(read(proc.stdout, MAX_OUTPUT_BYTES * 6 + 4096))
    tasks = [stdout, asyncio.create_task(read(proc.stderr, 16_000)), asyncio.create_task(write())]
    try:
        await asyncio.gather(*tasks)
        await proc.wait()
        return stdout.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
