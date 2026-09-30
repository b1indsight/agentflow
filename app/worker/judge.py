"""Run one controlled bub request with bounded time, output, environment and lifetime."""

from __future__ import annotations

import asyncio
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from tempfile import TemporaryDirectory
from uuid import uuid4

from loguru import logger
from pydantic import SecretStr, ValidationError

from app.domain.context_models import MAX_CONTEXT_BYTES, ReviewContext
from app.domain.judge_models import Failure, JudgeDecision, JudgeRequest, strict_json, waiting


class OutputLimitError(Exception):
    pass


class BubJudge:
    """Judge port. Credentials enter only this adapter and the short-lived child environment."""

    def __init__(self, *, model: str = "", api_key: SecretStr | None = None,
                 api_base: str | None = None, timeout: float = 60.0) -> None:
        self.model, self.api_key, self.api_base, self.timeout = model, api_key, api_base, timeout

    def configuration_failure(self) -> Failure | None:
        """Local readiness check: never contacts a provider or reads personal bub settings."""
        if not self.model or ":" not in self.model or not self.api_key:
            return "missing-configuration"
        if not self.api_key.get_secret_value().strip():
            return "missing-configuration"
        try:
            if version("bub") != "0.4.1":
                return "adapter-unavailable"
        except PackageNotFoundError:
            return "adapter-unavailable"
        return None

    async def triage(self, context: ReviewContext) -> JudgeDecision:
        request = JudgeRequest(session_id=uuid4().hex, context=context)
        if (context.limitations or len(context.model_dump_json().encode()) > MAX_CONTEXT_BYTES
                or context.pull_request is None or context.pull_request.state != "open"
                or context.pull_request.merged):
            return waiting(request, self.model, "incomplete-context")
        failure = self.configuration_failure()
        if failure:
            return waiting(request, self.model, failure)
        assert self.api_key is not None
        env = {
            "PATH": os.defpath, "LANG": "C.UTF-8",
            "FLOWTOOL_JUDGE_MODEL": self.model,
            "FLOWTOOL_JUDGE_API_KEY": self.api_key.get_secret_value(),
        }
        if self.api_base:
            env["FLOWTOOL_JUDGE_API_BASE"] = self.api_base
        with TemporaryDirectory(prefix="flowtool-judge-") as directory:
            env["BUB_HOME"] = directory
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-I", "-m", "app.worker.judge_runner", env=env, cwd=directory,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError:
                return waiting(request, self.model, "start-failed")
            try:
                async with asyncio.timeout(self.timeout):
                    out = await _exchange(proc, request.model_dump_json().encode())
            except TimeoutError:
                return waiting(request, self.model, "timeout")
            except OutputLimitError:
                return waiting(request, self.model, "output-limit")
            finally:
                await _reap(proc)
        if proc.returncode != 0:
            return waiting(request, self.model, "nonzero-exit")
        if not out:
            return waiting(request, self.model, "empty-output")
        try:
            decision = JudgeDecision.model_validate(strict_json(out))
            if (decision.context_digest != context.digest or decision.model != self.model
                    or decision.session_id != request.session_id):
                raise ValueError("envelope identity mismatch")
            return decision
        except (ValueError, ValidationError):
            return waiting(request, self.model, "invalid-envelope")


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Kill and drain together: a paused pipe can block Process.wait even after SIGKILL."""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass  # The child exited between the returncode check and the signal.
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
        logger.warning("Judge cleanup exceeded its time limit after termination")


async def _exchange(proc: asyncio.subprocess.Process, request: bytes) -> bytes:
    """Drain both pipes concurrently so verbose/misbehaving children cannot deadlock or fill RAM."""
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
            pass  # Exit code/envelope below diagnoses a child which exited before reading stdin.

    stdout = asyncio.create_task(read(proc.stdout, 64_000))
    tasks = [stdout, asyncio.create_task(read(proc.stderr, 16_000)), asyncio.create_task(write())]
    try:
        await asyncio.gather(*tasks)
        await proc.wait()
        return stdout.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
