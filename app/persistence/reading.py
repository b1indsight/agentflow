"""Owner-local receipts and independently fenced cross-device reading positions."""

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg.types.multirange import Multirange
from psycopg.types.range import Range

from app.contracts.console import ActivityPage, ConsoleFailure, EntryReading, FlowSummary
from app.contracts.reading import (
    MarkReading,
    PositionClaim,
    PositionSave,
    ReadBatch,
    ReadingAnchor,
    ReadingResume,
    ReadingState,
    ResumeReason,
)
from app.persistence.activity import ActivityCursor, ActivityStore
from app.persistence.activity_selection import LATEST_ACTIVITY, display_body
from app.persistence.records import Row
from app.persistence.repository import Repository

_BODY_LENGTH = f"length({display_body('a.body', 'a.source')})"
_UNREAD = (
    "NOT coalesce(r.explicit_read,FALSE) AND (NOT coalesce(r.header_read,FALSE)"
    f" OR NOT coalesce(r.ranges @> int8range(0,{_BODY_LENGTH}),FALSE))"
)


class ReadingStore(Repository):
    """Serialize receipt changes per owner+Flow, without acquiring workflow write access.

    Receipts union only exact revision ranges. Explicit unread leaves a generation
    tombstone. Position claims use CAS; saves require its epoch and an increasing
    page-local sequence, so delayed/background writers cannot steal a bookmark.
    """

    def state(self, owner: str, flow_id: int, *, snapshot: int | None = None) -> ReadingState:
        head = ActivityStore(self._db)._head(flow_id)
        if snapshot is None:
            snapshot = head["activity_version"]
        if not 0 <= snapshot <= head["activity_version"]:
            raise ConsoleFailure("snapshot_invalid")
        with self._db.connection() as connection:
            row = connection.execute(
                LATEST_ACTIVITY + "SELECT s.receipts_version,s.position_revision,s.writer_epoch,"
                "s.anchor,s.saved_at,(SELECT count(*) FROM latest) AS total_count,"
                "(SELECT count(*) FROM latest a LEFT JOIN console_read_receipts r"
                " ON r.owner_key=%s AND r.flow_id=a.flow_id AND r.key=a.key"
                " AND r.read_revision=a.read_revision WHERE " + _UNREAD + ") AS unread_count"
                " FROM (VALUES(1)) seed(n) LEFT JOIN console_reading_state s"
                " ON s.owner_key=%s AND s.flow_id=%s",
                (flow_id, snapshot, owner, owner, flow_id),
            ).fetchone()
        assert row is not None
        return ReadingState(snapshot=snapshot, **{
            key: value for key, value in row.items() if value is not None
        })

    def _lock(self, connection: Connection[Row], owner: str, flow_id: int) -> Row:
        connection.execute(
            "INSERT INTO console_reading_state(owner_key,flow_id) VALUES(%s,%s)"
            " ON CONFLICT DO NOTHING", (owner, flow_id),
        )
        row = connection.execute(
            "SELECT * FROM console_reading_state WHERE owner_key=%s AND flow_id=%s FOR UPDATE",
            (owner, flow_id),
        ).fetchone()
        assert row is not None
        return row

    def _entry(self, connection: Connection[Row], flow_id: int, key: str,
               version: int, revision: int) -> Row:
        row = connection.execute(
            f"SELECT a.parent_key,a.occurred_at,{_BODY_LENGTH} AS body_length FROM console_activity a"
            " WHERE a.flow_id=%s AND a.key=%s AND a.version=%s AND a.read_revision=%s"
            " AND a.source!='flow'", (flow_id, key, version, revision),
        ).fetchone()
        if row is None:
            raise ConsoleFailure("reading_revision_invalid", 422)
        return row

    def _receipt(self, connection: Connection[Row], owner: str, flow_id: int,
                 key: str, revision: int, generation: int) -> None:
        connection.execute(
            "INSERT INTO console_read_receipts(owner_key,flow_id,key,read_revision)"
            " VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", (owner, flow_id, key, revision),
        )
        row = connection.execute(
            "SELECT generation FROM console_read_receipts WHERE owner_key=%s AND flow_id=%s"
            " AND key=%s AND read_revision=%s", (owner, flow_id, key, revision),
        ).fetchone()
        if row is None or row["generation"] != generation:
            raise ConsoleFailure("reading_conflict")

    def observe(self, owner: str, flow_id: int, batch: ReadBatch) -> ReadingState:
        """Atomically merge a bounded batch; malformed entries cannot partially mark read."""
        ActivityStore(self._db)._head(flow_id)
        with self._db.transaction() as tx, tx.connection() as connection:
            store = ReadingStore(tx)
            store._lock(connection, owner, flow_id)
            changed = False
            for item in batch.items:
                entry = store._entry(connection, flow_id, item.key, item.version, item.read_revision)
                if any(not 0 <= start < end <= entry["body_length"] for start, end in item.ranges):
                    raise ConsoleFailure("reading_range_invalid", 422)
                store._receipt(connection, owner, flow_id, item.key, item.read_revision, item.generation)
                ranges = Multirange([Range(start, end) for start, end in item.ranges])
                updated = connection.execute(
                    "UPDATE console_read_receipts SET header_read=header_read OR %s,ranges=ranges+%s"
                    " WHERE owner_key=%s AND flow_id=%s AND key=%s AND read_revision=%s"
                    " AND (header_read IS DISTINCT FROM (header_read OR %s) OR ranges!=ranges+%s)",
                    (item.header, ranges, owner, flow_id, item.key, item.read_revision, item.header, ranges),
                ).rowcount
                changed = changed or bool(updated)
            if changed:
                connection.execute(
                    "UPDATE console_reading_state SET receipts_version=receipts_version+1"
                    " WHERE owner_key=%s AND flow_id=%s", (owner, flow_id),
                )
            return store.state(owner, flow_id)

    def mark(self, owner: str, flow_id: int, command: MarkReading) -> ReadingState:
        head = ActivityStore(self._db)._head(flow_id)
        with self._db.transaction() as tx, tx.connection() as connection:
            store = ReadingStore(tx)
            state = store._lock(connection, owner, flow_id)
            if command.receipts_version != state["receipts_version"]:
                raise ConsoleFailure("reading_conflict")
            if command.action == "snapshot_read":
                assert command.snapshot is not None
                if command.snapshot > head["activity_version"]:
                    raise ConsoleFailure("snapshot_invalid")
                changed = connection.execute(
                    LATEST_ACTIVITY + "INSERT INTO console_read_receipts"
                    "(owner_key,flow_id,key,read_revision,header_read,explicit_read)"
                    " SELECT %s,flow_id,key,read_revision,TRUE,TRUE FROM latest"
                    " ON CONFLICT(owner_key,flow_id,key,read_revision) DO UPDATE"
                    " SET header_read=TRUE,explicit_read=TRUE"
                    " WHERE NOT console_read_receipts.explicit_read",
                    (flow_id, command.snapshot, owner),
                ).rowcount
            else:
                assert command.key is not None and command.version is not None
                assert command.read_revision is not None and command.generation is not None
                store._entry(connection, flow_id, command.key, command.version, command.read_revision)
                store._receipt(connection, owner, flow_id, command.key, command.read_revision, command.generation)
                assignment = ("generation=generation+1,header_read=FALSE,ranges='{}',explicit_read=FALSE"
                              if command.action == "entry_unread" else "header_read=TRUE,explicit_read=TRUE")
                changed = connection.execute(
                    "UPDATE console_read_receipts SET " + assignment +
                    " WHERE owner_key=%s AND flow_id=%s AND key=%s AND read_revision=%s" +
                    (" AND NOT explicit_read" if command.action == "entry_read" else ""),
                    (owner, flow_id, command.key, command.read_revision),
                ).rowcount
            if changed:
                connection.execute(
                    "UPDATE console_reading_state SET receipts_version=receipts_version+1"
                    " WHERE owner_key=%s AND flow_id=%s", (owner, flow_id),
                )
            return store.state(owner, flow_id)

    def claim(self, owner: str, flow_id: int, command: PositionClaim) -> ReadingState:
        ActivityStore(self._db)._head(flow_id)
        with self._db.transaction() as tx, tx.connection() as connection:
            store = ReadingStore(tx)
            state = store._lock(connection, owner, flow_id)
            if state["position_revision"] != command.position_revision:
                raise ConsoleFailure("position_conflict")
            connection.execute(
                "UPDATE console_reading_state SET writer_session=%s,writer_epoch=writer_epoch+1,"
                "position_revision=position_revision+1,last_sequence=0 WHERE owner_key=%s AND flow_id=%s",
                (command.session_id, owner, flow_id),
            )
            return store.state(owner, flow_id)

    def save_position(self, owner: str, flow_id: int, command: PositionSave) -> ReadingState:
        head = ActivityStore(self._db)._head(flow_id)
        with self._db.transaction() as tx, tx.connection() as connection:
            store = ReadingStore(tx)
            state = store._lock(connection, owner, flow_id)
            if (state["writer_session"] != command.session_id or state["writer_epoch"] != command.writer_epoch
                    or state["last_sequence"] >= command.sequence):
                raise ConsoleFailure("position_conflict")
            anchor = command.anchor
            row = store._entry(connection, flow_id, anchor.key, anchor.version, anchor.read_revision)
            if (not anchor.version <= anchor.snapshot <= head["activity_version"]
                    or anchor.body_offset > row["body_length"] or anchor.parent_key != row["parent_key"]
                    or anchor.occurred_at != row["occurred_at"] or anchor.layout_version != 1
                    or anchor.part == "summary" and anchor.body_offset != 0):
                raise ConsoleFailure("reading_anchor_invalid", 422)
            selected = connection.execute(
                LATEST_ACTIVITY + "SELECT version FROM latest WHERE key=%s",
                (flow_id, anchor.snapshot, anchor.key),
            ).fetchone()
            if selected is None or selected["version"] != anchor.version:
                raise ConsoleFailure("reading_anchor_invalid", 422)
            connection.execute(
                "UPDATE console_reading_state SET anchor=%s,last_sequence=%s,saved_at=clock_timestamp(),"
                "position_revision=position_revision+1 WHERE owner_key=%s AND flow_id=%s",
                (Jsonb(anchor.model_dump(mode="json")), command.sequence, owner, flow_id),
            )
            return store.state(owner, flow_id)

    def resume(self, owner: str, flow_id: int) -> ReadingResume:
        """Locate a bounded context directly in the new snapshot; GET never moves the bookmark."""
        state = self.state(owner, flow_id)
        original = state.anchor
        reason: ResumeReason = "exact"
        target = None
        with self._db.connection() as connection:
            if original:
                target = connection.execute(
                    LATEST_ACTIVITY + "SELECT a.key,a.version,a.read_revision,a.parent_key,a.deleted,"
                    f"a.occurred_at,{_BODY_LENGTH} AS body_length FROM latest a WHERE key=%s",
                    (flow_id, state.snapshot, original.key),
                ).fetchone()
                parent = target["parent_key"] if target else original.parent_key
                if parent and not connection.execute(
                    LATEST_ACTIVITY + "SELECT 1 FROM latest WHERE key=%s",
                    (flow_id, state.snapshot, parent),
                ).fetchone():
                    # A surviving reply must not restore into a removed discussion.
                    parent = None
                    target = None
                if target is None:
                    reason = "missing"
                    # Prefer the next sibling; if none remains, the previous sibling.
                    target = connection.execute(
                        LATEST_ACTIVITY + "SELECT a.key,a.version,a.read_revision,a.parent_key,a.deleted,"
                        f"a.occurred_at,{_BODY_LENGTH} AS body_length FROM latest a"
                        " WHERE parent_key IS NOT DISTINCT FROM %s ORDER BY"
                        " CASE WHEN (occurred_at,key)>=(%s,%s) THEN 0 ELSE 1 END,"
                        " CASE WHEN (occurred_at,key)>=(%s,%s) THEN occurred_at END ASC,"
                        " CASE WHEN (occurred_at,key)>=(%s,%s) THEN key END ASC,occurred_at DESC,key DESC LIMIT 1",
                        (flow_id, state.snapshot, parent, original.occurred_at, original.key,
                         original.occurred_at, original.key, original.occurred_at, original.key),
                    ).fetchone()
                elif target["deleted"]:
                    reason = "deleted"
                elif original.layout_version != 1:
                    reason = "incompatible"
                elif target["read_revision"] != original.read_revision:
                    reason = "edited"
            resolved = None
            cursor = None
            parent_key = target["parent_key"] if target else None
            if target:
                resolved = ReadingAnchor(
                    key=target["key"], version=target["version"], read_revision=target["read_revision"],
                    snapshot=state.snapshot, parent_key=parent_key,
                    part=original.part if original and reason == "exact" else "summary",
                    body_offset=original.body_offset if original and reason == "exact" else 0,
                    block_fraction=original.block_fraction if original and reason == "exact" else 0,
                    occurred_at=target["occurred_at"], layout_version=1,
                )
                preceding = connection.execute(
                    LATEST_ACTIVITY + "SELECT occurred_at,key FROM latest"
                    " WHERE parent_key IS NOT DISTINCT FROM %s AND (occurred_at,key)<(%s,%s)"
                    " ORDER BY occurred_at DESC,key DESC OFFSET 5 LIMIT 1",
                    (flow_id, state.snapshot, parent_key, target["occurred_at"], target["key"]),
                ).fetchone()
                if preceding:
                    cursor = ActivityCursor(flow=flow_id, snapshot=state.snapshot, parent=parent_key,
                                            time=preceding["occurred_at"], key=preceding["key"]).encode()
        page = ActivityStore(self._db).page(flow_id, snapshot=state.snapshot, cursor=cursor,
                                            parent=parent_key, limit=20)
        self.annotate_page(owner, flow_id, page)
        return ReadingResume(state=state, original_anchor=original, resolved_anchor=resolved,
                             reason=reason, page=page, start_cursor=cursor, parent_key=parent_key)

    def annotate_page(self, owner: str, flow_id: int, page: ActivityPage) -> ActivityPage:
        """Batch only the page's receipt metadata; never load historical bodies or receipts."""
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM console_read_receipts WHERE owner_key=%s AND flow_id=%s"
                " AND key=ANY(%s) AND read_revision=ANY(%s)",
                (owner, flow_id, [e.key for e in page.items], [e.read_revision for e in page.items]),
            ).fetchall()
        receipts = {(row["key"], row["read_revision"]): row for row in rows}
        for entry in page.items:
            row = receipts.get((entry.key, entry.read_revision))
            if row:
                ranges = [[span.lower, span.upper] for span in row["ranges"]]
                complete = entry.body_length == 0 or ranges == [[0, entry.body_length]]
                entry.reading = EntryReading(
                    generation=row["generation"], header_read=row["header_read"], ranges=ranges,
                    explicit_read=row["explicit_read"],
                    unread=not (row["explicit_read"] or row["header_read"] and complete),
                )
        return page

    def annotate_flows(self, owner: str, flows: list[FlowSummary]) -> None:
        """One query for all board cards, including unloaded discussion replies."""
        if not flows:
            return
        with self._db.connection() as connection:
            rows = connection.execute(
                "WITH latest AS (SELECT DISTINCT ON(flow_id,key) * FROM console_activity"
                " WHERE flow_id=ANY(%s) AND source!='flow' ORDER BY flow_id,key,"
                "deleted DESC,updated_at DESC,version DESC) SELECT a.flow_id,count(*) FILTER(WHERE "
                + _UNREAD + ") AS n FROM latest a LEFT JOIN console_read_receipts r"
                " ON r.owner_key=%s AND r.flow_id=a.flow_id AND r.key=a.key"
                " AND r.read_revision=a.read_revision GROUP BY a.flow_id",
                ([flow.id for flow in flows], owner),
            ).fetchall()
        counts = {row["flow_id"]: row["n"] for row in rows}
        for flow in flows:
            flow.unread_count = counts.get(flow.id, 0)
