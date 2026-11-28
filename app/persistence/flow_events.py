"""Durable console events and commit notifications for the single serve process."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from threading import Lock
from typing import TYPE_CHECKING
from uuid import UUID

from psycopg import Connection

from app.persistence.records import Row, required
from app.persistence.repository import Repository

if TYPE_CHECKING:
    from app.persistence.database import Database


def event_id(ms: int, seq: int, server: UUID) -> str:
    return f"{ms:013d}-{seq:019d}-{server}"


class FlowEvents(Repository):
    """Subscribers borrow a DB connection only to read a cursor or a batch."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self._lock = Lock()
        self._subscribers: set[Callable[[], asyncio.Handle]] = set()

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Event]:
        """Subscribe before reading the cursor so a racing commit cannot be missed."""
        changed = asyncio.Event()
        wake = partial(asyncio.get_running_loop().call_soon_threadsafe, changed.set)
        with self._lock:
            self._subscribers.add(wake)
        try:
            yield changed
        finally:
            with self._lock:
                self._subscribers.remove(wake)

    def committed(self) -> None:
        with self._lock:
            for wake in self._subscribers:
                wake()

    @staticmethod
    def append_finished(connection: Connection[Row], jobs: list[int]) -> None:
        """Take the event clock last, so sequence and commit order agree across workflows."""
        with connection.cursor() as cursor:
            cursor.executemany(
                "WITH tick AS (UPDATE console_event_clock SET seq=seq+1, "
                "timestamp_ms=greatest(timestamp_ms,floor(extract(epoch FROM clock_timestamp())*1000)::bigint) "
                "WHERE singleton RETURNING seq,timestamp_ms) "
                "INSERT INTO console_flow_events(seq,timestamp_ms,event_type,flow_id,payload) "
                "SELECT seq,timestamp_ms,'flow.job.finished.v1',flow_id, "
                "jsonb_build_object('job_id',id,'kind',kind,'state',state) FROM tick,jobs WHERE id=%s",
                [(job,) for job in jobs],
            )

    def resume(self, last_id: str | None) -> tuple[UUID, int, str | None]:
        """Return the exclusive replay position, or a new snapshot watermark."""
        with self._db.connection() as connection:
            clock = required(connection.execute("SELECT * FROM console_event_clock").fetchone())
            server, head = clock["server_id"], clock["seq"]
            if last_id == event_id(0, 0, server):
                return server, 0, None
            if match := re.fullmatch(r"([0-9]{13})-([0-9]{19})-([0-9a-f-]{36})", last_id or ""):
                stamp, number, origin = match.groups()
                seq = int(number)
                if origin == str(server) and seq <= head and connection.execute(
                    "SELECT 1 FROM console_flow_events WHERE seq=%s AND timestamp_ms=%s", (seq, int(stamp))
                ).fetchone():
                    return server, seq, None
            return server, head, event_id(clock["timestamp_ms"], head, server)

    def after(self, seq: int) -> list[Row]:
        with self._db.connection() as connection:
            return connection.execute(
                "SELECT seq,timestamp_ms,event_type,flow_id,payload FROM console_flow_events "
                "WHERE seq>%s ORDER BY seq LIMIT 64", (seq,),
            ).fetchall()
