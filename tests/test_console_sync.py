"""Durable source recovery and active Worker history ingestion on real PostgreSQL."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Event
from time import monotonic
from typing import Literal
from unittest.mock import AsyncMock, Mock

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.contracts.console import SourceStatus
from app.contracts.conversation import (
    ConversationContent,
    ConversationItem,
    ConversationPage,
    SessionHistory,
)
from app.contracts.protocol import PROTOCOL_VERSION
from app.controler.console.sync import ConsoleSynchronizer
from app.controler.workers.conversation import ConversationService
from app.domain.models import JobKind
from app.github import GitHubApp
from app.persistence.activity import ActivityStore
from app.persistence.console import ConsoleStore
from app.persistence.console_sync import ConsoleSyncStore, SyncClaim
from app.persistence.store import Store


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
        # A binary/tool-derived character must not poison this page's checkpoint.
        content=ConversationContent(text="worker display body\0"),
    )
    tool = item.model_copy(
        update={
            "id": "record-2",
            "kind": "tool_call",
            "content": ConversationContent(name="shell\0", arguments="x" * 3000),
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
    message = next(i for i in first.items if i.source == "worker" and i.kind == "message")
    assert message.body == "worker display body\\u0000"
    assert message.metadata["unsupported_characters_escaped"] is True
    summary = next(i for i in first.items if i.kind == "tool_call")
    assert summary.title == "shell\\u0000"
    assert summary.body == "x" * 2000
    assert summary.metadata["summary_truncated"] and summary.metadata["query"] == "conversation"
    ConsoleStore(store._db).request_sync(flow.id)
    only_worker(store._db)
    again = sync.store.claim()
    assert again and again.cursor == "eof-checkpoint"
    await sync.sync(again)
    assert archive.page(flow.id).snapshot == first.snapshot


def test_reconnect_and_publication_use_the_same_lock_order(store):
    # Reconnecting a bound worker enqueues sources before refreshing its execution.
    # Hold publication first, then finish while the reconnect waits on that lock.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    store.workers.worker_connected("w", PROTOCOL_VERSION, "old")
    store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("w", "token", "old") is not None
    only_worker(store._db)
    sync = ConsoleSyncStore(store._db)
    claim = sync.claim()
    assert claim is not None
    backend: Queue[int] = Queue()

    def reconnect():
        with store.transaction() as tx, tx.connection() as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            backend.put(connection.info.backend_pid)
            tx.workers.worker_connected("w", PROTOCOL_VERSION, "new")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store._db.transaction() as tx, tx.connection() as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            connection.execute(
                "SELECT flow_id FROM console_flows WHERE flow_id=%s FOR UPDATE", (flow.id,)
            )
            pending = pool.submit(reconnect)
            pid = backend.get(timeout=5)
            deadline = monotonic() + 3
            with store.connection() as observer:
                while True:
                    blocked = observer.execute(
                        "SELECT %s=ANY(pg_blocking_pids(%s)) AS blocked",
                        (connection.info.backend_pid, pid),
                    ).fetchone()
                    if blocked["blocked"]:
                        break
                    assert not pending.done()
                    assert monotonic() < deadline, "reconnect did not wait on publication"
                    Event().wait(0.01)
            assert ConsoleSyncStore(tx).finish(claim, [], cursor="eof", more=False, limitations=[])
        pending.result(timeout=5)
    assert store.workers.list_workers()[0].connection_epoch == "new"


def test_coverage_limitations_survive_clean_pages_and_incremental_refresh(store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    only_worker(store._db)
    with store.connection() as connection:
        connection.execute(
            "UPDATE console_sync SET limitations=%s WHERE source='worker'",
            (Jsonb(["not_found", "worker_source_truncated"]),),
        )
    sync = ConsoleSyncStore(store._db)
    first = sync.claim()
    assert first is not None
    sync.finish(first, [], cursor="page-2", more=True, limitations=["worker_source_truncated"])
    with store.connection() as connection:
        connection.execute("UPDATE console_sync SET next_at=now()")
    second = sync.claim()
    assert second is not None and second.cursor == "page-2"
    sync.finish(second, [], cursor="eof", more=False, limitations=[])
    ConsoleStore(store._db).request_sync(flow.id)
    only_worker(store._db)
    third = sync.claim()
    assert third is not None and third.cursor == "eof"
    sync.finish(third, [], cursor="eof", more=False, limitations=[])
    worker = next(s for s in ConsoleStore(store._db).sources(flow.id) if s.source == "worker")
    assert worker.status == "partial"
    assert worker.limitations == ["worker_source_truncated"]


@pytest.mark.parametrize("disconnect", [False, True])
def test_worker_freshness_and_current_error_are_separate_from_session_coverage(
    store: Store, monkeypatch: pytest.MonkeyPatch, disconnect: bool
) -> None:
    # Incremental reads leave old sessions untouched; their coverage must not hide
    # a newer successful read or the current connection failure.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    now = flow.created_at + timedelta(hours=1)
    monkeypatch.setattr(store._db, "_clock", lambda: now)
    only_worker(store._db)
    sync = ConsoleSyncStore(store._db)
    first_session = SessionHistory(
        attempt_id=1,
        session_id="12345678-1234-1234-1234-123456789abc",
        session_generation=0,
        status="available",
        starts_at_beginning=True,
        reached_end=True,
    )
    first = sync.claim()
    assert first is not None and first.source == "worker"
    assert sync.finish(
        first, [], cursor="second", more=True, limitations=[], sessions=[first_session]
    )

    now += timedelta(minutes=1)
    second = sync.claim()
    assert second is not None and second.cursor == "second"
    assert sync.finish(
        second,
        [],
        cursor="eof",
        more=False,
        limitations=[],
        sessions=[
            SessionHistory(
                attempt_id=2,
                session_id="12345678-1234-1234-1234-123456789def",
                session_generation=0,
                status="unsupported",
                starts_at_beginning=True,
                reached_end=True,
            )
        ],
    )
    console = ConsoleStore(store._db)
    if disconnect:
        console.request_sync(flow.id)
        only_worker(store._db)
        claim = sync.claim()
        assert claim is not None and claim.source == "worker"
        sync.fail(claim, "worker_unavailable", delay=None)

    source = next(source for source in console.sources(flow.id) if source.source == "worker")
    assert source.status == ("unavailable" if disconnect else "partial")
    assert source.reason == ("worker_unavailable" if disconnect else "unsupported")
    assert source.last_success_at == now


@pytest.mark.parametrize("reason", ["no_worker", "no_session"])
async def test_worker_availability_recovers_without_a_permanent_coverage_gap(
    store: Store, reason: Literal["no_worker", "no_session"]
) -> None:
    # Startup without an assignment/session is temporary, unlike truncated history.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    only_worker(store._db)
    conversations = Mock(spec=ConversationService)
    conversations.read = AsyncMock(
        side_effect=[
            ConversationPage(flow_id=flow.id, unavailable_reason=reason),
            ConversationPage(flow_id=flow.id, next_cursor="eof"),
        ]
    )
    sync = ConsoleSynchronizer(Mock(), conversations, database=store._db)
    claim = sync.store.claim()
    assert claim is not None
    await sync.sync(claim)
    before = next(s for s in ConsoleStore(store._db).sources(flow.id) if s.source == "worker")
    assert before.status == "unavailable" and before.reason == reason
    assert before.limitations == [] and before.last_success_at is None
    ConsoleStore(store._db).request_sync(flow.id)
    only_worker(store._db)
    claim = sync.store.claim()
    assert claim is not None
    await sync.sync(claim)
    after = next(s for s in ConsoleStore(store._db).sources(flow.id) if s.source == "worker")
    assert after.status == "available" and after.reason is None
    assert after.limitations == [] and after.last_success_at is not None


@pytest.mark.parametrize("status", ["not_found", "unsupported"])
async def test_missing_session_recovers_only_after_that_session_is_reread(
    store: Store, status: Literal["not_found", "unsupported"]
) -> None:
    # A clean later session must not hide an earlier missing one, including after restart.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    only_worker(store._db)
    first = SessionHistory(
        attempt_id=1,
        session_id="12345678-1234-1234-1234-123456789abc",
        session_generation=0,
        status=status,
        starts_at_beginning=True,
        reached_end=True,
    )
    second = SessionHistory(
        attempt_id=2,
        session_id="12345678-1234-1234-1234-123456789def",
        session_generation=0,
        status="available",
        starts_at_beginning=True,
        reached_end=True,
    )
    conversations = Mock(spec=ConversationService)
    conversations.read = AsyncMock(
        side_effect=[
            ConversationPage(
                flow_id=flow.id, sessions=[first], has_more=True, next_cursor="second"
            ),
            ConversationPage(flow_id=flow.id, sessions=[second], next_cursor="eof"),
            ConversationPage(
                flow_id=flow.id,
                sessions=[first.model_copy(update={"status": "available"})],
                has_more=True,
                next_cursor="second",
            ),
            ConversationPage(flow_id=flow.id, sessions=[second], next_cursor="eof"),
        ]
    )
    sync = ConsoleSynchronizer(Mock(), conversations, database=store._db)
    for index, cursor in enumerate([None, "second", None, "second"]):
        if index == 2:
            sync = ConsoleSynchronizer(Mock(), conversations, database=store._db)
            ConsoleStore(store._db).request_sync(flow.id)
            only_worker(store._db)
        with store.connection() as connection:
            connection.execute("UPDATE console_sync SET next_at=now()")
        claim = sync.store.claim()
        assert claim is not None and claim.source == "worker" and claim.cursor == cursor
        await sync.sync(claim)
        source = next(s for s in ConsoleStore(store._db).sources(flow.id) if s.source == "worker")
        if index == 1:
            assert source.status == "partial" and source.reason == status
        elif index == 3:
            assert source.status == "available" and source.reason is None
            assert source.limitations == []
    conversations.read.assert_any_await(flow.id, None, 50)
    with store.connection() as connection:
        checkpoint = connection.execute(
            "SELECT cursor FROM console_sync WHERE flow_id=%s AND source='worker'",
            (flow.id,),
        ).fetchone()
    assert checkpoint is not None and checkpoint["cursor"] == "eof"


async def test_session_gap_survives_clean_pages_until_complete_clean_rescan(store: Store) -> None:
    # Page-local unsupported must survive clean continuation/EOF, even across restart.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    console = ConsoleStore(store._db)
    session = SessionHistory(
        attempt_id=1,
        session_id="12345678-1234-1234-1234-123456789abc",
        session_generation=0,
        status="unsupported",
        starts_at_beginning=True,
        reached_end=False,
    )
    conversations = Mock(spec=ConversationService)
    conversations.read = AsyncMock()
    unsupported_start = ConversationPage(
        flow_id=flow.id, sessions=[session], next_cursor="middle", has_more=True
    )
    clean_tail = ConversationPage(
        flow_id=flow.id,
        sessions=[
            session.model_copy(
                update={
                    "status": "available",
                    "starts_at_beginning": False,
                    "reached_end": True,
                }
            )
        ],
        next_cursor="eof",
    )
    clean_restart = ConversationPage(
        flow_id=flow.id,
        sessions=[session.model_copy(update={"status": "available"})],
        next_cursor="middle",
        has_more=True,
    )

    async def read_page(page: ConversationPage, *, expected_cursor: str | None) -> SourceStatus:
        # Recreate the synchronizer on every page to exercise durable recovery.
        sync = ConsoleSynchronizer(Mock(), conversations, database=store._db)
        only_worker(store._db)
        with store.connection() as connection:
            # Advance durable scheduling without wall-clock sleeps.
            connection.execute("UPDATE console_sync SET next_at=now()")
        claim = sync.store.claim()
        assert claim is not None and claim.source == "worker"
        assert claim.cursor == expected_cursor
        conversations.read.return_value = page
        await sync.sync(claim)
        conversations.read.assert_awaited_with(flow.id, expected_cursor, 50)
        return next(source for source in console.sources(flow.id) if source.source == "worker")

    # A clean final page must preserve the unsupported first page's gap.
    source = await read_page(unsupported_start, expected_cursor=None)
    assert source.status == "pending" and source.reason == "unsupported"
    source = await read_page(clean_tail, expected_cursor="middle")
    assert source.status == "partial" and source.reason == "unsupported"

    # A persisted incremental checkpoint from before gap recovery was added.
    console.request_sync(flow.id)
    with store.connection() as connection:
        connection.execute(
            "UPDATE console_sync SET cursor='eof' WHERE flow_id=%s AND source='worker'",
            (flow.id,),
        )
    source = await read_page(clean_tail, expected_cursor="eof")
    assert source.status == "partial" and source.reason == "unsupported"

    # The next refresh must rewind, and repair coverage only after the clean scan ends.
    console.request_sync(flow.id)
    source = await read_page(clean_restart, expected_cursor=None)
    assert source.status == "pending" and source.reason == "unsupported"
    source = await read_page(clean_tail, expected_cursor="middle")
    assert source.status == "available" and source.reason is None

    # Once repaired, the next refresh keeps its incremental EOF checkpoint.
    console.request_sync(flow.id)
    source = await read_page(clean_tail, expected_cursor="eof")
    assert source.status == "available" and source.reason is None


@pytest.mark.parametrize("legacy", ["no_worker", "no_session", "not_found", "unsupported"])
async def test_legacy_availability_limitations_force_a_fresh_scan_and_clear(
    store: Store, legacy: str
) -> None:
    flow = store.flows.create_flow("owner/repo", 7, 42)
    only_worker(store._db)
    with store.connection() as connection:
        connection.execute(
            "UPDATE console_sync SET cursor='old-eof',limitations=%s WHERE source='worker'",
            (Jsonb([legacy]),),
        )
    conversations = Mock(spec=ConversationService)
    conversations.read = AsyncMock(
        return_value=ConversationPage(
            flow_id=flow.id,
            next_cursor="new-eof",
            sessions=[
                SessionHistory(
                    attempt_id=1,
                    session_id="12345678-1234-1234-1234-123456789abc",
                    session_generation=0,
                    status="available",
                    starts_at_beginning=True,
                    reached_end=True,
                )
            ],
        )
    )
    sync = ConsoleSynchronizer(Mock(), conversations, database=store._db)
    claim = sync.store.claim()
    assert claim is not None and claim.cursor is None
    await sync.sync(claim)
    conversations.read.assert_awaited_once_with(flow.id, None, 50)
    source = next(s for s in ConsoleStore(store._db).sources(flow.id) if s.source == "worker")
    assert source.status == "available" and source.reason is None
    assert source.limitations == []


async def test_shutdown_during_failed_batch_waits_for_real_database_threads(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Failure starts cancellation cleanup; shutdown must not cancel that drain again.
    store.flows.create_flow("owner/repo", 7, 42)
    sync = ConsoleSynchronizer(Mock(), Mock(), database=store._db)
    claim = sync.store.claim()
    assert claim is not None
    monkeypatch.setattr(sync.store, "claim", Mock(return_value=claim))
    loop = asyncio.get_running_loop()
    entered: Queue[bool] = Queue()
    started, release = asyncio.Event(), Event()
    siblings: list[asyncio.Task[None]] = []
    drained: list[bool] = []
    finished: list[Event] = []
    first = True

    def blocking(done: Event) -> None:
        try:
            with store.connection() as connection:
                connection.execute("SELECT 1")
                entered.put(True)
                if entered.qsize() == 3:
                    loop.call_soon_threadsafe(started.set)
                assert release.wait(10)
        finally:
            done.set()

    async def source(claim: SyncClaim) -> None:
        nonlocal first
        if first:
            first = False
            await started.wait()
            raise psycopg.DatabaseError("page failed")
        task = asyncio.current_task()
        assert task is not None
        siblings.append(task)
        done = Event()
        finished.append(done)
        try:
            await sync.db(lambda: blocking(done))
        finally:
            drained.append(done.is_set())

    monkeypatch.setattr(sync, "sync", source)
    run = asyncio.create_task(sync.run())
    try:
        async with asyncio.timeout(3):
            await started.wait()
            while not all(task.cancelling() for task in siblings):
                await asyncio.sleep(0)
            # Let the already-cancelled children enter db()'s thread drain first.
            await asyncio.sleep(0)
            for _ in range(2):
                run.cancel()
                await asyncio.sleep(0)
            # A source timeout can also cancel a child already draining its thread.
            for task in siblings:
                task.cancel()
            await asyncio.sleep(0)
            assert not run.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await run
        assert drained == [True, True, True]
    finally:
        release.set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        for done in finished:
            assert await asyncio.to_thread(done.wait, 3)


@pytest.mark.parametrize("cancel", [False, True])
async def test_batch_drains_siblings_before_retry_or_shutdown(store, monkeypatch, cancel):
    # A failed page or shutdown must not leave siblings holding DB capacity.
    store.flows.create_flow("owner/repo", 7, 42)
    sync = ConsoleSynchronizer(Mock(), Mock(), database=store._db)
    claim = sync.store.claim()
    assert claim is not None
    started, fail, cleaning, release = (asyncio.Event() for _ in range(4))
    children: list[asyncio.Task] = []
    claims = 0

    async def db(call):
        nonlocal claims
        claims += 1
        assert claims <= 4, "next batch started before siblings were drained"
        return claim

    async def source(claim):
        task = asyncio.current_task()
        assert task is not None
        children.append(task)
        if len(children) == 4:
            started.set()
        try:
            await fail.wait()
            if asyncio.current_task() is children[0]:
                raise psycopg.DatabaseError("page failed")
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    monkeypatch.setattr(sync, "db", db)
    monkeypatch.setattr(sync, "sync", source)
    run = asyncio.create_task(sync.run())
    try:
        await asyncio.wait_for(started.wait(), 3)
        if cancel:
            run.cancel()
        else:
            fail.set()
        await asyncio.wait_for(cleaning.wait(), 3)
        assert not run.done()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await run
        else:
            # The first failed child can finish, but siblings must be cancelled too.
            await asyncio.wait_for(asyncio.gather(*children, return_exceptions=True), 3)
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
        assert all(task.done() for task in children)
    finally:
        release.set()
        for task in [run, *children]:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(run, *children, return_exceptions=True)


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
