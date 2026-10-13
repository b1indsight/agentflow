import base64
import json
from types import SimpleNamespace

import pytest

from app.contracts.conversation import ConversationFailure, HistoryQuery
from app.controler.workers.conversation import ConversationService
from app.domain.models import Attempt
from app.worker.state import history
from app.worker.state.history import CodexHistory
from tests.test_conversation_history import SESSION, message, rollout

OTHER = "019eac3b-a60c-7a93-a442-3718f660fc8d"


def test_single_session_query_validates_before_reading(tmp_path):
    reader = CodexHistory(tmp_path)
    path = rollout(tmp_path, [message("user", "first"), message("assistant", "second")])
    query = HistoryQuery(session_id=SESSION, limit=1)
    first = reader.query(query)
    assert "attempt_id" not in first.items[0].model_dump()
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        HistoryQuery(session_id=OTHER, cursor=first.next_cursor)
    for cursor in ("bad", "e30=", base64.urlsafe_b64encode(json.dumps([SESSION, True, "file"]).encode()).decode()):
        with pytest.raises(ConversationFailure, match="cursor_invalid"):
            HistoryQuery(session_id=SESSION, cursor=cursor)
    second_query = HistoryQuery(session_id=SESSION, cursor=first.next_cursor)
    # Replacing a same-length file must be rejected even after query validation.
    path.rename(path.with_suffix(".old"))
    rollout(tmp_path, [message("user", "first"), message("assistant", "second")])
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        reader.query(second_query)


@pytest.mark.asyncio
async def test_control_owns_session_order_attribution_and_tail(tmp_path):
    first_path = rollout(tmp_path, [message("user", "one")])
    second_path = first_path.with_name(first_path.name.replace(SESSION, OTHER))
    second_path.write_text(json.dumps({"type": "session_meta", "payload": {"id": OTHER}}) + "\n"
                           + json.dumps(message("assistant", "two")) + "\n")
    attempts = [Attempt(id=7, flow_id=42, codex_session_id=SESSION),
                Attempt(id=8, flow_id=42, codex_session_id=OTHER, session_generation=1)]
    store = SimpleNamespace(get_flow=lambda _: object(),
                            get_flow_execution=lambda _: SimpleNamespace(worker_id="worker"),
                            list_attempts=lambda _: attempts)
    calls = []
    reader = CodexHistory(tmp_path)

    async def query(worker_id, request):
        calls.append(request)
        return reader.query(request)

    service = ConversationService(store, SimpleNamespace(query=query))
    first = await service.read(42, None, 100)
    assert len(calls) == 1 and first.has_more
    second = await service.read(42, first.next_cursor, 100)
    assert len(calls) == 2 and not second.has_more
    assert len(second.sessions) == 1 and second.sessions[0].session_id == OTHER
    with second_path.open("a") as file:
        file.write(json.dumps(message("assistant", "tail")) + "\n")
    tail = await service.read(42, second.next_cursor, 100)
    assert [i.content.text for i in tail.items] == ["tail"]
    assert len(tail.next_cursor) <= 2048
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        await service.read(43, tail.next_cursor, 100)
    attempts[1].session_generation += 1
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        await service.read(42, tail.next_cursor, 100)
    first_path.unlink()
    missing = await service.read(42, None, 100)
    assert missing.items == [] and missing.has_more
    assert missing.sessions[0].status == "not_found"
    resumed = await service.read(42, missing.next_cursor, 100)
    assert resumed.items[0].session_id == OTHER
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        await service.read(42, base64.urlsafe_b64encode(b'{"index":0}').decode(), 100)


def test_byte_budget_does_not_skip_record(tmp_path, monkeypatch):
    rollout(tmp_path, [message("user", "one"), message("assistant", "two")])
    reader = CodexHistory(tmp_path)
    all_items = reader.query(HistoryQuery(session_id=SESSION)).items
    monkeypatch.setattr(history, "MAX_PAGE", max(len(i.model_dump_json().encode()) for i in all_items))
    first = reader.query(HistoryQuery(session_id=SESSION))
    second = reader.query(HistoryQuery(session_id=SESSION, cursor=first.next_cursor))
    assert first.has_more and first.items + second.items == all_items
