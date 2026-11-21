import base64
import json
from types import SimpleNamespace

import pytest

from app.contracts.conversation import ConversationFailure, HistoryQuery
from app.contracts.protocol import ExecutionStopped
from app.controler.workers.conversation import ConversationService
from app.controler.workflow.service import ControlService
from app.persistence.records import required
from app.worker.execution import codex
from tests.test_conversation_history import SESSION, codex_agent, message, rollout
from tests.workspace_fixtures import register_worker

OTHER = "019eac3b-a60c-7a93-a442-3718f660fc8d"


def test_single_session_query_validates_before_reading(tmp_path, monkeypatch):
    reader = codex_agent(tmp_path, monkeypatch)
    path = rollout(tmp_path, [message("user", "first"), message("assistant", "second")])
    query = HistoryQuery(session_id=SESSION, limit=1)
    first = reader.read_history(query)
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
        reader.read_history(second_query)


@pytest.mark.asyncio
async def test_control_owns_session_order_attribution_and_tail(store, completed_open_initial, tmp_path, monkeypatch):
    first_path = rollout(tmp_path, [message("user", "one")])
    second_path = first_path.with_name(first_path.name.replace(SESSION, OTHER))
    second_path.write_text(json.dumps({"type": "session_meta", "payload": {"id": OTHER}}) + "\n"
                           + json.dumps(message("assistant", "two")) + "\n")
    flow, *_ = completed_open_initial
    service = ControlService(database=store.database)
    service.cancel(flow)
    stop = required(store.stops.pending("worker-a"))
    assert store.stops.confirm("worker-a", ExecutionStopped(
        **stop.fence().model_dump(), execution_nonce="first-execution",
    ), "epoch")
    reopened = service.resume(flow, expected_attempt_id=flow.required_attempt_id)
    register_worker(store, "worker-b", repo=flow.repo, epoch="next")
    assert store.assignments.assign_job("worker-b", "new-assignment", "next")
    attempts = [store.flows.get_current_attempt(f) for f in (flow, reopened)]
    for attempt, session, generation in zip(attempts, (SESSION, OTHER), (0, 1), strict=True):
        attempt.codex_session_id, attempt.session_generation = session, generation
        store.flows.update_attempt(attempt)
    calls = []
    offline = set()
    reader = codex_agent(tmp_path, monkeypatch)

    async def query(worker_id, request):
        assert worker_id == ("worker-a" if request.session_id == SESSION else "worker-b")
        if worker_id in offline:
            raise ConversationFailure("worker_unavailable")
        calls.append(request)
        return reader.read_history(request)

    service = ConversationService(
        SimpleNamespace(query=query), database=store.database,
    )
    first = await service.read(flow.id, None, 100)
    assert len(calls) == 1 and first.has_more
    second = await service.read(flow.id, first.next_cursor, 100)
    assert len(calls) == 2 and not second.has_more
    assert len(second.sessions) == 1 and second.sessions[0].session_id == OTHER
    with second_path.open("a") as file:
        file.write(json.dumps(message("assistant", "tail")) + "\n")
    tail = await service.read(flow.id, second.next_cursor, 100)
    assert [i.content.text for i in tail.items] == ["tail"]
    assert len(tail.next_cursor) <= 2048
    other_flow = store.flows.create_flow(flow.repo, flow.issue_number + 1, flow.installation_id)
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        await service.read(other_flow.id, tail.next_cursor, 100)
    attempts[1].session_generation += 1
    store.flows.update_attempt(attempts[1])
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        await service.read(flow.id, tail.next_cursor, 100)
    # A disconnected former owner leaves a coverage gap, not a barrier to the live tail.
    offline.add("worker-a")
    gap = await service.read(flow.id, None, 100)
    assert not gap.items and gap.has_more and not gap.sessions[0].reached_end
    assert (gap.sessions[0].attempt_id, gap.sessions[0].status) == (attempts[0].id, "worker_unavailable")
    live = await service.read(flow.id, gap.next_cursor, 100)
    assert [i.content.text for i in live.items] == ["two", "tail"]
    offline.add("worker-b")
    with pytest.raises(ConversationFailure, match="worker_unavailable"):
        await service.read(flow.id, live.next_cursor, 100)
    offline.clear()
    first_path.unlink()
    missing = await service.read(flow.id, None, 100)
    assert missing.items == [] and missing.has_more
    assert missing.sessions[0].status == "not_found"
    resumed = await service.read(flow.id, missing.next_cursor, 100)
    assert resumed.items[0].session_id == OTHER
    with pytest.raises(ConversationFailure, match="cursor_invalid"):
        await service.read(flow.id, base64.urlsafe_b64encode(b'{"index":0}').decode(), 100)


def test_byte_budget_does_not_skip_record(tmp_path, monkeypatch):
    rollout(tmp_path, [message("user", "one"), message("assistant", "two")])
    reader = codex_agent(tmp_path, monkeypatch)
    all_items = reader.read_history(HistoryQuery(session_id=SESSION)).items
    monkeypatch.setattr(codex, "MAX_PAGE", max(len(i.model_dump_json().encode()) for i in all_items))
    first = reader.read_history(HistoryQuery(session_id=SESSION))
    second = reader.read_history(HistoryQuery(session_id=SESSION, cursor=first.next_cursor))
    assert first.has_more and first.items + second.items == all_items
