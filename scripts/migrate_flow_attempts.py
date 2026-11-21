"""Offline history-only cutover for an unmarked round ledger."""

import argparse
import json

import psycopg
from psycopg import Connection, sql
from psycopg.rows import dict_row

from app.persistence.database import (
    FLOW_TABLES,
    mark_flow_schema_current,
    require_current_schema,
    require_worker_key,
    schema_relations,
)
from app.persistence.records import Row
from app.persistence.schema import SCHEMAS


def flow_schema_version(connection: Connection[Row]) -> str:
    """Identify a supported source or target schema; never infer a completed cutover from rows."""
    relations = schema_relations(connection)
    names = {name for name, kind in relations.items() if kind in ("r", "p")}
    if names & {"issue_flow_owners", "flow_deliveries", "webhook_inbox"}:
        raise ValueError("pre-185 schema: stop writers and migrate the three legacy tables offline")
    if not relations:
        return "empty"
    columns = {
        (row["table_name"], row["column_name"])
        for row in connection.execute(
            "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=current_schema()"
        )
    }
    if "workers" in names:
        require_worker_key(connection)
        if columns & {("workers", "workspace_state"), ("workers", "retired_generation")}:
            raise ValueError(
                "obsolete workspace state schema: this unreleased schema requires explicit"
                " reconciliation before upgrading; automatic migration is not supported"
            )
    legacy = {
        ("flows", "current_round_no"),
        ("flows", "delivery_branch"),
        ("attempts", "round_no"),
        ("jobs", "round_no"),
        ("console_activity", "round_no"),
    }
    new = {
        ("attempts", "delivery_branch"),
        ("jobs", "attempt_id"),
        ("console_activity", "attempt_id"),
        ("flows", "history_only"),
    }
    if ("flows", "is_issue_owner") not in columns:
        raise ValueError("incomplete Flow schema: offline investigation required")
    obsolete = {
        "review_items",
        "review_pending_edits",
        "review_thread_observations",
        "feedback_reconcile",
        "flow_reconcile",
    }
    if "flowtool_schema_version" in names:
        require_current_schema(connection, relations)
        if not (legacy & columns) and "flow_attempt_legacy_map" not in names:
            return "current"
    elif (
        FLOW_TABLES <= names
        and legacy <= columns
        and not (new & columns)
        and not ((obsolete | {"flow_attempt_legacy_map"}) & names)
    ):
        return "legacy"
    raise ValueError("unrecognized or partial Flow schema: offline investigation required")


def _unsafe(c: Connection[Row]) -> list[str]:
    """Never discard execution, source receipts or an uncertain remote write to make a cutover pass."""
    return [
        f"{r['source']} {r['id']}: not settled"
        for r in c.execute("""
        SELECT 'job' AS source,j.id::text AS id FROM jobs j WHERE state NOT IN ('done','failed')
          OR kind NOT IN ('initial','revision','archive','inference')
          OR ((result_payload IS NOT NULL OR result_digest IS NOT NULL OR result_received_at IS NOT NULL
               OR result_applied_at IS NOT NULL) AND (result_payload IS NULL OR result_digest IS NULL
               OR result_received_at IS NULL OR result_applied_at IS NULL))
          OR (kind<>'inference' AND (started_at IS NOT NULL OR dispatch_authorized_at IS NOT NULL
              OR (kind='archive' AND payload->'archive' IS NOT NULL)) AND (result_received_at IS NULL
              OR (started_at IS NOT NULL AND result_payload->>'source' IS DISTINCT FROM 'worker'))
              AND NOT EXISTS (SELECT 1 FROM execution_stops s WHERE s.job_id=j.id
                  AND s.flow_id=j.flow_id AND s.assignment_token=j.assignment_token AND s.state='confirmed'))
        UNION ALL SELECT 'result application',j.id::text FROM result_applications r JOIN jobs j ON j.id=r.job_id
          WHERE j.result_digest IS DISTINCT FROM r.digest OR j.result_applied_at IS NULL
        UNION ALL SELECT 'binding',flow_id::text FROM flow_executions WHERE released_at IS NULL
        UNION ALL SELECT 'worker slot',worker_id||':'||workspace FROM workers
          WHERE current_job_id IS NOT NULL OR current_inference_job_id IS NOT NULL
        UNION ALL SELECT 'stop',request_id FROM execution_stops WHERE state<>'confirmed'
          OR confirmed_at IS NULL OR receipt_digest IS NULL
        UNION ALL SELECT 'effect',concat(job_id,':',effect_key) FROM result_effects
          WHERE state NOT IN ('succeeded','failed','skipped') OR claimed_by IS NOT NULL OR claim_expires_at IS NOT NULL
          OR (dispatched_at IS NOT NULL AND receipt IS NULL)
        UNION ALL SELECT 'tool call',call_id FROM flow_tool_calls WHERE response IS NULL
        UNION ALL SELECT 'webhook',delivery_id FROM events WHERE state<>'done' OR claim_token IS NOT NULL
          OR claim_expires_at IS NOT NULL
        UNION ALL SELECT 'project add',id::text FROM project_projections WHERE add_started
        UNION ALL SELECT 'review batch',id::text FROM review_batches WHERE state IN ('ready','processing')
        ORDER BY source,id
    """)
    ]


# Only an owner referenced by a saved row needs an identity. Old INITIAL jobs could
# be archived before creating an Attempt; use the recorded start, not preparation time.
MISSING_ATTEMPTS = """
    SELECT r.flow_id,r.round_no,CASE WHEN r.round_no=1 THEN f.created_at ELSE
      (SELECT CASE WHEN count(*)=1 THEN min(occurred_at) END FROM console_activity
       WHERE flow_id=r.flow_id AND round_no=r.round_no AND source='flow' AND kind='round_resumed')
      END AS started FROM (
        SELECT id AS flow_id,current_round_no AS round_no FROM flows
        UNION SELECT flow_id,round_no FROM jobs WHERE flow_id IS NOT NULL
        UNION SELECT flow_id,round_no FROM console_activity WHERE round_no IS NOT NULL
      ) r JOIN flows f ON f.id=r.flow_id LEFT JOIN attempts a USING(flow_id,round_no)
      WHERE a.id IS NULL
"""


def _convert_rounds(c: Connection[Row]) -> None:
    """Only backfill structural ownership. Never rewrite opaque content or original evidence."""
    for table in ("attempts", "jobs"):
        c.execute(sql.SQL("DROP TRIGGER console_fact_changed ON {}").format(sql.Identifier(table)))
    c.execute("DROP TRIGGER console_preserve_evidence ON console_activity; DROP VIEW console_facts")
    c.execute(
        "ALTER TABLE jobs ADD attempt_id bigint; ALTER TABLE console_activity ADD attempt_id bigint"
    )
    columns = c.execute("""SELECT attname,format_type(atttypid,atttypmod) AS typ FROM pg_attribute
        WHERE attrelid='flows'::regclass AND starts_with(attname,'delivery_') AND NOT attisdropped
        ORDER BY attnum""").fetchall()
    for col in columns:
        c.execute(
            sql.SQL("ALTER TABLE attempts ADD {} {}").format(
                sql.Identifier(col["attname"]), sql.SQL(col["typ"])
            )
        )
    check = c.execute("""SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint
        WHERE conrelid='flows'::regclass AND conname='flows_delivery_complete'""").fetchone()
    if not check:
        raise ValueError("missing original delivery constraint")
    c.execute(
        sql.SQL("ALTER TABLE attempts ADD CONSTRAINT attempts_delivery_complete {}").format(
            sql.SQL(check["def"])
        )
    )
    c.execute(
        "ALTER TABLE attempts ADD UNIQUE(flow_id,id); ALTER TABLE flows DROP CONSTRAINT fk_flows_current_attempt"
    )
    c.execute(f"""INSERT INTO attempts(flow_id,round_no,created_at,updated_at)
        SELECT flow_id,round_no,started,started FROM ({MISSING_ATTEMPTS}) missing""")
    c.execute("""
        UPDATE flows f SET current_attempt_id=m.id FROM attempts m
          WHERE (f.id,f.current_round_no)=(m.flow_id,m.round_no);
        UPDATE jobs j SET attempt_id=m.id FROM attempts m
          WHERE (j.flow_id,j.round_no)=(m.flow_id,m.round_no);
        UPDATE console_activity a SET attempt_id=m.id FROM attempts m
          WHERE (a.flow_id,a.round_no)=(m.flow_id,m.round_no);
        UPDATE console_activity s SET attempt_id=a.id FROM attempts a WHERE s.source='worker'
          AND s.round_no IS NULL AND s.flow_id=a.flow_id AND s.metadata->>'attempt_id'=a.id::text;
        ALTER TABLE jobs ADD FOREIGN KEY(flow_id,attempt_id) REFERENCES attempts(flow_id,id) MATCH FULL;
        ALTER TABLE console_activity ADD CONSTRAINT console_activity_owner
            FOREIGN KEY(flow_id,attempt_id) REFERENCES attempts(flow_id,id);
        """)
    names = sql.SQL(",").join(sql.Identifier(col["attname"]) for col in columns)
    c.execute(
        sql.SQL(
            "UPDATE attempts a SET ({})=(SELECT {} FROM flows WHERE id=a.flow_id)"
            " FROM flows f WHERE f.current_attempt_id=a.id"
        ).format(names, names)
    )
    c.execute(
        sql.SQL("ALTER TABLE flows DROP current_round_no, {}").format(
            sql.SQL(",").join(
                sql.SQL("DROP {}").format(sql.Identifier(col["attname"])) for col in columns
            )
        )
    )
    c.execute(
        "ALTER TABLE attempts DROP round_no; ALTER TABLE jobs DROP round_no; ALTER TABLE console_activity DROP round_no"
    )
    # A populated round-era card is deliberately not re-published by the new frozen-card
    # projection. Adapt only its structural identity fields; do not append another event.
    c.execute("""UPDATE console_flows c SET facts=(c.facts-'current_round_no')||jsonb_build_object(
            'current_attempt_id',a.id,'attempt_id',a.id,'attempt_started_at',a.created_at)
        FROM flows f JOIN attempts a ON a.id=f.current_attempt_id
        WHERE f.id=c.flow_id AND c.facts<>'{}'""")


def _freeze(c: Connection[Row]) -> None:
    # Do not let the old projection trigger publish a state transition for a change in authority.
    c.execute("DROP TRIGGER console_fact_changed ON flows")
    _convert_rounds(c)
    c.execute("""ALTER TABLE flows ADD history_only boolean NOT NULL DEFAULT false,
        ADD CONSTRAINT flows_history_archived CHECK (NOT history_only OR state='archived')""")
    c.execute("UPDATE flows SET history_only=true,is_issue_owner=false")
    # A public link names a worker file, not a Flow. Retire old capabilities as one registry.
    c.execute("ALTER TABLE worker_file_links RENAME TO historical_worker_file_links")
    for schema in SCHEMAS:
        c.execute(schema)
    # Keep coverage, timestamps, gaps, cursors and source content. Stopping a scan does not
    # imply it ever reached EOF. These are work queues, not external-write acknowledgements.
    c.execute("""
        SELECT console_refresh(id) FROM flows ORDER BY id;
        UPDATE console_sync SET pending=false,dirty=false,claim_token=NULL,claim_until=NULL,
          status=CASE WHEN status IN ('pending','syncing') THEN 'partial' ELSE status END
          WHERE pending OR dirty OR claim_token IS NOT NULL OR claim_until IS NOT NULL;
        DELETE FROM project_projections;
        DELETE FROM reconcile_checks;
        UPDATE review_buffers SET due_at=NULL WHERE due_at IS NOT NULL;
        UPDATE console_activity SET awaiting_agent=false WHERE awaiting_agent;
    """)
    mark_flow_schema_current(c)


def migrate(database_url: str, *, apply: bool = False) -> Row:
    """Inspect a consistent read-only snapshot by default; --apply rechecks under offline locks."""
    with psycopg.connect(database_url, row_factory=dict_row) as c:
        if not apply:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        status = flow_schema_version(c)
        report: Row = {"status": status, "applied": False, "conflicts": []}
        if status != "legacy":
            return report
        if apply:
            c.execute(
                "LOCK flows,attempts,jobs,console_activity,console_flows,console_sync,flow_tool_calls,"
                "result_applications,result_effects,workers,flow_executions,execution_stops,events,review_buffers,"
                "review_batches,reconcile_checks,project_projections,worker_file_links IN ACCESS EXCLUSIVE MODE NOWAIT"
            )
            # Another completed cutover may have released its locks since the first read.
            if flow_schema_version(c) != status:
                raise ValueError("schema changed during preflight; rerun offline")
        report["conflicts"] = [
            f"flow {row['id']}: not archived ({row['state']})"
            for row in c.execute("SELECT id,state FROM flows WHERE state<>'archived' ORDER BY id")
        ]
        if report["conflicts"]:
            return report
        report["conflicts"].extend(
            r["problem"]
            for r in c.execute(f"""
            SELECT format('flow %s round %s: multiple attempts',flow_id,round_no) AS problem
              FROM attempts GROUP BY flow_id,round_no HAVING count(*)>1
            UNION ALL SELECT format('flow %s round %s: missing start',flow_id,round_no)
              FROM ({MISSING_ATTEMPTS}) missing WHERE started IS NULL
            UNION ALL SELECT format('flow %s: current attempt belongs elsewhere',f.id)
              FROM flows f JOIN attempts a ON a.id=f.current_attempt_id
              WHERE (a.flow_id,a.round_no)<>(f.id,f.current_round_no)
            UNION ALL SELECT format('flow %s activity %s: conflicting worker identity',s.flow_id,s.version)
              FROM console_activity s LEFT JOIN attempts a ON a.id::text=s.metadata->>'attempt_id'
              WHERE s.source='worker' AND s.metadata->>'attempt_id' IS NOT NULL AND
                (a.flow_id IS DISTINCT FROM s.flow_id OR a.round_no<>s.round_no)
        """)
        )
        report["conflicts"].extend(_unsafe(c))
        if apply and not report["conflicts"]:
            _freeze(c)
            report["applied"] = True
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url", required=True, help="PostgreSQL conninfo (prefer service=NAME)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply only after stopping every writer and verifying backup",
    )
    args = parser.parse_args()
    report = migrate(args.database_url, apply=args.apply)
    print(json.dumps(report, default=str, indent=2))
    raise SystemExit(1 if report["conflicts"] else 0)
