"""Durable source recovery and active Worker history ingestion on real PostgreSQL."""

from unittest.mock import AsyncMock, Mock

from app.contracts.conversation import ConversationContent, ConversationItem, ConversationPage
from app.controler.console.sync import ConsoleSynchronizer
from app.controler.workers.conversation import ConversationService
from app.github import GitHubApp
from app.persistence.activity import ActivityStore
from app.persistence.console import ConsoleStore
from app.persistence.console_sync import ConsoleSyncStore


def only_worker(database):
    with database.connection() as connection:
        connection.execute("UPDATE console_sync SET pending=(source='worker')")


async def test_worker_is_actively_queried_and_eof_checkpoint_survives_refresh(store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    only_worker(store._db)
    conversations = Mock(spec=ConversationService)
    item = ConversationItem(
        id="record-1",
        attempt_id=1,
        session_id="12345678-1234-1234-1234-123456789abc",
        session_generation=0,
        timestamp=flow.created_at.isoformat(),
        role="assistant",
        kind="message",
        content=ConversationContent(text="worker display body"),
    )
    tool = item.model_copy(
        update={
            "id": "record-2",
            "kind": "tool_call",
            "content": ConversationContent(name="shell", arguments="x" * 3000),
        }
    )
    conversations.read = AsyncMock(
        return_value=ConversationPage(
            flow_id=flow.id, items=[item, tool], next_cursor="eof-checkpoint"
        )
    )
    sync = ConsoleSynchronizer(Mock(spec=GitHubApp), conversations, database=store._db)
    claim = sync.store.claim()
    assert claim and claim.source == "worker"
    await sync.sync(claim)
    conversations.read.assert_awaited_once_with(flow.id, None, 50)
    archive = ActivityStore(store._db)
    first = archive.page(flow.id)
    assert any(i.body == "worker display body" for i in first.items)
    summary = next(i for i in first.items if i.kind == "tool_call")
    assert summary.body == "x" * 2000
    assert summary.metadata["summary_truncated"] and summary.metadata["query"] == "conversation"
    ConsoleStore(store._db).request_sync(flow.id)
    only_worker(store._db)
    again = sync.store.claim()
    assert again and again.cursor == "eof-checkpoint"
    await sync.sync(again)
    assert archive.page(flow.id).snapshot == first.snapshot


def test_stale_claim_cannot_publish_and_rate_limit_pauses_installation_not_worker(store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    sync = ConsoleSyncStore(store._db)
    first = sync.claim()
    assert first and first.source == "github"
    with store._db.connection() as connection:
        connection.execute(
            "UPDATE console_sync SET claim_until=now()-interval '1 second'"
            " WHERE flow_id=%s AND source='github'",
            (flow.id,),
        )
    replacement = sync.claim()
    assert replacement and replacement.claim_token != first.claim_token
    assert not sync.finish(first, [], cursor=None, more=False, limitations=[])
    sync.fail(replacement, "rate_limited", delay=600)
    store.flows.create_flow("owner/other", 8, 42)
    worker = sync.claim()
    assert worker and worker.source == "worker"
    sync.finish(worker, [], cursor=None, more=False, limitations=[])
    worker = sync.claim()
    assert worker and worker.source == "worker"
    sync.finish(worker, [], cursor=None, more=False, limitations=[])
    assert sync.claim() is None


def test_uncommitted_publication_is_not_a_reading_snapshot(store):
    from app.github.console import Observation

    flow = store.flows.create_flow("owner/repo", 7, 42)
    archive = ActivityStore(store._db)
    before = archive.page(flow.id)
    item = Observation(
        key="external:1",
        source="issue.comments",
        kind="comment",
        occurred_at=flow.created_at,
        updated_at=flow.created_at,
        author="owner",
        body="committed later",
    )
    # Readers use another pooled connection while this publication remains uncommitted.
    with store._db.transaction() as tx:
        ActivityStore(tx).append(flow.id, item)
        during = archive.page(flow.id)
        assert during.snapshot == before.snapshot
    assert archive.page(flow.id, snapshot=during.snapshot).total == before.total
    assert archive.page(flow.id).total == before.total + 1
