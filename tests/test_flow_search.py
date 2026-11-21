"""Worker searches are fenced reads over the same PostgreSQL board as the Web console."""

from threading import get_ident
from unittest.mock import AsyncMock

import pytest

from app.communication.dispatch import Dispatcher
from app.contracts.console import ConsoleFailure
from app.contracts.protocol import (
    ExecutionIdentity,
    FlowSearchCall,
    FlowSearchQuery,
    FlowSearchResult,
    call_header,
)
from app.controler.workers.errors import ProtocolViolation
from app.domain.models import JobKind
from app.persistence.console import ConsoleStore, PageCursor
from tests.flow_fixtures import enqueue_job
from tests.wire import worker_message
from tests.worker_fixtures import server_callbacks


def test_repository_scope_covers_rows_counts_watermark_and_cursor(store):
    # Matching terms and a newer out-of-scope ID catch leakage in all three SQL reads.
    ids = []
    for issue in (1, 2):
        for repo in ("owner/repo", "other/repo"):
            flow = store.flows.create_flow(repo, issue, 1)
            with store.connection() as conn:
                conn.execute("UPDATE console_flows SET title='Same title' WHERE flow_id=%s", (flow.id,))
            if repo == "owner/repo":
                ids.append(flow.id)
    console = ConsoleStore(store.database)
    for q in ("", 'status:queued "Same title"'):
        first = console.flows(repo_scope="owner/repo", q=q, limit=1)
        assert first.counts == {"queued": 2, "running": 0, "blocked": 0, "review": 0, "archived": 0}
        assert first.has_more and first.next_cursor
        assert PageCursor.decode(first.next_cursor).ceiling == ids[1]
        last = console.flows(repo_scope="owner/repo", q=q, limit=1, cursor=first.next_cursor)
        assert last.counts == first.counts and not last.has_more and last.next_cursor is None
        assert [item.id for item in first.items + last.items] == ids[::-1]
        for scope in ("other/repo", None):
            with pytest.raises(ConsoleFailure, match="cursor_invalid"):
                console.flows(repo_scope=scope, q=q, cursor=first.next_cursor)
    empty = console.flows(repo_scope="owner/repo", q="repo:other/repo")
    assert not empty.items and not any(empty.counts.values()) and not empty.has_more
    assert console.flows().counts["queued"] == 4


def search_route(store, runtime, job_id, *, epoch="epoch", token="token"):
    callbacks = server_callbacks(runtime, "worker-a", epoch)
    callbacks.current[job_id] = store.jobs.get_job(job_id), token, None
    routes = Dispatcher()
    callbacks.bind(routes)
    routes.freeze()
    send = AsyncMock()
    callbacks.session.send = send

    async def call(q="", *, assignment_token=token):
        item = FlowSearchCall(**call_header(), method="server.search_flows",
                              arguments=ExecutionIdentity(job_id=job_id, assignment_token=assignment_token),
                              query=FlowSearchQuery(q=q))
        await routes.dispatch(item, worker_message(item, epoch=epoch))
        reply, = send.await_args.args[0].payload
        assert isinstance(reply, FlowSearchResult) and reply.reply_to == item.message_id
        return reply

    return call


async def test_control_search_derives_scope_and_rejects_expired_authority(
    store, prepared_initial, server_runtime, monkeypatch,
):
    flow, _, _, request, _ = prepared_initial
    job_id = request.arguments.job_id
    store.flows.create_flow("other/repo", flow.issue_number, 1)
    runtime = server_runtime()
    search = runtime.tools.search
    loop_thread = get_ident()

    def checked_search(*args, **kwargs):
        # A slow board scan must not delay heartbeats on unrelated worker connections.
        assert get_ident() != loop_thread
        return search(*args, **kwargs)

    monkeypatch.setattr(runtime.tools, "search", checked_search)
    call = search_route(store, runtime, job_id)
    page = (await call()).page
    assert [row.id for row in page.items] == [flow.id] and sum(page.counts.values()) == 1
    assert (await call("bad:qualifier")).error == "search_invalid"
    with pytest.raises(ProtocolViolation):
        await call(assignment_token="wrong")
    # Even a matching local assignment cannot authorize a superseded database epoch.
    stale = await search_route(store, runtime, job_id, epoch="superseded")()
    assert stale.error and stale.page is None
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.stops.request(flow.id)
    assert store.stops.has_pending(flow.id)
    stopped = await call()
    assert stopped.error and stopped.page is None


async def test_authorization_and_page_share_a_database_snapshot(store, prepared_initial, server_runtime, monkeypatch):
    # A page admitted before revocation must not disclose flows published after revocation.
    flow, _, _, request, _ = prepared_initial
    call = search_route(store, server_runtime(), request.arguments.job_id)
    read = ConsoleStore.flows

    def publish_stop_then_read(console, **query):
        with store.transaction() as writer:
            writer.lock_workflow(flow.id)
            writer.stops.request(flow.id)
            writer.flows.create_flow(flow.repo, 99, 1)
        return read(console, **query)

    monkeypatch.setattr(ConsoleStore, "flows", publish_stop_then_read)
    page = (await call()).page
    assert [row.id for row in page.items] == [flow.id] and sum(page.counts.values()) == 1
    assert (await call()).page is None


async def test_active_revision_cannot_search(store, completed_open_initial, server_runtime):
    flow, _, _, _, _ = completed_open_initial
    job = enqueue_job(store, flow.id, JobKind.REVISION)
    assert store.assignments.assign_job("worker-a", "revision", "epoch").id == job.id
    assert store.assignments.start_assignment(job.id, job.attempt_id, "worker-a", "revision", "epoch")
    denied = await search_route(store, server_runtime(), job.id, token="revision")()
    assert denied.error and denied.page is None
