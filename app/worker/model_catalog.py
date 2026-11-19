"""Read native coding-agent model metadata without opening a coding session."""

import asyncio
import json
import os
import signal
import subprocess
from collections.abc import Callable
from contextlib import suppress

import anyio

from app.contracts.catalog import CatalogModel

_TIMEOUT = 15
_MAX_BYTES = 4 * 1024 * 1024  # Codex includes long instructions for each model.


class CatalogUnavailable(Exception):
    """The CLI did not return a complete, readable catalog within worker limits."""


async def _read_cli(command: list[str], env: dict[str, str]) -> bytearray:
    async with asyncio.timeout(_TIMEOUT):
        async with await anyio.open_process(
            command, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ) as proc:
            try:
                assert proc.stdout is not None
                data = bytearray()
                async for chunk in proc.stdout:
                    if len(data) + len(chunk) > _MAX_BYTES:
                        raise CatalogUnavailable("catalog output exceeded byte limit")
                    data.extend(chunk)
                if await proc.wait() != 0:
                    raise CatalogUnavailable("catalog command failed")
                return data
            finally:
                # The executable may be a launcher. Kill its group before closing
                # pipes and reaping, even when its child outlives the launcher.
                with suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)


async def read_model_catalog(command: list[str], env: dict[str, str],
                             parse: Callable[[dict], CatalogModel | None]) -> list[CatalogModel]:
    """Read a bounded models array; the backend selects and decodes its own rows."""
    try:
        rows = json.loads(await _read_cli(command, env))["models"]
        if not isinstance(rows, list):
            raise TypeError
        return [model for row in rows if (model := parse(row)) is not None]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        # Native diagnostics and validation inputs can contain configuration or credentials.
        raise CatalogUnavailable("model catalog unavailable") from None
