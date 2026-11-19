import asyncio
import json
import os
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest

from app.worker import model_catalog


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str, str], None]:
    def install(name: str, code: str) -> None:
        script = tmp_path / name
        script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(code))
        script.chmod(0o700)

    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-config"))
    monkeypatch.setenv("AMP_API_KEY", "test-auth")
    monkeypatch.setenv("FLOWTOOL_WORKER_TOKEN", "do-not-inherit")
    return install


@pytest.mark.parametrize("agent", ["codex", "amp"])
async def test_refresh_projects_only_coding_models(
    agent: Literal["codex", "amp"], cli: Callable[[str, str], None], tmp_path: Path,
) -> None:
    rows: list[dict[str, object]]
    if agent == "amp":
        args = ["plugins", "show-agent-options", "--json", "--log-file", os.devnull]
        rows = [
            {"id": "provider/Future", "displayName": "Future",
             "capabilities": {"tools": True, "efforts": ["low", "max"]}},
            {"id": "provider/image", "capabilities": {"tools": False}},
        ]
        check = "assert os.environ['AMP_API_KEY']=='test-auth' and os.environ['AMP_SKIP_UPDATE_CHECK']=='1'"
        efforts = ["low", "max"]
    else:
        args = ["debug", "models"]
        rows = [
            {"slug": "provider/Future", "display_name": "Future", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "high", "description": "not public"}]},
            {"slug": "internal", "visibility": "hide"},
        ]
        check = f"assert os.environ['CODEX_HOME']=={str(tmp_path / 'codex-config')!r}"
        efforts = ["high"]
    cli(agent, f"""
        import os, sys
        from pathlib import Path
        assert sys.argv[1:] == {args!r} and 'FLOWTOOL_WORKER_TOKEN' not in os.environ
        {check}
        print((Path.home() / 'response').read_text())
    """)
    response = tmp_path / "response"
    response.write_text('{"models": []}')
    assert await model_catalog.discover_models(agent) == []
    response.write_text(json.dumps({"models": rows, "private": "do not forward"}))
    assert [m.model_dump() for m in await model_catalog.discover_models(agent)] == [
        {"id": "provider/Future", "displayName": "Future", "efforts": efforts},
    ]


@pytest.mark.parametrize("code", [
    None, "import sys; print('{\"models\": []}'); sys.exit('secret-in-stderr')", "print('secret-in-stdout')",
    "print('{\"models\": {}}')", "print('{\"models\": [{\"visibility\": \"list\"}]}')",
    "print(' ' * 8192 + '{\"models\": []}')",
])
async def test_unavailable_is_sanitized(
    code: str | None, cli: Callable[[str, str], None], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_catalog, "_MAX_BYTES", 4096)
    if code is not None:
        cli("codex", code)
    with pytest.raises(model_catalog.CatalogUnavailable) as error:
        await model_catalog.discover_models("codex")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("cancel", [False, True])
async def test_interrupted_discovery_reaps_cli(
    cancel: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pidfile = tmp_path / "pid"
    binary = tmp_path / "amp"
    binary.write_text(f'#!/bin/sh\necho $$ > "{pidfile}"\nexec /bin/sleep 30\n')
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(model_catalog, "_TIMEOUT", 10 if cancel else 2)
    task = asyncio.create_task(model_catalog.discover_models("amp"))
    if cancel:
        async with asyncio.timeout(5):
            while not pidfile.exists():
                await asyncio.sleep(0.01)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else model_catalog.CatalogUnavailable):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
