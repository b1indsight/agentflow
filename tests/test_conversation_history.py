import json
import os
from pathlib import Path

import pytest

from app.contracts.conversation import ConversationFailure, HistoryQuery
from app.worker.execution.amp_cli import AmpProcessResult
from app.worker.state import history
from app.worker.state.amp_history import AmpHistory
from app.worker.state.history import CodexHistory

SESSION = "019eac3b-a60c-7a93-a442-3718f660fc8c"
AMP_SESSION = "T-019eac3b-a60c-7a93-a442-3718f660fc8c"


def rollout(root: Path, records: list[dict]) -> Path:
    path = root / "sessions" / "2026" / f"rollout-test-{SESSION}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [{"type": "session_meta", "payload": {"id": SESSION}}, *records]
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    return path


def message(role: str, text: str) -> dict:
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}


def test_history_roles_tools_pagination_and_live_tail(tmp_path: Path) -> None:
    path = rollout(tmp_path, [
        message("user", "Fix PR feedback"),
        {"type": "event_msg", "payload": {"type": "user_message", "message": "duplicate"}},
        {"type": "response_item", "payload": {"type": "reasoning", "summary": ["private"]}},
        {"type": "response_item", "payload": {**message("assistant", "private")["payload"], "channel": "analysis"}},
        message("assistant", "Checking tests"),
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "c1",
                                               "name": "exec_command", "arguments": '{"cmd":"pytest"}'}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1",
                                               "output": "passed"}},
    ])
    reader = CodexHistory(tmp_path)
    first = reader.query(HistoryQuery(session_id=SESSION, cursor=None, limit=2))
    assert [(item.role, item.kind) for item in first.items] == [("user", "message"), ("assistant", "message")]
    assert first.has_more
    second = reader.query(HistoryQuery(session_id=SESSION, cursor=first.next_cursor, limit=100))
    assert [(item.role, item.call_id) for item in second.items] == [("assistant", "c1"), ("tool", "c1")]
    assert not second.has_more
    # Codex appends JSONL while a running session is being viewed: do not consume a partial line.
    tail = json.dumps(message("assistant", "Done"))
    with path.open("a") as file:
        file.write(tail[:20])
    partial = reader.query(HistoryQuery(session_id=SESSION, cursor=second.next_cursor, limit=100))
    assert partial.items == []
    with path.open("a") as file:
        file.write(tail[20:] + "\n")
    final = reader.query(HistoryQuery(session_id=SESSION, cursor=partial.next_cursor, limit=100))
    assert final.items[0].content.text == "Done"
    assert final.items[0].id not in {item.id for item in first.items + second.items}


def test_history_rejects_symlink_escape_and_wrong_session(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    path = rollout(outside, [message("user", "other account")])
    root = tmp_path / "codex"
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / path.name).symlink_to(path)
    # A rollout-looking symlink must never expose files outside the worker's history root.
    with pytest.raises(ConversationFailure, match="history_unreadable"):
        CodexHistory(root).query(HistoryQuery(session_id=SESSION, cursor=None, limit=10))
    path = rollout(tmp_path / "wrong", [])
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "other-session"}}) + "\n")
    with pytest.raises(ConversationFailure, match="history_unreadable"):
        CodexHistory(tmp_path / "wrong").query(HistoryQuery(session_id=SESSION, cursor=None, limit=10))


def test_history_reports_unknown_records_and_truncates_text(tmp_path: Path) -> None:
    rollout(tmp_path, [message("user", "x" * 20000),
                       {"type": "response_item", "payload": {"type": "future_item"}}])
    page = CodexHistory(tmp_path).query(HistoryQuery(session_id=SESSION, cursor=None, limit=100))
    assert page.items[0].truncated
    assert len(page.items[0].content.text) < 20000
    assert page.status == "unsupported"


def test_history_archive_and_truncated_source(tmp_path: Path) -> None:
    first = rollout(tmp_path, [message("user", "initial")])
    archived = tmp_path / "archived_sessions" / first.name
    archived.parent.mkdir()
    first.rename(archived)
    reader = CodexHistory(tmp_path)
    page = reader.query(HistoryQuery(session_id=SESSION, limit=1))
    assert page.items[0].content.text == "initial"
    assert not page.has_more
    archived.write_text("")
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        reader.query(HistoryQuery(session_id=SESSION, cursor=page.next_cursor, limit=1))


def test_history_prefers_active_file_over_archived_copy(tmp_path: Path) -> None:
    # During archival, both copies may be present; the active source takes priority.
    path = rollout(tmp_path, [message("user", "active")])
    archived = tmp_path / "archived_sessions" / path.name
    archived.parent.mkdir()
    archived.write_text(path.read_text().replace('"active"', '"archived"'))
    page = CodexHistory(tmp_path).query(HistoryQuery(session_id=SESSION))
    assert [item.content.text for item in page.items] == ["active"]


@pytest.mark.parametrize("inside_record", [False, True])
def test_history_rejects_cursor_before_metadata_or_inside_record(tmp_path: Path, inside_record: bool) -> None:
    path = rollout(tmp_path, [message("user", "first")])
    reader = CodexHistory(tmp_path)
    page = reader.query(HistoryQuery(session_id=SESSION))
    resumed = HistoryQuery(session_id=SESSION, cursor=page.next_cursor)
    _, identity = resumed.position
    assert identity is not None
    with path.open("rb") as file:
        metadata_size = len(file.readline())
    # A well-formed cursor can still point into metadata or the middle of a JSONL record.
    offset = metadata_size + 1 if inside_record else 0
    cursor = resumed.cursor_at(offset, identity)
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        reader.query(HistoryQuery(session_id=SESSION, cursor=cursor))


def test_history_malformed_limits_and_secret_filter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "private-key-value")
    path = rollout(tmp_path, [message("assistant", "private-key-value Bearer abc123 ghp_testsecret")])
    reader = CodexHistory(tmp_path)
    text = reader.query(HistoryQuery(session_id=SESSION, cursor=None, limit=100)).items[0].content.text
    assert "private-key-value" not in text and "abc123" not in text and "ghp_testsecret" not in text
    with path.open("a") as file:
        file.write("invalid-json\n")
    with pytest.raises(ConversationFailure, match="history_unreadable"):
        reader.query(HistoryQuery(session_id=SESSION, cursor=None, limit=100))
    rollout(tmp_path, [message("user", "x" * 300000)])
    with pytest.raises(ConversationFailure, match="history_limit"):
        reader.query(HistoryQuery(session_id=SESSION, cursor=None, limit=100))


def test_history_scan_budget_returns_continuation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(history, "MAX_SCAN", 1)
    rollout(tmp_path, [{"type": "turn_context", "payload": {}}, message("user", "later")])
    reader = CodexHistory(tmp_path)
    page = reader.query(HistoryQuery(session_id=SESSION, cursor=None, limit=100))
    assert page.items == [] and page.has_more
    page = reader.query(HistoryQuery(session_id=SESSION, cursor=page.next_cursor, limit=100))
    assert page.items[0].content.text == "later"


def test_history_refuses_special_files_and_racing_directory_symlink(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    path = rollout(root, [])
    path.unlink()
    os.mkfifo(path)
    reader = CodexHistory(root)
    with pytest.raises(ConversationFailure, match="history_unreadable"):
        reader.query(HistoryQuery(session_id=SESSION, cursor=None, limit=100))
    # Exercise the open boundary independently: a directory can be swapped after discovery.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_text("private")
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        reader._open(root / "link" / "data")


def test_amp_history_maps_public_blocks_and_invalidates_edited_prefix(monkeypatch) -> None:
    def native(role: str, message_id: str, content: list[dict]) -> dict:
        return {"role": role, "protocolMessageID": message_id,
                "createdAt": "2026-10-21T00:00:00Z", "content": content}

    first = native("assistant", "M-first", [
        {"type": "thinking", "thinking": "private"},
        {"type": "text", "text": "Bearer abc123 private-key-value"},
        {"type": "tool_use", "id": "TU-1", "name": "shell", "input": {"key": "private-key-value"}},
    ])
    second = native("user", "M-second", [
        {"type": "tool_result", "toolUseID": "TU-1", "run": {"result": {"output": "done"}}},
    ])
    reader = AmpHistory()
    monkeypatch.setenv("AMP_API_KEY", "private-key-value")
    monkeypatch.setattr("app.worker.state.amp_history.CACHE_SECONDS", 0)
    messages = [first]

    async def export(*args, **kwargs):
        return AmpProcessResult(json.dumps({"id": AMP_SESSION, "messages": messages}).encode(), b"", 0)

    monkeypatch.setattr("app.worker.state.amp_history.run_amp", export)
    page = reader.query(HistoryQuery(session_id=AMP_SESSION, limit=2))
    assert [(item.role, item.kind, item.id) for item in page.items] == [
        ("assistant", "message", f"{AMP_SESSION}:M-first:1"),
        ("assistant", "tool_call", f"{AMP_SESSION}:M-first:2"),
    ]
    assert "private-key-value" not in page.items[0].content.text
    # A native user tool_result is deliberately exposed as the existing tool role.
    messages.append(second)
    next_page = reader.query(HistoryQuery(session_id=AMP_SESSION, cursor=page.next_cursor))
    assert [(item.role, item.call_id) for item in next_page.items] == [("tool", "TU-1")]

    edited = native("assistant", "M-first", [{"type": "text", "text": "changed"}])
    messages[0] = edited
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        reader.query(HistoryQuery(session_id=AMP_SESSION, cursor=page.next_cursor))


def test_amp_history_pages_inside_native_messages_and_bounds_bytes(monkeypatch) -> None:
    # One native message may contain more blocks than a page: the cursor must advance.
    reader = AmpHistory()
    message = {"role": "assistant", "protocolMessageID": "M-many", "content": [
        {"type": "text", "text": "字" * 8192} for _ in range(50)
    ]}

    async def export(*args, **kwargs):
        return AmpProcessResult(json.dumps({"id": AMP_SESSION, "messages": [message]}, ensure_ascii=False).encode(), b"", 0)

    monkeypatch.setattr("app.worker.state.amp_history.run_amp", export)
    first = reader.query(HistoryQuery(session_id=AMP_SESSION, limit=1))
    assert len(first.items) == 1 and first.has_more
    rest = reader.query(HistoryQuery(session_id=AMP_SESSION, cursor=first.next_cursor))
    assert rest.items[0].id == f"{AMP_SESSION}:M-many:1" and rest.has_more
    assert len(rest.model_dump_json().encode()) < 262144


def test_amp_history_redacts_other_clients_access_tokens(monkeypatch) -> None:
    # External continuation can introduce a credential absent from this worker's environment.
    monkeypatch.delenv("AMP_API_KEY", raising=False)
    token = "sgamp_external-client_123456789"
    # Native tool payloads become JSON strings before entering the public history cache.
    payload = {f"line\n{token}": [f"line\t{token}", {"output": f"line\r{token}"}],
               "path": "task-abcdefghijklmnop", "count": 2, "ok": True}
    redacted = {"line\n[REDACTED]": ["line\t[REDACTED]", {"output": "line\r[REDACTED]"}],
                "path": "task-abcdefghijklmnop", "count": 2, "ok": True}
    message = {"role": "assistant", "protocolMessageID": "M-token", "content": [
        {"type": "text", "text": f"line\n{token}"},
        {"type": "tool_use", "id": "TU-token", "name": "shell", "input": payload},
        {"type": "tool_result", "toolUseID": "TU-token", "run": {"result": payload}},
    ]}

    async def export(*args, **kwargs):
        return AmpProcessResult(json.dumps({"id": AMP_SESSION, "messages": [message]}).encode(), b"", 0)

    monkeypatch.setattr("app.worker.state.amp_history.run_amp", export)
    reader = AmpHistory()
    for _ in range(2):
        page = reader.query(HistoryQuery(session_id=AMP_SESSION))
        assert len(page.items) == 3
        assert token not in page.model_dump_json()
        assert page.items[0].content.text == "line\n[REDACTED]"
        assert page.items[1].content.arguments is not None
        assert json.loads(page.items[1].content.arguments) == redacted
        assert json.loads(page.items[2].content.text) == redacted
