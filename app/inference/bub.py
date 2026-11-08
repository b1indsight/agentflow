"""One isolated bub completion; credentials never enter generic task or result models."""

import asyncio
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from tempfile import TemporaryDirectory

from loguru import logger
from pydantic import SecretStr, ValidationError

from app.domain.inference_models import (
    MAX_OUTPUT_BYTES,
    InferenceError,
    InferenceFailure,
    InferenceOutput,
    InferenceTask,
)
from app.domain.json import strict_json

ADAPTER_VERSION = "bub-0.4.1/v1"


class OutputLimitError(Exception):
    pass


class BubInference:
    """Explicit configuration only; SDK imports and model calls stay in a short-lived child."""

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

    async def infer(self, task: InferenceTask) -> InferenceOutput:
        try:
            return await self._infer(task)
        except InferenceError as error:
            raise InferenceError(error.failure, backend=ADAPTER_VERSION) from None

    async def _infer(self, task: InferenceTask) -> InferenceOutput:
        if failure := self.configuration_failure():
            raise InferenceError(failure)
        assert self.api_key is not None
        env = {
            "PATH": os.defpath, "LANG": "C.UTF-8",
            "FLOWTOOL_JUDGE_MODEL": self.model,
            "FLOWTOOL_JUDGE_API_KEY": self.api_key.get_secret_value(),
        }
        if self.api_base:
            env["FLOWTOOL_JUDGE_API_BASE"] = self.api_base
        with TemporaryDirectory(prefix="flowtool-inference-") as directory:
            env["BUB_HOME"] = directory
            proc = None
            try:
                async with asyncio.timeout(task.timeout_seconds):
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable, "-I", "-m", "app.inference.runner", env=env, cwd=directory,
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    out = await _exchange(proc, task.model_dump_json().encode())
            except TimeoutError:
                raise InferenceError(InferenceFailure(code="execution_timeout")) from None
            except OSError:
                raise InferenceError(InferenceFailure(code="configuration")) from None
            except OutputLimitError:
                raise InferenceError(InferenceFailure(code="output_limit")) from None
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
            raise InferenceError(InferenceFailure(code="backend_error"))
        try:
            data = strict_json(out)
            if isinstance(data, dict) and "code" in data:
                raise InferenceError(InferenceFailure.model_validate(data))
            output = InferenceOutput.model_validate(data)
            output.validate_for(task)
            if output.model != self.model or output.adapter_version != ADAPTER_VERSION:
                raise ValueError("inference provenance mismatch")
            return output
        except (ValueError, ValidationError):
            raise InferenceError(InferenceFailure(code="invalid_response")) from None


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
