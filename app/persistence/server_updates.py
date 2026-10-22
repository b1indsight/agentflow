"""PostgreSQL event storage and Server consumer exclusion."""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from app.persistence.records import Row, required

SERVER_UPDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS server_update_events (
  run_id BIGINT NOT NULL,
  attempt INTEGER NOT NULL,
  sha TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued'
    CHECK(state IN ('queued','running','deployed','checked','failed','skipped')),
  run_number BIGINT,
  image TEXT,
  reason TEXT,
  PRIMARY KEY(run_id,attempt)
);
"""


@contextmanager
def database() -> Iterator[psycopg.Connection[Row]]:
    """Use the existing database directly, including while the Controller is stopped."""
    with psycopg.connect(
        os.environ["FLOWTOOL_DATABASE_URL"],
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=10,
    ) as connection:
        yield connection


@contextmanager
def consumer_lock(connection: psycopg.Connection[Row]) -> Iterator[None]:
    """Exclude another consumer without holding a transaction over deployment."""
    locked = required(
        connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(current_schema() || ':server-update',0)) AS locked"
        ).fetchone()
    )["locked"]
    if not locked:
        raise RuntimeError("A Server update consumer is already running")
    try:
        if connection.execute(
            "SELECT 1 FROM server_update_events WHERE state='running' LIMIT 1"
        ).fetchone():
            raise RuntimeError("Interrupted Server update: inspect its event and operation.json")
        yield
    finally:
        connection.execute(
            "SELECT pg_advisory_unlock(hashtextextended(current_schema() || ':server-update',0))"
        )
