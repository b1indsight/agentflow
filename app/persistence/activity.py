"""Permanent collected versions and per-Flow committed reading snapshots."""

import hashlib
import json
from datetime import datetime
from difflib import unified_diff

from psycopg.types.json import Jsonb

from app.contracts.console import ActivityItem, ActivityPage, ConsoleFailure, ContentPage
from app.domain.flow_policy import ORIGIN_KINDS, FlowFact
from app.github.console import Observation
from app.persistence.console import ConsoleStore
from app.persistence.cursor import Cursor
from app.persistence.records import Row
from app.persistence.repository import Repository

_FLOW_FACT_TITLES = {
    "created": "Flow 创建",
    "snapshot": "账本起点快照",
    "job_enqueued": "任务入队",
    "job_claimed": "任务已派发",
    "assignment_requeued": "派发收回后重新排队",
    "execution_uncertain": "执行状态无法确认",
    "result_failed": "结果失败",
    "delivery_baseline_rejected": "PR 分支已变化",
    "delivery_applied": "交付已应用",
    "finalization_requested": "请求收尾",
    "cleanup_applied": "清理结果已应用",
    "retry_requested": "请求重试",
    "attempt_started": "重新开工",
    "archive": "Flow 归档",
    "merge": "已确认合并",
    "archive_correction": "归档结果自动更正",
    "titled": "标题已生成",
}
_ATTEMPT_FACTS = frozenset({"archive", "merge", "archive_correction", "attempt_started"})
# Keep in sync with console_preserve_evidence: every other source='flow' kind is the ledger.
_NON_LEDGER_SQL = "('state','baseline','inference_exhausted')"


class ActivityCursor(Cursor):
    flow: int
    snapshot: int
    time: datetime
    key: str
    parent: str | None
    versions: str | None = None
    revision: int = 0


# Decode coding-result summaries and hide GitHub transport receipts at the same boundary.
# Keeping this in PostgreSQL gives previews, paging and diffs one body without unbounded reads.
def _display_body(alias: str) -> str:
    prefix = f"{alias}." if alias else ""
    body, source = f"{prefix}body", f"{prefix}source"
    questions = f"{body}::jsonb->'needs_clarification'"
    return (
        f"CASE WHEN {source}='worker' AND {prefix}kind='message'"
        f" AND {prefix}author='assistant' AND pg_input_is_valid({body},'jsonb') THEN "
        # A valid JSONB guard also rejects NUL/invalid surrogates before decoding native text.
        f"CASE WHEN jsonb_typeof({body}::jsonb->'summary')='string'"
        f" AND {body}::jsonb->>'vcs_backend_used' IN ('git','jj')"
        f" THEN ({body}::jsonb->>'summary') || CASE WHEN jsonb_typeof({questions})='array'"
        " THEN coalesce(E'\\n\\n待澄清：\\n\\n' || (SELECT string_agg('- ' || (q #>> '{}'),"
        f" E'\\n' ORDER BY n) FROM jsonb_array_elements({questions}) WITH ORDINALITY AS points(q,n)"
        " WHERE jsonb_typeof(q)='string' AND btrim(q #>> '{}')<>''), '') ELSE '' END"
        f" ELSE {body} END ELSE "
        f"CASE WHEN {source}='worker' THEN {body} ELSE "
        f"regexp_replace({body}, E'\\n\\n<!-- flowtool:effect:[0-9a-f]{{64}} -->$', '') END END"
    )


_CURRENT_DISPLAY_BODY = _display_body("current")
_PREVIOUS_DISPLAY_BODY = _display_body("p")


# Metadata-only observations do not make a comment look edited. The predecessor
# belongs to this immutable version, never to a newer page/snapshot.
_PREVIOUS = (
    "CASE WHEN NOT current.deleted THEN (SELECT p.version FROM console_activity p"
    " WHERE p.flow_id=current.flow_id AND p.key=current.key AND p.version<current.version"
    " AND p.updated_at<=current.updated_at AND NOT p.deleted"
    f" AND (p.title,{_PREVIOUS_DISPLAY_BODY}) IS DISTINCT FROM "
    f"(current.title,{_CURRENT_DISPLAY_BODY})"
    " ORDER BY p.updated_at DESC,p.version DESC LIMIT 1) END AS previous_version"
)
_SELECT = (
    "key,version,attempt_id,source,kind,occurred_at,updated_at,observed_at,author,title,"
    f"left({_CURRENT_DISPLAY_BODY},2000) AS body,length({_CURRENT_DISPLAY_BODY}) AS body_length,"
    "url,parent_key,deleted,metadata,"
    "(source!='flow' AND source!='worker' AND occurred_at<%s) AS prior," + _PREVIOUS
)


class ActivityStore(Repository):
    """Serialize publication with its archive rows, without sequence/commit visibility gaps."""

    def append(self, flow_id: int, item: Observation) -> bool:
        """Collect external versions without allowing them to impersonate workflow evidence."""
        if item.source == "flow" or item.key.startswith("flow:"):
            raise ValueError("Flow evidence requires the internal activity writer")
        return self._append(flow_id, item)

    def append_flow(
        self,
        flow_id: int,
        attempt_id: int,
        kind: str,
        *,
        occurred_at: datetime,
        metadata: dict[str, str | int | bool | None],
    ) -> bool:
        """Append one immutable Flow ledger fact, sharing the ordinary publication fence.

        Call from the workflow transaction after validating the fact. These records
        are durable evidence, not mutable display-object versions or an Attempt entity.
        A repeated once-per-attempt fact is idempotent; a conflicting one is rejected.
        """
        if attempt_id < 1 or kind not in _FLOW_FACT_TITLES:
            raise ValueError("invalid Flow activity identity")
        with self._db.transaction() as tx, tx.connection() as connection:
            head = connection.execute(
                "SELECT activity_version FROM console_flows WHERE flow_id=%s FOR UPDATE",
                (flow_id,),
            ).fetchone()
            if head is None:
                raise ConsoleFailure("flow_not_found", 404)
            if kind in _ATTEMPT_FACTS:
                key = f"flow:{flow_id}:attempt:{attempt_id}:{kind}"
            elif kind in ORIGIN_KINDS:
                key = f"flow:{flow_id}:{kind}"
            else:
                # Repeatable transitions are identified by the version this lock reserves.
                key = f"flow:{flow_id}:fact:{head['activity_version'] + 1}"
            item = Observation(
                key=key, attempt_id=attempt_id, source="flow", kind=kind,
                occurred_at=occurred_at, updated_at=occurred_at, author="FlowTool",
                title=_FLOW_FACT_TITLES[kind], metadata=metadata,
            )
            if kind in _ATTEMPT_FACTS:
                previous = ActivityStore(tx).flow_event(flow_id, attempt_id, kind)
                if previous is not None:
                    # Compare immutable business evidence, not its presentation.
                    if previous.occurred_at != occurred_at or previous.metadata != metadata:
                        raise ValueError("conflicting immutable Flow activity")
                    return False
            return ActivityStore(tx)._append(flow_id, item)

    def flow_facts(self, flow_id: int) -> list[FlowFact]:
        """Read the Flow ledger in replay order, skipping display and notice rows."""
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT kind,attempt_id,occurred_at,metadata FROM console_activity"
                f" WHERE flow_id=%s AND source='flow' AND kind NOT IN {_NON_LEDGER_SQL}"
                " ORDER BY version",
                (flow_id,),
            ).fetchall()
        return [FlowFact.model_validate(row) for row in rows]

    def flow_event(self, flow_id: int, attempt_id: int, kind: str) -> Observation | None:
        """Read original workflow evidence, never the folded conversation projection."""
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT key,attempt_id,source,kind,occurred_at,updated_at,author,title,body,"
                " url,parent_key,deleted,metadata FROM console_activity"
                " WHERE flow_id=%s AND attempt_id=%s AND source='flow' AND kind=%s",
                (flow_id, attempt_id, kind),
            ).fetchone()
        return Observation.model_validate(row) if row else None

    def archived(self, flow_id: int, attempt_id: int) -> Observation | None:
        return self.flow_event(flow_id, attempt_id, "archive")

    def history(self, flow_id: int, attempt_id: int | None = None) -> list[ActivityItem]:
        """Read the Flow audit in version order, optionally scoped to one execution."""
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT key,version,attempt_id,source,kind,occurred_at,updated_at,observed_at,author,"
                "title,body,length(body) AS body_length,url,parent_key,deleted,metadata,"
                "FALSE AS prior FROM console_activity WHERE flow_id=%s AND source='flow'"
                + (" AND attempt_id=%s" if attempt_id is not None else "") + " ORDER BY version",
                (flow_id, attempt_id) if attempt_id is not None else (flow_id,),
            ).fetchall()
        return [ActivityItem.model_validate(row) for row in rows]

    def _append(self, flow_id: int, item: Observation) -> bool:
        # PostgreSQL TEXT/JSONB cannot store NUL. Keep its visible escape and disclose
        # the encoding; hash the original so a literal escape is a different version.
        content = item.model_dump(mode="json", exclude={"source"})
        escaped = item.model_copy(
            update={
                "key": item.key.replace("\0", "\\u0000"),
                "source": item.source.replace("\0", "\\u0000"),
                "kind": item.kind.replace("\0", "\\u0000"),
                "author": item.author.replace("\0", "\\u0000"),
                "title": item.title.replace("\0", "\\u0000"),
                "body": item.body.replace("\0", "\\u0000"),
                "url": item.url.replace("\0", "\\u0000") if item.url is not None else None,
                "parent_key": item.parent_key.replace("\0", "\\u0000")
                if item.parent_key is not None
                else None,
                "metadata": {
                    key.replace("\0", "\\u0000"): value.replace("\0", "\\u0000")
                    if isinstance(value, str)
                    else value
                    for key, value in item.metadata.items()
                },
            }
        )
        if escaped != item:
            escaped = escaped.model_copy(
                update={
                    "metadata": {**escaped.metadata, "unsupported_characters_escaped": True},
                }
            )
        item = escaped
        with self._db.transaction() as tx, tx.connection() as connection:
            row = connection.execute(
                "SELECT c.activity_version,f.history_only FROM console_flows c"
                " JOIN flows f ON f.id=c.flow_id WHERE c.flow_id=%s FOR UPDATE OF c", (flow_id,)
            ).fetchone()
            if row is None:
                raise ConsoleFailure("flow_not_found", 404)
            if row["history_only"]:
                if item.source == "flow":
                    raise ValueError("flow_read_only")
                return False
            previous = connection.execute(
                "SELECT parent_key,digest,attempt_id FROM console_activity"
                " WHERE flow_id=%s AND key=%s ORDER BY version DESC LIMIT 1",
                (flow_id, item.key),
            ).fetchone()
            if previous and previous["attempt_id"] is not None:
                if item.attempt_id is not None and item.attempt_id != previous["attempt_id"]:
                    raise ValueError("activity object cannot move to another Attempt")
                item = item.model_copy(update={"attempt_id": previous["attempt_id"]})
                content["attempt_id"] = previous["attempt_id"]
            if previous and previous["parent_key"] and item.parent_key is None:
                item = item.model_copy(update={"parent_key": previous["parent_key"]})
                content["parent_key"] = previous["parent_key"]
            encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            # Thread resolution and optional check enrichment have no remote revision clock.
            # Keep A→B→A transitions; repeated reads of the current A are still idempotent.
            duplicate = (
                (previous and previous["digest"] == digest)
                if item.kind in ("thread", "check")
                else connection.execute(
                    "SELECT 1 FROM console_activity WHERE flow_id=%s AND key=%s AND digest=%s",
                    (flow_id, item.key, digest),
                ).fetchone()
            )
            if duplicate:
                return False
            version = row["activity_version"] + 1
            connection.execute(
                "INSERT INTO console_activity(flow_id,version,attempt_id,key,source,kind,occurred_at,updated_at,"
                "author,title,body,url,parent_key,deleted,metadata,digest)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    flow_id,
                    version,
                    item.attempt_id,
                    item.key,
                    item.source,
                    item.kind,
                    item.occurred_at,
                    item.updated_at,
                    item.author,
                    item.title,
                    item.body,
                    item.url,
                    item.parent_key,
                    item.deleted,
                    Jsonb(item.metadata),
                    digest,
                ),
            )
            connection.execute(
                "UPDATE console_flows SET activity_version=%s WHERE flow_id=%s", (version, flow_id)
            )
            if item.source == "issue.description" and not item.deleted:
                connection.execute(
                    "UPDATE console_flows SET state_version=state_version+CASE WHEN title IS DISTINCT FROM %s"
                    " THEN 1 ELSE 0 END,title=%s,title_updated_at=%s"
                    " WHERE flow_id=%s AND (title_updated_at IS NULL OR title_updated_at<=%s)",
                    (item.title, item.title, item.updated_at, flow_id, item.updated_at),
                )
        return True

    def _head(self, flow_id: int) -> Row:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT activity_version,(facts->>'created_at')::timestamptz AS created_at"
                " FROM console_flows WHERE flow_id=%s",
                (flow_id,),
            ).fetchone()
        if row is None:
            raise ConsoleFailure("flow_not_found", 404)
        return row

    def page(
        self,
        flow_id: int,
        *,
        cursor: str | None = None,
        snapshot: int | None = None,
        limit: int = 20,
        since: int | None = None,
        parent: str | None = None,
    ) -> ActivityPage:
        """Read conversation versions at one watermark, excluding internal Flow audit records."""
        head = self._head(flow_id)
        position = ActivityCursor.decode(cursor) if cursor else None
        if position:
            if (
                position.flow != flow_id
                or position.parent != parent
                or position.versions is not None
                or snapshot is not None
                and snapshot != position.snapshot
            ):
                raise ConsoleFailure("cursor_invalid")
            snapshot = position.snapshot
        if snapshot is None:
            snapshot = head["activity_version"]
        if (
            not 0 <= snapshot <= head["activity_version"]
            or since is not None
            and not 0 <= since <= snapshot
        ):
            raise ConsoleFailure("snapshot_invalid")
        prefix = (
            "WITH latest AS (SELECT DISTINCT ON (key) * FROM console_activity"
            " WHERE flow_id=%s AND version<=%s AND source!='flow'"
            " ORDER BY key,deleted DESC,updated_at DESC,version DESC) "
        )
        with self._db.connection() as connection:
            count = connection.execute(
                prefix
                + "SELECT count(*) AS n FROM latest WHERE parent_key IS NOT DISTINCT FROM %s",
                (flow_id, snapshot, parent),
            ).fetchone()
            assert count is not None
            after = " AND (occurred_at,key)>(%s,%s)" if position else ""
            rows = connection.execute(
                prefix
                + "SELECT "
                + _SELECT
                + " FROM latest current WHERE parent_key IS NOT DISTINCT FROM %s"
                + after
                + " ORDER BY occurred_at,key LIMIT %s",
                (
                    flow_id,
                    snapshot,
                    head["created_at"],
                    parent,
                    *((position.time, position.key) if position else ()),
                    limit + 1,
                ),
            ).fetchall()
        more = len(rows) > limit
        return ActivityPage(
            items=[ActivityItem.model_validate(row) for row in rows[:limit]],
            snapshot=snapshot,
            total=count["n"],
            has_more=more,
            next_cursor=ActivityCursor(
                flow=flow_id,
                snapshot=snapshot,
                time=rows[limit - 1]["occurred_at"],
                key=rows[limit - 1]["key"],
                parent=parent,
            ).encode()
            if more
            else None,
            sources=ConsoleStore(self._db).sources(flow_id),
            changes=self._changes(flow_id, since, snapshot) if since is not None else {},
        )

    def _changes(self, flow_id: int, before: int, after: int) -> dict[str, int]:
        """Compare visible versions, including old edits/deletions and early backfill."""
        with self._db.connection() as connection:
            rows = connection.execute(
                "WITH old AS (SELECT DISTINCT ON(key) key,version,occurred_at,deleted,title,body,source,"
                "metadata,author,kind,url,parent_key"
                " FROM console_activity"
                " WHERE flow_id=%s AND version<=%s AND source!='flow'"
                " ORDER BY key,deleted DESC,updated_at DESC,version DESC),"
                " new AS (SELECT DISTINCT ON(key) key,version,occurred_at,deleted,title,body,source,"
                "metadata,author,kind,url,parent_key"
                " FROM console_activity"
                " WHERE flow_id=%s AND version<=%s AND source!='flow'"
                " ORDER BY key,deleted DESC,updated_at DESC,version DESC)"
                " SELECT CASE WHEN n.deleted THEN 'deleted' WHEN o.key IS NOT NULL THEN 'edited'"
                " WHEN n.occurred_at<(SELECT max(occurred_at) FROM old) THEN 'backfilled' ELSE 'new' END AS kind,"
                " count(*) AS n FROM new n LEFT JOIN old o USING(key)"
                " WHERE n.version IS DISTINCT FROM o.version AND (o.key IS NULL OR (n.title,"
                + _display_body("n")
                + ",n.deleted,n.metadata,n.author,n.url,n.parent_key) IS DISTINCT FROM (o.title,"
                + _display_body("o")
                + ",o.deleted,o.metadata,o.author,o.url,o.parent_key)) GROUP BY 1",
                (flow_id, before, flow_id, after),
            ).fetchall()
        return {row["kind"]: row["n"] for row in rows}

    def versions(
        self,
        flow_id: int,
        key: str,
        *,
        cursor: str | None = None,
        snapshot: int | None = None,
        limit: int = 20,
    ) -> ActivityPage:
        head = self._head(flow_id)
        position = ActivityCursor.decode(cursor) if cursor else None
        if position and (
            position.flow != flow_id
            or position.versions != key
            or snapshot is not None
            and snapshot != position.snapshot
        ):
            raise ConsoleFailure("cursor_invalid")
        snapshot = (
            position.snapshot
            if position
            else snapshot
            if snapshot is not None
            else head["activity_version"]
        )
        if not 0 <= snapshot <= head["activity_version"]:
            raise ConsoleFailure("snapshot_invalid")
        with self._db.connection() as connection:
            count = connection.execute(
                "SELECT count(*) AS n FROM console_activity WHERE flow_id=%s AND key=%s AND version<=%s",
                (flow_id, key, snapshot),
            ).fetchone()
            assert count is not None
            rows = connection.execute(
                "SELECT " + _SELECT + " FROM console_activity current WHERE flow_id=%s AND key=%s"
                " AND version>%s AND version<=%s ORDER BY version LIMIT %s",
                (
                    head["created_at"],
                    flow_id,
                    key,
                    position.revision if position else 0,
                    snapshot,
                    limit + 1,
                ),
            ).fetchall()
        more = len(rows) > limit
        return ActivityPage(
            items=[ActivityItem.model_validate(row) for row in rows[:limit]],
            snapshot=snapshot,
            total=count["n"],
            has_more=more,
            sources=[],
            next_cursor=ActivityCursor(
                flow=flow_id,
                snapshot=snapshot,
                time=rows[limit - 1]["occurred_at"],
                key=key,
                parent=None,
                versions=key,
                revision=rows[limit - 1]["version"],
            ).encode()
            if more
            else None,
        )

    def content(
        self,
        flow_id: int,
        key: str,
        version: int,
        offset: int,
        limit: int = 16384,
        *,
        compare: bool = False,
    ) -> ContentPage:
        if compare:
            # A patch must be complete to preserve its headers, hunks and line coordinates.
            if offset != 0:
                raise ConsoleFailure("offset_invalid", 422)
            text, previous = self._comparison(flow_id, key, version)
            return ContentPage(
                key=key,
                version=version,
                previous_version=previous,
                text=text,
                total=len(text),
                next_offset=None,
            )
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT substring("
                + _display_body("")
                + " FROM %s FOR %s) AS text,length("
                + _display_body("")
                + ") AS n"
                " FROM console_activity WHERE flow_id=%s AND key=%s AND version=%s",
                (offset + 1, limit, flow_id, key, version),
            ).fetchone()
        if row is None:
            raise ConsoleFailure("content_not_found", 404)
        if offset > row["n"]:
            raise ConsoleFailure("offset_invalid", 422)
        end = offset + len(row["text"])
        return ContentPage(
            key=key,
            version=version,
            text=row["text"],
            total=row["n"],
            next_offset=end if end < row["n"] else None,
        )

    def _comparison(self, flow_id: int, key: str, version: int) -> tuple[str, int]:
        """Compare complete immutable text, with explicit CPU/input budgets, never a clipped diff."""
        with self._db.connection() as connection:
            current = connection.execute(
                "SELECT left("
                + _CURRENT_DISPLAY_BODY
                + ",65537) AS body,left(title,4097) AS title,"
                + _PREVIOUS
                + " FROM console_activity current WHERE flow_id=%s AND key=%s AND version=%s",
                (flow_id, key, version),
            ).fetchone()
            if current is None:
                raise ConsoleFailure("content_not_found", 404)
            previous = current["previous_version"]
            if previous is None:
                raise ConsoleFailure("previous_version_not_found", 404)
            old = connection.execute(
                "SELECT left("
                + _display_body("")
                + ",65537) AS body,left(title,4097) AS title"
                " FROM console_activity WHERE flow_id=%s AND key=%s AND version=%s",
                (flow_id, key, previous),
            ).fetchone()
        assert old is not None
        for item in (old, current):
            if len(item["body"]) > 65536 or len(item["title"]) > 4096:
                raise ConsoleFailure("diff_too_large", 422)
        # Splitting on LF keeps trailing empty lines and CRLF changes visible.
        before = (old["title"] + "\n\n" + old["body"]).split("\n")
        after = (current["title"] + "\n\n" + current["body"]).split("\n")
        if max(len(before), len(after)) > 2000:
            raise ConsoleFailure("diff_too_large", 422)
        return "\n".join(
            unified_diff(
                before,
                after,
                fromfile="上一版",
                tofile="当前版本",
                lineterm="",
            )
        ), previous
