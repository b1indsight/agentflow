"""Private exec adapter for communication-idle updates; never initializes the live database."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import timedelta

import psycopg


def execute() -> None:
    """Pause admissions or read one consistent execution snapshot, without worker-set gates."""
    mode = sys.argv[1]
    if mode not in {"status", "pause", "resume", "schema"}:
        raise ValueError("unsupported maintenance mode")

    from app.controler.config import ServerSettings
    from app.persistence.database import Database
    from app.persistence.maintenance import MaintenanceStore
    from app.persistence.records import required
    from app.persistence.schema import SCHEMAS

    settings = ServerSettings.model_validate({})
    database = Database(
        settings.database_url,
        recovery_grace=timedelta(seconds=settings.worker_recovery_grace),
    )
    try:
        with database.transaction() as tx, tx.connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            connection.execute("SET LOCAL statement_timeout='10s'")
            connection.execute("SET LOCAL lock_timeout='3s'")
            if mode == "schema":
                connection.execute("SET TRANSACTION READ ONLY")
                # Match column-check.py's stored-column scope; views are not data columns.
                result = {
                    "name": required(
                        connection.execute("SELECT current_schema() AS name").fetchone()
                    )["name"],
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
                print(json.dumps(result, sort_keys=True))
                return
            maintenance = MaintenanceStore(tx)
            if mode == "pause":
                maintenance.pause(sys.argv[2])
            elif mode == "resume":
                maintenance.resume(sys.argv[2])
            queries = {
                "jobs": "SELECT count(*) AS n FROM jobs WHERE state='running'",
                "dispatches": "SELECT count(*) AS n FROM jobs"
                " WHERE result_received_at IS NULL AND started_at IS NULL"
                " AND (dispatch_authorized_at IS NOT NULL"
                " OR (kind='archive' AND payload->'archive' IS NOT NULL))",
                "stops": "SELECT count(*) AS n FROM execution_stops WHERE state!='confirmed'",
                "results": "SELECT count(*) AS n FROM jobs"
                " WHERE result_received_at IS NOT NULL AND result_applied_at IS NULL",
                "effects": "SELECT count(*) AS n FROM result_effects"
                " WHERE state NOT IN ('succeeded','skipped','failed')"
                " OR (dispatched_at IS NOT NULL AND receipt IS NULL)",
                "slots": "SELECT count(*) AS n FROM workers WHERE current_job_id IS NOT NULL",
                "controls": "SELECT count(*) AS n FROM server_activities",
            }
            result = {
                # Read from the live image, not the host's injected adapter source.
                "communication_idle": getattr(MaintenanceStore, "COMMUNICATION_IDLE", None),
                "maintenance": asdict(maintenance.status()),
                "counts": {
                    name: required(connection.execute(query).fetchone())["n"]
                    for name, query in queries.items()
                },
                "control_work": maintenance.active(),
                # Bindings and worker states are diagnostic, never idle predicates.
                "bindings": required(
                    connection.execute(
                        "SELECT count(*) AS n FROM flow_executions WHERE released_at IS NULL"
                    ).fetchone()
                )["n"],
                "workers": connection.execute(
                    "SELECT worker_id,draining,status,current_job_id FROM workers ORDER BY worker_id"
                ).fetchall(),
            }
    finally:
        database.close()
    print(json.dumps(result, sort_keys=True))


def main() -> None:
    """Retry only transactions PostgreSQL confirms were aborted, never lost responses."""
    for attempt in range(3):
        try:
            execute()
            return
        except psycopg.errors.SerializationFailure:
            if attempt == 2:
                raise


if __name__ == "__main__":
    try:
        main()
    except (
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        OSError,
        ImportError,
        psycopg.Error,
    ) as error:
        # Settings and connection errors can contain secrets. Keep raw diagnostics private.
        print(
            json.dumps(
                {
                    "error_type": type(error).__name__,
                    "hint": "requires execution-idle maintenance support in the server image",
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
