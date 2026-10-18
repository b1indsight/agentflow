"""Permanent collected versions and per-Flow committed reading snapshots."""

import base64
import hashlib
import json
from datetime import datetime
from difflib import unified_diff

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, ValidationError

from app.contracts.console import ActivityItem, ActivityPage, ConsoleFailure, ContentPage
from app.github.console import Observation
from app.persistence.console import ConsoleStore
from app.persistence.records import Row
from app.persistence.repository import Repository


class ActivityCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    flow: int
    snapshot: int
    time: datetime
    key: str
    parent: str | None
    versions: str | None = None
    revision: int = 0

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, token: str) -> "ActivityCursor":
        try:
            return cls.model_validate_json(base64.b64decode(token, altchars=b"-_", validate=True))
        except (ValueError, ValidationError) as error:
            raise ConsoleFailure("cursor_invalid") from error


# Metadata-only observations do not make a comment look edited. The predecessor
# belongs to this immutable version, never to a newer page/snapshot.
_PREVIOUS = (
    "CASE WHEN NOT current.deleted THEN (SELECT p.version FROM console_activity p"
    " WHERE p.flow_id=current.flow_id AND p.key=current.key AND p.version<current.version"
    " AND p.updated_at<=current.updated_at AND NOT p.deleted"
    " AND (p.title,p.body) IS DISTINCT FROM (current.title,current.body)"
    " ORDER BY p.updated_at DESC,p.version DESC LIMIT 1) END AS previous_version"
)
_SELECT = (
    "key,version,source,kind,occurred_at,updated_at,observed_at,author,title,"
    "left(body,2000) AS body,length(body) AS body_length,url,parent_key,deleted,metadata,"
    "(source!='flow' AND source!='worker' AND occurred_at<%s) AS prior," + _PREVIOUS
)


class ActivityStore(Repository):
    """Serialize publication with its archive rows, without sequence/commit visibility gaps."""

    def append(self, flow_id: int, item: Observation) -> bool:
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
                "SELECT activity_version FROM console_flows WHERE flow_id=%s FOR UPDATE", (flow_id,)
            ).fetchone()
            if row is None:
                raise ConsoleFailure("flow_not_found", 404)
            previous = connection.execute(
                "SELECT parent_key,digest FROM console_activity WHERE flow_id=%s"
                " AND key=%s ORDER BY version DESC LIMIT 1",
                (flow_id, item.key),
            ).fetchone()
            if previous and previous["parent_key"] and item.parent_key is None:
                item = item.model_copy(update={"parent_key": previous["parent_key"]})
                content["parent_key"] = previous["parent_key"]
            digest = hashlib.sha256(
                json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            # GitHub exposes no revision clock for thread resolution. Reopening A→B→A
            # must keep the second A; repeated reads of the current A are still idempotent.
            duplicate = (
                (previous and previous["digest"] == digest)
                if item.kind == "thread"
                else connection.execute(
                    "SELECT 1 FROM console_activity WHERE flow_id=%s AND key=%s AND digest=%s",
                    (flow_id, item.key, digest),
                ).fetchone()
            )
            if duplicate:
                return False
            version = row["activity_version"] + 1
            connection.execute(
                "INSERT INTO console_activity(flow_id,version,key,source,kind,occurred_at,updated_at,"
                "author,title,body,url,parent_key,deleted,metadata,digest)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    flow_id,
                    version,
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
                "WITH old AS (SELECT DISTINCT ON(key) key,version,occurred_at FROM console_activity"
                " WHERE flow_id=%s AND version<=%s AND source!='flow'"
                " ORDER BY key,deleted DESC,updated_at DESC,version DESC),"
                " new AS (SELECT DISTINCT ON(key) key,version,occurred_at,deleted FROM console_activity"
                " WHERE flow_id=%s AND version<=%s AND source!='flow'"
                " ORDER BY key,deleted DESC,updated_at DESC,version DESC)"
                " SELECT CASE WHEN n.deleted THEN 'deleted' WHEN o.key IS NOT NULL THEN 'edited'"
                " WHEN n.occurred_at<(SELECT max(occurred_at) FROM old) THEN 'backfilled' ELSE 'new' END AS kind,"
                " count(*) AS n FROM new n LEFT JOIN old o USING(key) WHERE n.version IS DISTINCT FROM o.version GROUP BY 1",
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
                "SELECT substring(body FROM %s FOR %s) AS text,length(body) AS n"
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
                "SELECT left(body,65537) AS body,left(title,4097) AS title,"
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
                "SELECT left(body,65537) AS body,left(title,4097) AS title"
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
