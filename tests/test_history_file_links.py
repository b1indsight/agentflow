"""Outbound history rewrites destinations without changing native history or its cursors."""

import asyncio
import json
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from pydantic import SecretStr

from app.contracts.conversation import HistoryQuery
from app.contracts.files import FileFailure
from app.persistence.activity import ActivityStore
from app.persistence.console import ConsoleStore
from app.worker.config import WorkerSettings
from app.worker.execution.amp_agent import AmpCodingAgent, AmpProcessResult
from app.worker.runtime import WorkerRuntime
from tests.test_conversation_history import AMP_SESSION, SESSION, message, rollout
from tests.test_remote_e2e import remote_connection
from tests.test_remote_e2e import remote_flow as remote_flow  # noqa: PLC0414
from tests.test_workspace_dispatch import wait_until

LINK = "/api/files/" + "a" * 32


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return WorkerRuntime(WorkerSettings(
        worker_id="worker-1", worker_token=SecretStr("worker-secret"),
        serve_ws_url="wss://unused", worker_root=tmp_path / "worker",
        coding_agent="codex", trusted_host_execution=False,
    ))


async def test_only_message_markdown_destinations_are_registered(worker, tmp_path, monkeypatch):
    # One target occurs in prose, code, a label, and real inline/reference/image links.
    # Replacing strings globally would expose the code-only files and corrupt the other text.
    target = tmp_path / ".amp/in/artifacts/报告 (100%).md"
    escaped = str(target).replace(" ", "%20").replace("(", r"\(").replace(")", r"\)")
    uri = target.as_uri()
    text = (
        f"裸路径 {target}\r\n\r\n"
        f"[{target}]({escaped} \"报告标题\")\r\n\r\n"
        f"![图片](<{uri}> '原题')\r\n\r\n"
        f"> - [引用][报告]\r\n>\r\n> [报告]:\r\n>   <{uri}> \"标题\"\r\n\r\n"
        f"Unicode 分隔\u2028仍在同一行 [文件](<{uri}>)\r\n\r\n"
        "`[代码](/tmp/code)`\r\n\r\n```md\r\n[围栏](/tmp/fence)\r\n```\r\n\r\n"
        "\\[转义](/tmp/escaped)\r\n\r\n"
        "[外链](https://example.test/tmp/external) [相对](./local.md)\r\n\r\n"
        "[远程文件](file://other-host/tmp/remote)\r\n\r\n"
        "脚注[^n]\r\n\r\n[^n]: 内容 [脚注文件](/tmp/footnote)\r\n\r\n"
        "    第二段 [文件](/tmp/footnote-two)\r\n\r\n"
        "    [脚注定义]: /tmp/footnote-definition\r\n\r\n"
        "[正文引用脚注里的定义][脚注定义]\r\n"
    )
    source = rollout(tmp_path / "codex", [message("assistant", text), message("system", "[系统](/tmp/system)")])

    async def register(path, backend):
        assert path == target
        assert backend == "codex"
        return LINK

    monkeypatch.setattr(worker, "register_file", register)
    page = await worker.read_history(HistoryQuery(session_id=SESSION))
    named = LINK + "#name=%E6%8A%A5%E5%91%8A%20%28100%25%29.md"
    expected = text.replace(f"]({escaped} ", f"]({named} ").replace(f"<{uri}>", f"<{named}>")
    assert page.items[0].content.text == expected
    assert page.items[1].content.text == "[系统](/tmp/system)"
    assert json.loads(source.read_text().split("\n")[1])["payload"]["content"][0]["text"] == text


async def test_amp_repeat_and_next_page_keep_native_cursor_and_cache(worker, monkeypatch):
    # Browsing old Amp sessions must work on a Codex worker with no current assignment.
    assert worker.executions == {}
    first_text = "[报告](/tmp/report%23draft.md)"
    payload = {"id": AMP_SESSION, "messages": [
        {"protocolMessageID": "one", "role": "assistant", "content": [{"type": "text", "text": first_text}]},
        {"protocolMessageID": "two", "role": "user", "content": [{"type": "text", "text": "second"}]},
    ]}

    async def export(*args, **kwargs):
        return AmpProcessResult(stdout=json.dumps(payload).encode(), stderr=b"", exit_code=0)

    monkeypatch.setattr(AmpCodingAgent, "run_process", staticmethod(export))
    monkeypatch.setattr(worker, "register_file", AsyncMock(return_value=LINK))
    query = HistoryQuery(session_id=AMP_SESSION, limit=1)
    native = await asyncio.to_thread(worker._agents["amp"].read_history, query)
    page = await worker.read_history(query)
    assert page.items[0].content.text == f"[报告]({LINK}#name=report%23draft.md)"
    assert page.next_cursor == native.next_cursor
    assert native.items[0].content.text == first_text
    assert await asyncio.to_thread(worker._agents["amp"].read_history, query) == native
    assert await worker.read_history(query) == page
    following = await worker.read_history(HistoryQuery(session_id=AMP_SESSION, cursor=page.next_cursor))
    assert following.items[0].content.text == "second"


@pytest.mark.parametrize("history_backend", ["codex", "amp"])
async def test_private_amp_images_register_without_downloading_or_trusting_source(
    worker, tmp_path, monkeypatch, history_backend,
):
    # The published URL needs Amp auth; amp-media-source is only metadata, not file authority.
    url = "https://ampcode.com/user-content/attachments/" + "b" * 64 + "-screen.png"
    sentinel = tmp_path / "do-not-expose.txt"
    sentinel.write_text("not an image")
    full = url + "#amp-media-source=" + quote(sentinel.as_uri(), safe="")
    text = f'![截图](<{full}> "标题")\n\n[图片][ref]\n\n[ref]: <{url}>\n\n`![代码]({full})`'
    payload = {"id": AMP_SESSION, "messages": [
        {"protocolMessageID": "one", "role": "assistant", "content": [{"type": "text", "text": text}]},
    ]}

    async def export(*args, **kwargs):
        return AmpProcessResult(stdout=json.dumps(payload).encode(), stderr=b"", exit_code=0)

    async def register(source, backend):
        assert source == url and backend == "amp"
        return LINK

    monkeypatch.setattr(AmpCodingAgent, "run_process", staticmethod(export))
    monkeypatch.setattr(worker, "register_file", register)
    if history_backend == "codex":
        rollout(tmp_path / "codex", [message("assistant", text)])
    session_id = AMP_SESSION if history_backend == "amp" else SESSION
    query = HistoryQuery(session_id=session_id)
    native = await asyncio.to_thread(worker._agents[history_backend].read_history, query)
    page = await worker.read_history(query)
    named = f"{LINK}#name=screen.png"
    assert page.items[0].content.text == text.replace(f"<{full}>", f"<{named}>").replace(
        f"<{url}>", f"<{named}>",
    )
    assert page.next_cursor == native.next_cursor
    assert native.items[0].content.text == text


async def test_registration_deadline_and_failure_keep_partial_text(worker, tmp_path, monkeypatch):
    monkeypatch.setattr("app.worker.runtime.HISTORY_TIMEOUT", 0.1, raising=False)
    text = "[好](/tmp/good) [失败](/tmp/missing) [慢](/tmp/slow)"
    rollout(tmp_path / "codex", [message("assistant", text)])
    cancelled = asyncio.Event()

    async def register(path, backend):
        if path.name == "missing":
            raise FileFailure("file_not_found")
        if path.name == "slow":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return LINK

    monkeypatch.setattr(worker, "register_file", register)
    page = await asyncio.wait_for(worker.read_history(HistoryQuery(session_id=SESSION)), 1)
    assert page.items[0].content.text == f"[好]({LINK}#name=good) [失败](/tmp/missing) [慢](/tmp/slow)"
    assert cancelled.is_set()


async def test_expanding_short_links_never_overflows_item_or_page(worker, tmp_path, monkeypatch):
    text = "中" * (8192 - len("[x](/x)")) + "[x](/x)"
    rollout(tmp_path / "codex", [message("assistant", text), message("user", "[ok](/x)")])
    monkeypatch.setattr(worker, "register_file", AsyncMock(return_value=LINK))
    page = await worker.read_history(HistoryQuery(session_id=SESSION))
    assert page.items[0].content.text == text
    assert page.items[1].content.text == f"[ok]({LINK}#name=x)"
    assert not page.items[0].truncated

    # A nearly full UTF-8 source page cannot grow just because each item has space.
    body = "中" * 2600 + " [x](/x)" * 35
    rollout(tmp_path / "codex", [message("assistant", body)] * 40)
    query = HistoryQuery(session_id=SESSION)
    native = await asyncio.to_thread(worker._agents["codex"].read_history, query)
    page = await worker.read_history(query)
    assert [item.id for item in page.items] == [item.id for item in native.items]
    assert page.next_cursor == native.next_cursor
    assert sum(len(item.model_dump_json().encode()) for item in page.items) <= 256 * 1024
    texts = "".join(item.content.text for item in page.items)
    assert LINK in texts and "](/x)" in texts


async def test_file_count_and_duplicate_targets_are_bounded(worker, tmp_path, monkeypatch):
    text = " ".join(f"[{i}](/tmp/file-{i})" for i in range(17)) + " [重复](file:///tmp/file-0)"
    rollout(tmp_path / "codex", [message("assistant", text)])
    monkeypatch.setattr(worker, "register_file", AsyncMock(return_value=LINK))
    page = await worker.read_history(HistoryQuery(session_id=SESSION))
    assert page.items[0].content.text.count(LINK) == 17
    assert "[16](/tmp/file-16)" in page.items[0].content.text


async def test_uploaded_history_and_refresh_use_real_registered_files(store, remote_flow, tmp_path, monkeypatch):
    # First archive a missing file, then make it available. An EOF-only refresh
    # would never revisit those messages or repair their file links.
    flow, runtime, _github, worker, agent = remote_flow
    path = tmp_path / "报告.md"
    text = f"[报告](<{path.as_uri()}>)"
    rollout(tmp_path / "codex", [message("assistant", text)] * 53)
    finish = agent.finish

    async def finish_session(context):
        result = await finish(context)
        result.session_id = SESSION
        return result

    monkeypatch.setattr(agent, "finish", finish_session)
    async with remote_connection(runtime, worker):
        await wait_until(lambda: worker.executions.get(flow.id) is not None
                         and worker.executions[flow.id].state.load().acknowledged)
        jobs_before = store.jobs.list_jobs(flow.id)
        console = ConsoleStore(store.database)
        archive = ActivityStore(store.database)
        for available in (False, True):
            if available:
                path.write_text("# 报告\n\n内容 100%\n")
            console.request_sync(flow.id, active_only=False)
            for number in range(2):
                with store.connection() as connection:
                    connection.execute("UPDATE console_sync SET pending=FALSE WHERE source='github'")
                    connection.execute("UPDATE console_sync SET next_at=now() WHERE source='worker'")
                claim = runtime.console.store.claim()
                assert claim is not None and claim.source == "worker"
                if number == 0:
                    assert claim.cursor is None
                await runtime.console.sync(claim)
            current = archive.page(flow.id, limit=50)
            assert current.total == 53
            assert not runtime.console.store.claim()
            if not available:
                assert current.items[0].body == text
                continue
            rewritten = current.items[0].body
            assert rewritten.startswith("[报告](</api/files/")
            link_id = rewritten.removeprefix("[报告](</api/files/").removesuffix(">)").split("#")[0]
            assert rewritten.endswith("#name=%E6%8A%A5%E5%91%8A.md>)")
            assert len(link_id) == 32
            assert await runtime.files.read(link_id) == path.read_bytes()
            assert all(item.body == rewritten for item in current.items)
            assert archive.page(flow.id, snapshot=current.snapshot, cursor=current.next_cursor,
                                limit=50).items[-1].body == rewritten
        assert store.jobs.list_jobs(flow.id) == jobs_before
        assert len(agent.started) == 1
