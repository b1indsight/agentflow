"""Inspect live column metadata and detect removed columns in a private probe database.

The --snapshot mode only reads metadata from the configured application database.
Replay uses the probe Pod's private PostgreSQL and accepts no production DSN or rows.
Declaration differences cover removal from CREATE definitions; PostgreSQL column
identities also catch DROP followed by ADD with the same name. Types, defaults,
constraints and Worker protocols are not gates.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
from uuid import uuid4

import psycopg
from psycopg import sql

Column = tuple[str, str]


def snapshot() -> dict:
    """Read live schema declarations and column metadata without initializing the database."""
    from app.controler.config import ServerSettings
    from app.persistence.database import Database
    from app.persistence.records import required
    from app.persistence.schema import SCHEMAS

    database = Database(ServerSettings.model_validate({}).database_url)
    try:
        with database.connection() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.execute("SET LOCAL statement_timeout='10s'")
            connection.execute("SET LOCAL lock_timeout='3s'")
            return {
                "name": required(connection.execute("SELECT current_schema() AS name").fetchone())[
                    "name"
                ],
                "statements": list(SCHEMAS),
                "columns": connection.execute(
                    'SELECT c.relname AS "table",a.attname AS "column",'
                    ' pg_catalog.format_type(a.atttypid,a.atttypmod) AS "type"'
                    " FROM pg_catalog.pg_attribute a"
                    " JOIN pg_catalog.pg_class c ON c.oid=a.attrelid"
                    " JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace"
                    " WHERE n.nspname=current_schema() AND c.relkind IN ('r','p')"
                    " AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum"
                ).fetchall(),
            }
    finally:
        database.close()


def connect(database: str, user: str) -> psycopg.Connection:
    """Always use the disposable socket; never inherit an application database URL."""
    return psycopg.connect(
        host="/socket",
        dbname=database,
        user=user,
        autocommit=True,
        connect_timeout=2,
        options="-c statement_timeout=20000 -c lock_timeout=3000",
    )


def columns(connection: psycopg.Connection) -> dict[Column, tuple[int, int]]:
    """Identify stored columns by relation OID and attribute number, not only name."""
    rows = connection.execute(
        "SELECT c.relname,a.attname,c.oid,a.attnum FROM pg_catalog.pg_attribute a"
        " JOIN pg_catalog.pg_class c ON c.oid=a.attrelid"
        " JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace"
        " WHERE n.nspname=current_schema() AND c.relkind IN ('r','p')"
        " AND a.attnum>0 AND NOT a.attisdropped"
    ).fetchall()
    return {(table, column): (relation, position) for table, column, relation, position in rows}


def execute_schemas(connection: psycopg.Connection, statements: list[str]) -> None:
    """Match Database.init_db: execute the complete schema in one transaction."""
    with connection.transaction():
        for statement in statements:
            connection.execute(statement)


def verify(admin: psycopg.Connection, baseline: dict, candidate: list[str]) -> dict:
    """Compare declarations and replay against old columns, without copying live data."""
    schema, statements, live = baseline["name"], baseline["statements"], baseline["columns"]
    if (
        not isinstance(schema, str)
        or not schema
        or not isinstance(statements, list)
        or not statements
        or any(not isinstance(item, str) or not item.strip() for item in statements)
        or not isinstance(live, list)
        or not live
        or any(
            not isinstance(row, dict)
            or any(
                not isinstance(row.get(key), str) or not row[key]
                for key in ("table", "column", "type")
            )
            for row in live
        )
    ):
        raise ValueError("Incomplete live column snapshot")
    live_names = {(row["table"], row["column"]) for row in live}
    token = uuid4().hex[:16]
    runner, replay, fresh = (f"column_{kind}_{token}" for kind in ("runner", "replay", "fresh"))
    try:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOINHERIT").format(sql.Identifier(runner))
        )
        for database in (replay, fresh):
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(
                    sql.Identifier(database), sql.Identifier(runner)
                )
            )
        with connect(fresh, runner) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
            )
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            execute_schemas(connection, candidate)
            declared_candidate = columns(connection)

        with connect(replay, runner) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
            )
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            execute_schemas(connection, statements)
            declared_old = columns(connection)
            tables = {table for table, _ in declared_old}
            # Legacy live-only columns are protected against physical deletion, but
            # their absence from both image declarations must not block harmless updates.
            for row in live:
                if row["table"] not in tables:
                    connection.execute(
                        sql.SQL("CREATE TABLE {}.{} ()").format(
                            sql.Identifier(schema), sql.Identifier(row["table"])
                        )
                    )
                    tables.add(row["table"])
                if (row["table"], row["column"]) not in declared_old:
                    connection.execute(
                        sql.SQL("ALTER TABLE {}.{} ADD COLUMN {} {}").format(
                            sql.Identifier(schema),
                            sql.Identifier(row["table"]),
                            sql.Identifier(row["column"]),
                            sql.SQL(row["type"]),
                        )
                    )
            before = columns(connection)
            if not live_names <= before.keys():
                raise ValueError("Could not reconstruct live columns")
            execute_schemas(connection, candidate)
            after = columns(connection)
            removed = (declared_old.keys() & live_names) - declared_candidate.keys()
            removed |= {name for name in live_names if after.get(name) != before[name]}
            return {
                "checked_columns": len(live_names),
                "removed_columns": [list(name) for name in sorted(removed)],
            }
    finally:
        for database in (replay, fresh):
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runner)))


def main() -> None:
    """Wait only for initial DB startup; never retry candidate DDL after a lost response."""
    if sys.argv[1:] == ["--snapshot"]:
        print(json.dumps(snapshot(), sort_keys=True))
        return
    importlib.import_module("app.controler.runtime")
    from app.persistence.schema import SCHEMAS

    baseline = json.loads(sys.argv[1])
    deadline = time.monotonic() + 120
    while True:
        try:
            admin = connect("postgres", "postgres")
            break
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Disposable column-check PostgreSQL did not start") from None
            time.sleep(1)
    with admin:
        result = verify(admin, baseline, list(SCHEMAS))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (psycopg.Error, RuntimeError, ValueError, KeyError, TypeError, ImportError) as error:
        print(
            f"Column inspection failed ({type(error).__name__}); inspect the probe Pod.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
