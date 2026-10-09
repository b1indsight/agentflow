import base64
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.contracts.conversation import ConversationFailure, SessionRef
from app.worker.state.history import CodexHistory
from app.worker.state.history_query import HistoryQuery
from app.worker.state.session import WorkerSession

SESSION = "019eac3b-a60c-7a93-a442-3718f660fc8c"
REF = SessionRef(attempt_id=1, session_id=SESSION, session_generation=0)


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
    first = reader.query(HistoryQuery(flow_id=42, sessions=[REF], cursor=None, limit=2).resolve())
    assert [(item.role, item.kind) for item in first.items] == [("user", "message"), ("assistant", "message")]
    assert first.has_more
    second = reader.query(HistoryQuery(
        flow_id=42, sessions=[REF], cursor=first.next_cursor.encode(), limit=100,
    ).resolve())
    assert [(item.role, item.call_id) for item in second.items] == [("assistant", "c1"), ("tool", "c1")]
    assert not second.has_more
    # Codex appends JSONL while a running session is being viewed: do not consume a partial line.
    tail = json.dumps(message("assistant", "Done"))
    with path.open("a") as file:
        file.write(tail[:20])
    partial = reader.query(HistoryQuery(
        flow_id=42, sessions=[REF], cursor=second.next_cursor.encode(), limit=100,
    ).resolve())
    assert partial.items == []
    with path.open("a") as file:
        file.write(tail[20:] + "\n")
    final = reader.query(HistoryQuery(
        flow_id=42, sessions=[REF], cursor=partial.next_cursor.encode(), limit=100,
    ).resolve())
    assert final.items[0].content.text == "Done"
    assert final.items[0].id not in {item.id for item in first.items + second.items}


def test_history_missing_and_cursor_identity(tmp_path: Path) -> None:
    reader = CodexHistory(tmp_path)
    assert reader.query(HistoryQuery(
        flow_id=42, sessions=[REF], cursor=None, limit=10,
    ).resolve()).sessions[0].status == "not_found"
    rollout(tmp_path, [message("user", "hello")])
    page = reader.query(HistoryQuery(flow_id=42, sessions=[REF], cursor=None, limit=10).resolve())
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        reader.query(HistoryQuery(
            flow_id=43, sessions=[REF], cursor=page.next_cursor.encode(), limit=10,
        ).resolve())
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        reader.query(HistoryQuery(
            flow_id=42, sessions=[REF.model_copy(update={"session_generation": 1})], cursor=page.next_cursor.encode(), limit=10,
        ).resolve())


def test_query_preserves_legacy_cursor_and_does_not_mutate_spec(tmp_path: Path, monkeypatch) -> None:
    # A client may resume a pre-upgrade cursor; resolving a spec must also isolate
    # it from later changes to caller-owned session references and reader progress.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = rollout(tmp_path, [message("user", "first"), message("assistant", "second")])
    rows = path.read_bytes().splitlines(keepends=True)
    info = path.stat()
    ref = REF.model_copy()
    fingerprint = hashlib.sha256(json.dumps([42, [ref.model_dump()]], sort_keys=True).encode()).hexdigest()
    legacy = base64.urlsafe_b64encode(json.dumps({
        "fingerprint": fingerprint, "index": 0, "offset": len(rows[0]) + len(rows[1]),
        "identity": f"{info.st_dev}:{info.st_ino}:{hashlib.sha256(rows[0]).hexdigest()}",
    }).encode()).decode()
    spec = HistoryQuery(flow_id=42, sessions=[ref], cursor=legacy, limit=1).resolve()
    before = spec.model_dump_json()
    ref.attempt_id = 99
    result = CodexHistory(tmp_path).query(spec)
    assert [item.content.text for item in result.items] == ["second"]
    assert result.items[0].attempt_id == 1
    assert spec.model_dump_json() == before
    with pytest.raises(ValidationError):
        spec.position.offset = 0
    with pytest.raises(ValidationError):
        spec.sessions[0].attempt_id = 99
    page = WorkerSession(tmp_path).read(42, [REF], legacy, 1)
    assert page.flow_id == 42 and page.items == result.items
    assert page.next_cursor == result.next_cursor.encode()


@pytest.mark.parametrize("cursor", ["not-base64", "e30=", "W10="])
def test_query_rejects_malformed_cursor_before_reading(cursor: str) -> None:
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        HistoryQuery(flow_id=42, sessions=[REF], cursor=cursor, limit=10).resolve()


def test_history_rejects_replaced_file_with_same_length(tmp_path: Path) -> None:
    # A valid query cannot detect file replacement: that must be checked at open.
    path = rollout(tmp_path, [message("user", "before")])
    reader = CodexHistory(tmp_path)
    page = reader.query(HistoryQuery(flow_id=42, sessions=[REF], limit=10).resolve())
    spec = HistoryQuery(flow_id=42, sessions=[REF], cursor=page.next_cursor.encode()).resolve()
    path.rename(path.with_suffix(".old"))
    rollout(tmp_path, [message("user", "after!")])
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        reader.query(spec)


def test_history_rejects_symlink_escape_and_wrong_session(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    path = rollout(outside, [message("user", "other account")])
    root = tmp_path / "codex"
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / path.name).symlink_to(path)
    # A rollout-looking symlink must never expose files outside the worker's history root.
    with pytest.raises(ConversationFailure, match="history_unreadable"):
        CodexHistory(root).query(HistoryQuery(flow_id=42, sessions=[REF], cursor=None, limit=10).resolve())
    path = rollout(tmp_path / "wrong", [])
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "other-session"}}) + "\n")
    with pytest.raises(ConversationFailure, match="history_unreadable"):
        CodexHistory(tmp_path / "wrong").query(HistoryQuery(
            flow_id=42, sessions=[REF], cursor=None, limit=10,
        ).resolve())


def test_history_reports_unknown_records_and_truncates_text(tmp_path: Path) -> None:
    rollout(tmp_path, [message("user", "x" * 20000),
                       {"type": "response_item", "payload": {"type": "future_item"}}])
    page = CodexHistory(tmp_path).query(HistoryQuery(
        flow_id=42, sessions=[REF], cursor=None, limit=100,
    ).resolve())
    assert page.items[0].truncated
    assert len(page.items[0].content.text) < 20000
    assert page.sessions[0].status == "unsupported"


def test_history_multiple_sessions_and_archive(tmp_path: Path) -> None:
    first = rollout(tmp_path, [message("user", "initial")])
    archived = tmp_path / "archived_sessions" / first.name
    archived.parent.mkdir()
    first.rename(archived)
    other = REF.model_copy(update={"attempt_id": 2, "session_id": "019eac3b-a60c-7a93-a442-3718f660fc8d"})
    second = first.with_name(first.name.replace(SESSION, other.session_id))
    second.write_text(json.dumps({"type": "session_meta", "payload": {"id": other.session_id}}) + "\n"
                      + json.dumps(message("user", "retry")) + "\n")
    reader = CodexHistory(tmp_path)
    page = reader.query(HistoryQuery(flow_id=42, sessions=[REF, other], cursor=None, limit=1).resolve())
    assert page.items[0].attempt_id == 1
    page = reader.query(HistoryQuery(
        flow_id=42, sessions=[REF, other], cursor=page.next_cursor.encode(), limit=1,
    ).resolve())
    assert page.items[0].attempt_id == 2
    assert not page.has_more
    second.write_text("")
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        reader.query(HistoryQuery(
            flow_id=42, sessions=[REF, other], cursor=page.next_cursor.encode(), limit=1,
        ).resolve())


def test_history_malformed_limits_and_secret_filter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "private-key-value")
    path = rollout(tmp_path, [message("assistant", "private-key-value Bearer abc123 ghp_testsecret")])
    reader = CodexHistory(tmp_path)
    text = reader.query(HistoryQuery(
        flow_id=42, sessions=[REF], cursor=None, limit=100,
    ).resolve()).items[0].content.text
    assert "private-key-value" not in text and "abc123" not in text and "ghp_testsecret" not in text
    with path.open("a") as file:
        file.write("invalid-json\n")
    with pytest.raises(ConversationFailure, match="history_unreadable"):
        reader.query(HistoryQuery(flow_id=42, sessions=[REF], cursor=None, limit=100).resolve())
    rollout(tmp_path, [message("user", "x" * 300000)])
    with pytest.raises(ConversationFailure, match="history_limit"):
        reader.query(HistoryQuery(flow_id=42, sessions=[REF], cursor=None, limit=100).resolve())


def test_history_scan_budget_returns_continuation(tmp_path: Path, monkeypatch) -> None:
    from app.worker.state import history

    monkeypatch.setattr(history, "MAX_SCAN", 1)
    rollout(tmp_path, [{"type": "turn_context", "payload": {}}, message("user", "later")])
    reader = CodexHistory(tmp_path)
    page = reader.query(HistoryQuery(flow_id=42, sessions=[REF], cursor=None, limit=100).resolve())
    assert page.items == [] and page.has_more
    page = reader.query(HistoryQuery(
        flow_id=42, sessions=[REF], cursor=page.next_cursor.encode(), limit=100,
    ).resolve())
    assert page.items[0].content.text == "later"


def test_history_page_size_keeps_unreturned_record_for_next_query(tmp_path: Path, monkeypatch) -> None:
    from app.worker.state import history

    # A byte-limited page must leave its cursor before the first excluded record.
    rollout(tmp_path, [message("user", "first"), message("assistant", "second")])
    reader = CodexHistory(tmp_path)
    spec = HistoryQuery(flow_id=42, sessions=[REF], limit=100).resolve()
    complete = reader.query(spec)
    budget = max(len(item.model_dump_json().encode()) for item in complete.items)
    monkeypatch.setattr(history, "MAX_PAGE", budget)
    first = reader.query(spec)
    assert first.items == complete.items[:1] and first.has_more
    second = reader.query(HistoryQuery(
        flow_id=42, sessions=[REF], cursor=first.next_cursor.encode(), limit=100,
    ).resolve())
    assert second.items == complete.items[1:] and not second.has_more


def test_history_refuses_special_files_and_racing_directory_symlink(tmp_path: Path) -> None:
    import os

    root = tmp_path / "codex"
    path = rollout(root, [])
    path.unlink()
    os.mkfifo(path)
    reader = CodexHistory(root)
    with pytest.raises(ConversationFailure, match="history_unreadable"):
        reader.query(HistoryQuery(flow_id=42, sessions=[REF], cursor=None, limit=100).resolve())
    # Exercise the open boundary independently: a directory can be swapped after discovery.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_text("private")
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        reader._open(root / "link" / "data")
