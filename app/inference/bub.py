"""An ordinary CodingAgent backed by an isolated, tool-free Bub SDK child."""

import asyncio
import json
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from loguru import logger
from pydantic import SecretStr

from app.contracts.conversation import HistoryPage, HistoryQuery
from app.domain.inference_models import InferenceFailure
from app.domain.models import CodingExecutionContext, CodingExecutionResult, SessionUnavailable
from app.worker.coding_agent import CodingAgent


class OutputLimitError(Exception):
    pass


class BubAgent(CodingAgent):
    """Explicit configuration only; SDK imports and model calls stay in a short-lived child."""

    backend = "bub"

    def __init__(self, *, model: str = "", api_key: SecretStr | None = None,
                 api_base: str | None = None) -> None:
        self.model, self.api_key, self.api_base = model.strip(), api_key, api_base

    @property
    def configured(self) -> bool:
        """Any supplied setting enables preflight; only a complete configuration can register."""
        return bool(self.model or (self.api_key and self.api_key.get_secret_value().strip())
                    or (self.api_base and self.api_base.strip()))

    def configuration_failure(self, *, model: str | None = None) -> InferenceFailure | None:
        """Check explicit provider configuration without importing its SDK in the worker."""
        parts = (self.model if model is None else model).split(":", 1)
        if (len(parts) != 2 or not all(part.strip() for part in parts)
                or not self.api_key or not self.api_key.get_secret_value().strip()):
            return InferenceFailure(code="configuration")
        try:
            if version("bub") != "0.4.1":
                return InferenceFailure(code="configuration")
        except PackageNotFoundError:
            return InferenceFailure(code="configuration")
        return None

    async def _run(
        self, prompt: str, cwd: Path, *, session_id: str | None = None,
        output_schema: Path | None = None, context: CodingExecutionContext | None = None,
        model: str | None = None, effort: str | None = None, max_output_bytes: int,
    ) -> CodingExecutionResult:
        """Exchange private SDK data; CodingAgent owns timeout and final-answer validation."""
        if session_id is not None:
            raise SessionUnavailable("Bub does not support resuming sessions")
        selected_model = self.model if model is None else model.strip()
        if self.configuration_failure(model=selected_model) or effort is not None:
            return CodingExecutionResult(None, "", 1, failure_kind="configuration",
                                         backend=self.backend, model=selected_model)
        assert self.api_key is not None
        env = {
            "PATH": os.defpath, "LANG": "C.UTF-8",
            "FLOWTOOL_JUDGE_MODEL": selected_model,
            "FLOWTOOL_JUDGE_API_KEY": self.api_key.get_secret_value(),
        }
        if self.api_base:
            env["FLOWTOOL_JUDGE_API_BASE"] = self.api_base
        request = json.dumps({
            "prompt": prompt,
            "output_schema": json.loads(output_schema.read_text()) if output_schema else None,
            "max_output_bytes": max_output_bytes,
        }).encode()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-m", "app.inference.runner", env=env, cwd=cwd,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out = await _exchange(proc, request, max_output_bytes)
        except OSError:
            return CodingExecutionResult(None, "", 1, failure_kind="configuration",
                                         backend=self.backend, model=selected_model)
        except OutputLimitError:
            return CodingExecutionResult(None, "", 1, failure_kind="output_limit",
                                         backend=self.backend, model=selected_model)
        finally:
            if proc is not None:
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
            return CodingExecutionResult(None, "", 1, failure_kind="backend_error",
                                         backend=self.backend, model=selected_model)
        try:
            return CodingExecutionResult(**json.loads(out))
        except (ValueError, TypeError):
            return CodingExecutionResult(None, "", 1, failure_kind="invalid_response",
                                         backend=self.backend, model=selected_model)

    def read_history(self, query: HistoryQuery) -> HistoryPage:
        raise NotImplementedError("Bub does not retain session history")


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


async def _exchange(
    proc: asyncio.subprocess.Process, request: bytes, max_output_bytes: int,
) -> bytes:
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

    stdout = asyncio.create_task(read(proc.stdout, max_output_bytes * 6 + 4096))
    tasks = [stdout, asyncio.create_task(read(proc.stderr, 16_000)), asyncio.create_task(write())]
    try:
        await asyncio.gather(*tasks)
        await proc.wait()
        return stdout.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
