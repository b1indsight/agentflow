import json
from pathlib import Path

import pytest

from app.contracts.conversation import ConversationFailure, HistoryQuery
from app.worker.state.history import CodexHistory

SESSION = "019eac3b-a60c-7a93-a442-3718f660fc8c"


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


def test_history_missing_and_cursor_identity(tmp_path: Path) -> None:
    reader = CodexHistory(tmp_path)
    assert reader.query(HistoryQuery(session_id=SESSION, cursor=None, limit=10)).status == "not_found"
    rollout(tmp_path, [message("user", "hello")])
    page = reader.query(HistoryQuery(session_id=SESSION, cursor=None, limit=10))
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        HistoryQuery(session_id="019eac3b-a60c-7a93-a442-3718f660fc8d", cursor=page.next_cursor)


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
    from app.worker.state import history

    monkeypatch.setattr(history, "MAX_SCAN", 1)
    rollout(tmp_path, [{"type": "turn_context", "payload": {}}, message("user", "later")])
    reader = CodexHistory(tmp_path)
    page = reader.query(HistoryQuery(session_id=SESSION, cursor=None, limit=100))
    assert page.items == [] and page.has_more
    page = reader.query(HistoryQuery(session_id=SESSION, cursor=page.next_cursor, limit=100))
    assert page.items[0].content.text == "later"


def test_history_refuses_special_files_and_racing_directory_symlink(tmp_path: Path) -> None:
    import os

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
