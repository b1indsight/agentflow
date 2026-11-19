"""Read native coding-agent model metadata without opening a coding session."""

import asyncio
import json
import os
import signal
import subprocess
from contextlib import suppress
from typing import Literal, assert_never

import anyio

from app.contracts.catalog import CatalogModel
from app.worker.execution.amp_agent import AMP_BIN, amp_environment
from app.worker.execution.codex import CODEX_BIN, _codex_environment

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


async def discover_models(agent: Literal["codex", "amp"]) -> list[CatalogModel]:
    """Requery the worker's native CLI; results are candidates, not proof of entitlement."""
    if agent == "codex":
        # The default uses Codex's own refresh policy; --bundled bypasses it.
        command, env = [CODEX_BIN, "debug", "models"], _codex_environment()
    elif agent == "amp":
        command = [AMP_BIN, "plugins", "show-agent-options", "--json", "--log-file", os.devnull]
        env = amp_environment()
    else:
        assert_never(agent)

    try:
        rows = json.loads(await _read_cli(command, env))["models"]
        if not isinstance(rows, list):
            raise TypeError
        models = []
        for row in rows:
            if agent == "codex":
                if row["visibility"] != "list":
                    continue
                levels = row["supported_reasoning_levels"]
                if not isinstance(levels, list):
                    raise TypeError
                models.append(CatalogModel(id=row["slug"], displayName=row["display_name"],
                                           efforts=[level["effort"] for level in levels]))
            else:
                caps = row["capabilities"]
                if not isinstance(caps["tools"], bool):
                    raise TypeError
                if caps["tools"]:
                    models.append(CatalogModel(id=row["id"], displayName=row["displayName"],
                                               efforts=caps["efforts"]))
        return models
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        # Native diagnostics and validation inputs can contain configuration or credentials.
        raise CatalogUnavailable("model catalog unavailable") from None
