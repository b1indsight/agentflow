import asyncio
import json

import httpx
import pytest

from app.controler.api import create_app
from app.persistence.records import required
from app.worker.execution.amp_process import AmpProcessResult
from tests.access import AccessIssuer
from tests.test_conversation_history import SESSION, message, rollout
from tests.test_remote_e2e import remote_connection
from tests.test_remote_e2e import remote_flow as remote_flow  # noqa: PLC0414 -- fixture re-export
from tests.test_workspace_dispatch import wait_until

OTHER_SESSION = "019eac3b-a60c-7a93-a442-3718f660fc8d"


@pytest.mark.parametrize("backend,phase", [
    (backend, phase) for backend in ("codex", "amp")
    for phase in ("worker", "preparation", "tool", "result")
] + [("amp", "slow_export")])
async def test_http_history_queues_behind_callbacks_but_not_execution(
    store, remote_flow, tmp_path, phase, monkeypatch, backend,
) -> None:
    flow, runtime, _github, worker, agent = remote_flow
    job, = store.jobs.list_jobs(flow.id)
    started, release = asyncio.Event(), asyncio.Event()
    blocked_phase = "worker" if phase == "slow_export" else phase

    def pause(target, method, selected):
        original = getattr(target, method)

        async def invoke(*args, **kwargs):
            if blocked_phase == selected:
                started.set()
                await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(target, method, invoke)

    pause(runtime.coordinator, "prepare_for_assignment", "preparation")
    pause(runtime.tools, "execute", "tool")
    pause(runtime.results, "_apply", "result")
    pause(agent, "start", "worker")
    path = rollout(tmp_path / "codex", [message("user", "PR feedback"), message("assistant", "Fixed")])
    path.with_name(path.name.replace(SESSION, OTHER_SESSION)).write_text(
        json.dumps({"type": "session_meta", "payload": {"id": OTHER_SESSION}}) + "\n"
        + json.dumps(message("assistant", "Follow-up")) + "\n"
    )
    session = ("T-" if backend == "amp" else "") + SESSION
    other_session = ("T-" if backend == "amp" else "") + OTHER_SESSION
    # Real persisted metadata keeps the history cursor stable while execution moves
    # through preparation, tool dispatch and terminal application.
    attempt = store.flows.create_attempt(flow.id)
    attempt.codex_session_id = session
    store.flows.update_attempt(attempt)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    continuation = store.flows.create_attempt(flow.id)
    continuation.codex_session_id, continuation.session_generation = other_session, 1
    store.flows.update_attempt(continuation)
    finish = agent.finish

    async def finish_session(context):
        result = await finish(context)
        result.session_id = session
        return result

    monkeypatch.setattr(agent, "finish", finish_session)
    if backend == "amp":
        async def export(args, **kwargs):
            if phase == "slow_export" and args[2] == session:
                # Scale time 1:10: a valid 15s export must outlast the old 10s broker budget.
                await asyncio.sleep(1.5)
            entries = ([("user", "PR feedback"), ("assistant", "Fixed")]
                       if args[2] == session else [("assistant", "Follow-up")])
            messages = [{"role": role, "protocolMessageID": f"M-{index}",
                         "content": [{"type": "text", "text": text}]}
                        for index, (role, text) in enumerate(entries)]
            return AmpProcessResult(json.dumps({"id": args[2], "messages": messages}).encode(), b"", 0)

        monkeypatch.setattr("app.worker.state.amp_history.run_amp", export)
    broker = runtime.conversations.broker
    broker.timeout = broker.timeout / 10 if phase == "slow_export" else 1
    issuer = AccessIssuer()
    await runtime.access.close()
    monkeypatch.setattr(runtime, "access", issuer.auth)
    app = create_app(runtime)
    app.state.runtime = runtime
    endpoint = f"/api/flows/{flow.id}/conversation"
    try:
        async with remote_connection(runtime, worker), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=issuer.headers(),
        ) as http:
            await asyncio.wait_for(started.wait(), 10)
            # Execution and preparation run independently; serial callbacks still hold replies.
            if phase in {"tool", "result"}:
                broker.timeout = 0.02
                blocked = await http.get(endpoint, params={"limit": 1})
                assert blocked.status_code == 504, blocked.text
                assert not release.is_set()
                release.set()
                await wait_until(lambda: required(store.jobs.get_job(job.id)).result_applied_at is not None)
                broker.timeout = 1
            first = await http.get(endpoint, params={"limit": 1})
            assert first.status_code == 200, first.text
            if phase == "preparation":
                assert not release.is_set()
            assert first.json()["items"][0]["role"] == "user"
            assert first.json()["sessions"][0]["starts_at_beginning"]
            assert not first.json()["sessions"][0]["reached_end"]
            release.set()
            await wait_until(lambda: required(store.jobs.get_job(job.id)).result_applied_at is not None)
            second = await http.get(endpoint, params={"cursor": first.json()["next_cursor"]})
            assert second.status_code == 200, second.text
            assert second.json()["items"][0]["content"]["text"] == "Fixed"
            assert second.json()["has_more"]
            # Flow continuation points to another session; this session is complete.
            assert not second.json()["sessions"][0]["starts_at_beginning"]
            assert second.json()["sessions"][0]["reached_end"]
            third = await http.get(endpoint, params={"cursor": second.json()["next_cursor"]})
            assert third.status_code == 200, third.text
            item = third.json()["items"][0]
            assert (item["attempt_id"], item["session_id"], item["session_generation"]) == (
                continuation.id, other_session, 1,
            )
            assert item["content"]["text"] == "Follow-up"
            assert not third.json()["has_more"]
            assert third.json()["sessions"][0]["starts_at_beginning"]
            assert third.json()["sessions"][0]["reached_end"]
            assert (await http.get(f"/api/flows/{flow.id + 1}/conversation")).status_code == 404
            assert not broker._inflight
            assert worker.executions[flow.id].state.load().terminal.result.session_id == session
    finally:
        release.set()
        await issuer.auth.close()
