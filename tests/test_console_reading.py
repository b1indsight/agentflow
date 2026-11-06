"""Personal progress uses exact revisions and PostgreSQL serialization, never a watermark."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest

from app.contracts.console import ConsoleFailure
from app.contracts.reading import MarkReading, PositionClaim, PositionSave, ReadBatch, ReadingAnchor
from app.github.console import Observation
from app.persistence.activity import ActivityStore
from app.persistence.reading import ReadingStore


@pytest.fixture
def reading(store):
    flow = store.flows.create_flow("owner/repo", 7, 1)
    archive = ActivityStore(store._db)
    item = Observation(key="comment:1", source="issue.comments", kind="comment", author="owner",
                       body="甲😀乙丙丁戊", occurred_at=flow.created_at, updated_at=flow.created_at)
    archive.append(flow.id, item)
    return flow, archive, ReadingStore(store._db), item


def observe(progress, flow_id, entry, ranges, *, generation=0, header=True, owner="owner"):
    return progress.observe(owner, flow_id, ReadBatch.model_validate({"items": [{
        "key": entry.key, "version": entry.version, "read_revision": entry.read_revision,
        "generation": generation, "header": header, "ranges": ranges,
    }]}))


def test_ranges_merge_without_filling_unseen_holes_and_owner_isolation(reading, store):
    flow, archive, progress, _ = reading
    entry = archive.page(flow.id).items[0]
    # A phone jumps past the middle; a desktop later reads that gap.
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda span: observe(progress, flow.id, entry, [span]), [[0, 2], [4, 6]]))
    assert progress.state("owner", flow.id).unread_count == 1
    first = progress.state("owner", flow.id).receipts_version
    observe(progress, flow.id, entry, [[0, 2], [4, 6]])
    assert progress.state("owner", flow.id).receipts_version == first
    assert observe(progress, flow.id, entry, [[2, 4]]).unread_count == 0
    assert progress.state("other", flow.id).unread_count == 1
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM console_reading_state").fetchone()["n"] == 1


def test_mark_unread_fences_delayed_observations_and_batch_rolls_back(reading):
    flow, archive, progress, _ = reading
    entry = archive.page(flow.id).items[0]
    state = observe(progress, flow.id, entry, [[0, 6]])
    command = MarkReading(action="entry_unread", receipts_version=state.receipts_version,
                          key=entry.key, version=entry.version, read_revision=entry.read_revision,
                          generation=0)
    state = progress.mark("owner", flow.id, command)
    with pytest.raises(ConsoleFailure, match="reading_conflict"):
        observe(progress, flow.id, entry, [[0, 6]])
    assert state.unread_count == 1
    batch = ReadBatch.model_validate({"items": [
        {"key": entry.key, "version": entry.version, "read_revision": entry.read_revision,
         "generation": 1, "header": True, "ranges": [[0, 2]]},
        {"key": entry.key, "version": entry.version, "read_revision": entry.read_revision,
         "generation": 1, "header": True, "ranges": [[0, 7]]},
    ]})
    with pytest.raises(ConsoleFailure, match="reading_range_invalid"):
        progress.observe("owner", flow.id, batch)
    assert progress.state("owner", flow.id).receipts_version == state.receipts_version


def test_revision_semantics_edits_backfills_metadata_and_old_observations(reading):
    flow, archive, progress, item = reading
    before = archive.page(flow.id)
    observe(progress, flow.id, before.items[0], [[0, 6]])
    archive.append(flow.id, item.model_copy(update={"url": "https://example.com/comment",
                                                   "metadata": {"transport": "new"}}))
    assert archive.page(flow.id).items[0].read_revision == before.items[0].read_revision
    assert progress.state("owner", flow.id).unread_count == 0
    archive.append(flow.id, item.model_copy(update={"body": "changed", "updated_at": item.updated_at + timedelta(seconds=2)}))
    archive.append(flow.id, item.model_copy(update={"body": "late old observation", "updated_at": item.updated_at + timedelta(seconds=1)}))
    archive.append(flow.id, item.model_copy(update={"key": "early", "occurred_at": item.occurred_at - timedelta(days=5)}))
    assert progress.state("owner", flow.id).unread_count == 2
    # An explicit operation on a pinned snapshot cannot read later edits/backfills.
    state = progress.state("owner", flow.id)
    progress.mark("owner", flow.id, MarkReading(action="snapshot_read", snapshot=before.snapshot,
                                                receipts_version=state.receipts_version))
    assert progress.state("owner", flow.id).unread_count == 2


def test_thread_state_returning_to_same_value_is_a_new_revision(reading):
    flow, archive, progress, item = reading
    thread = item.model_copy(update={"key": "thread", "kind": "thread", "body": "",
                                      "metadata": {"resolved": True}})
    archive.append(flow.id, thread)
    first = next(e for e in archive.page(flow.id).items if e.key == "thread")
    observe(progress, flow.id, first, [])
    archive.append(flow.id, thread.model_copy(update={"metadata": {"resolved": False}}))
    archive.append(flow.id, thread)
    current = next(e for e in archive.page(flow.id).items if e.key == "thread")
    assert current.read_revision != first.read_revision
    assert progress.state("owner", flow.id).unread_count == 2


def anchor_for(entry, snapshot):
    return ReadingAnchor(key=entry.key, version=entry.version, read_revision=entry.read_revision,
                         snapshot=snapshot, parent_key=entry.parent_key, part="body", body_offset=2,
                         block_fraction=0.5, occurred_at=entry.occurred_at, layout_version=1)


def test_position_cas_fences_old_devices_and_accepts_upward_reading(reading):
    flow, archive, progress, _ = reading
    page = archive.page(flow.id)
    entry = page.items[0]
    a, b = uuid4(), uuid4()
    state = progress.claim("owner", flow.id, PositionClaim(session_id=a, position_revision=0))
    saved = progress.save_position("owner", flow.id, PositionSave(session_id=a, writer_epoch=state.writer_epoch,
                                   sequence=2, anchor=anchor_for(entry, page.snapshot)))
    with pytest.raises(ConsoleFailure, match="position_conflict"):
        progress.claim("owner", flow.id, PositionClaim(session_id=b, position_revision=0))
    state = progress.claim("owner", flow.id, PositionClaim(session_id=b, position_revision=saved.position_revision))
    with pytest.raises(ConsoleFailure, match="position_conflict"):
        progress.save_position("owner", flow.id, PositionSave(session_id=a, writer_epoch=saved.writer_epoch,
                               sequence=3, anchor=anchor_for(entry, page.snapshot)))
    upward = anchor_for(entry, page.snapshot).model_copy(update={"body_offset": 0})
    progress.save_position("owner", flow.id, PositionSave(session_id=b, writer_epoch=state.writer_epoch,
                           sequence=1, anchor=upward))
    assert observe(progress, flow.id, entry, [[0, 6]]).anchor == upward
    with pytest.raises(ConsoleFailure, match="position_conflict"):
        progress.save_position("owner", flow.id, PositionSave(session_id=b, writer_epoch=state.writer_epoch,
                               sequence=1, anchor=upward))


def test_resume_is_bounded_and_fallback_is_visible_without_rewriting_position(reading, store):
    flow, archive, progress, item = reading
    page = archive.page(flow.id)
    original = anchor_for(page.items[0], page.snapshot)
    session = uuid4()
    claim = progress.claim("owner", flow.id, PositionClaim(session_id=session, position_revision=0))
    progress.save_position("owner", flow.id, PositionSave(session_id=session, writer_epoch=claim.writer_epoch,
                           sequence=1, anchor=original))
    for n in range(65):
        archive.append(flow.id, item.model_copy(update={"key": f"next:{n:03}", "occurred_at": item.occurred_at + timedelta(seconds=n+1)}))
    exact = progress.resume("owner", flow.id)
    assert exact.reason == "exact" and len(exact.page.items) <= 20
    assert exact.resolved_anchor.body_offset == 2
    archive.append(flow.id, item.model_copy(update={"body": "edited", "updated_at": item.updated_at + timedelta(seconds=3)}))
    edited = progress.resume("owner", flow.id)
    assert edited.reason == "edited" and edited.resolved_anchor.body_offset == 0
    archive.append(flow.id, item.model_copy(update={"deleted": True, "body": ""}))
    assert progress.resume("owner", flow.id).reason == "deleted"
    with store.connection() as connection:
        connection.execute("DELETE FROM console_activity WHERE flow_id=%s AND key=%s", (flow.id, item.key))
    missing = progress.resume("owner", flow.id)
    assert missing.reason == "missing" and missing.resolved_anchor.key == "next:000"
    assert progress.state("owner", flow.id).anchor == original


def test_migration_replay_is_idempotent_and_get_never_creates_state(reading, store):
    flow, archive, progress, item = reading
    archive.append(flow.id, item.model_copy(update={"body": "edited", "updated_at": item.updated_at + timedelta(seconds=1)}))
    expected = [(e.version, e.read_revision) for e in archive.versions(flow.id, item.key).items]
    with store.connection() as connection:
        connection.execute("UPDATE console_activity SET read_revision=NULL WHERE flow_id=%s", (flow.id,))
    progress.state("owner", flow.id)
    assert [(e.version, e.read_revision) for e in archive.versions(flow.id, item.key).items] == expected
    store.init_db()
    assert progress.state("owner", flow.id).unread_count == 1
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM console_reading_state").fetchone()["n"] == 0


def test_uncommitted_backfill_cannot_be_consumed_by_snapshot_mark(reading, store):
    flow, archive, progress, item = reading
    published, release = Event(), Event()
    before = archive.page(flow.id)
    observe(progress, flow.id, before.items[0], [], header=False)

    def append_later():
        with store._db.transaction() as tx:
            ActivityStore(tx).append(flow.id, item.model_copy(update={
                "key": "backfill", "occurred_at": item.occurred_at - timedelta(days=1),
            }))
            published.set()
            assert release.wait(5)

    # The archive version is allocated, but another connection cannot read it
    # until commit. Marking the visible snapshot must not cover this older event.
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(append_later)
        try:
            assert published.wait(5)
            state = progress.state("owner", flow.id)
            assert state.snapshot == before.snapshot
            marked = progress.mark("owner", flow.id, MarkReading(
                action="snapshot_read", snapshot=before.snapshot,
                receipts_version=state.receipts_version,
            ))
            assert marked.unread_count == 0
        finally:
            release.set()
        future.result()
    assert progress.state("owner", flow.id).unread_count == 1


def test_reply_resume_and_incompatible_layout_preserve_original_anchor(reading, store):
    flow, archive, progress, item = reading
    archive.append(flow.id, item.model_copy(update={"key": "thread", "kind": "thread"}))
    archive.append(flow.id, item.model_copy(update={"key": "reply", "parent_key": "thread"}))
    page = archive.page(flow.id, parent="thread")
    original = anchor_for(page.items[0], page.snapshot)
    session = uuid4()
    claim = progress.claim("owner", flow.id, PositionClaim(session_id=session, position_revision=0))
    progress.save_position("owner", flow.id, PositionSave(
        session_id=session, writer_epoch=claim.writer_epoch, sequence=1, anchor=original,
    ))
    resumed = progress.resume("owner", flow.id)
    assert resumed.parent_key == "thread" and resumed.page.items[0].key == "reply"
    with store.connection() as connection:
        connection.execute("UPDATE console_reading_state SET anchor=jsonb_set(anchor,'{layout_version}','2')")
    incompatible = progress.resume("owner", flow.id)
    assert incompatible.reason == "incompatible"
    assert incompatible.resolved_anchor.part == "summary"
    assert incompatible.original_anchor.layout_version == 2
    # A retained reply cannot restore into a parent removed by historical cleanup.
    with store.connection() as connection:
        connection.execute("DELETE FROM console_activity WHERE flow_id=%s AND key='thread'", (flow.id,))
    missing = progress.resume("owner", flow.id)
    assert missing.reason == "missing" and missing.parent_key is None
    assert missing.resolved_anchor.key == item.key
    assert missing.original_anchor == incompatible.original_anchor
