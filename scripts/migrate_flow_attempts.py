"""Offline history-only cutover for an unmarked round ledger or marked v2 Attempt ledger."""

import argparse
import json
from collections import defaultdict

import psycopg
from psycopg import Connection, sql
from psycopg.rows import dict_row

from app.domain.flow_policy import LEDGER_COLUMNS, FlowFact, replay_flow
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
    }
    boundary = {("flows", "history_only"), ("flows", "continued_from_flow_id")}
    if ("flows", "is_issue_owner") not in columns:
        raise ValueError("incomplete Flow schema: offline investigation required")
    obsolete = {
        "review_items",
        "review_pending_edits",
        "review_thread_observations",
        "feedback_reconcile",
        "flow_reconcile",
    }
    map_columns = {name for table, name in columns if table == "flow_attempt_legacy_map"}
    if "flowtool_schema_version" in names:
        versions = connection.execute("SELECT version FROM flowtool_schema_version").fetchall()
        if versions not in ([{"version": 2}], [{"version": 3}]):
            raise ValueError(f"unsupported Flow schema version: {versions}")
        if new <= columns and not (legacy & columns):
            if (
                versions == [{"version": 3}]
                and boundary <= columns
                and "flow_attempt_legacy_map" not in names
            ):
                require_current_schema(connection, relations)
                return "current"
            if (
                versions == [{"version": 2}]
                and FLOW_TABLES <= names
                and not (boundary & columns or obsolete & names)
                and map_columns == {"flow_id", "round_no", "attempt_id"}
            ):
                return "v2"
    elif (
        FLOW_TABLES <= names
        and legacy <= columns
        and not ((new | boundary) & columns)
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


def _verify_ledger(flow: Row, facts: list[Row], current: int) -> None:
    replay = replay_flow(
        flow["id"], [FlowFact.model_validate(f) for f in facts], task_content=flow["task_content"]
    )
    different = [
        col
        for col in LEDGER_COLUMNS
        if getattr(replay, col) != (current if col == "current_attempt_id" else flow[col])
    ]
    if different:
        raise ValueError(f"ledger projection differs: {different}")


def _inventory(c: Connection[Row]) -> Row:
    """Use observed IDs/versions, never choose between candidates or date a start by preparation."""
    conflicts: list[str] = []
    rounds: list[Row] = []
    for flow in c.execute("SELECT * FROM flows ORDER BY id"):
        fid, current = flow["id"], flow["current_round_no"]
        candidates: dict[int, list[Row]] = defaultdict(list)
        for a in c.execute(
            "SELECT id,round_no,created_at FROM attempts WHERE flow_id=%s ORDER BY id", (fid,)
        ):
            candidates[a["round_no"]].append(a)
        jobs = {
            j["round_no"]: j
            for j in c.execute(
                """
            SELECT round_no,min(id) FILTER(WHERE kind='initial') AS first_initial,
                min(created_at) AS first_job_at,bool_or(kind IN ('initial','revision') AND
                (started_at IS NOT NULL OR dispatch_authorized_at IS NOT NULL)) AS prepared
            FROM jobs WHERE flow_id=%s GROUP BY round_no""",
                (fid,),
            )
        }
        observed = {
            r["round_no"]
            for r in c.execute(
                "SELECT DISTINCT round_no FROM console_activity WHERE flow_id=%s AND round_no IS NOT NULL",
                (fid,),
            )
        }
        facts = c.execute(
            """SELECT version,round_no AS attempt_id,kind,occurred_at,metadata FROM console_activity
            WHERE flow_id=%s AND source='flow' AND kind NOT IN ('state','baseline','inference_exhausted')
            ORDER BY version""",
            (fid,),
        ).fetchall()
        starts: dict[int, list[Row]] = defaultdict(list)
        for fact in facts:
            n = fact["attempt_id"]
            if fact["kind"] in {"created", "snapshot", "round_resumed"}:
                starts[n].append(fact)
                if fact["kind"] == "snapshot":
                    if fact["metadata"].get("current_round_no") != n or n != 1:
                        conflicts.append(
                            f"flow {fid} snapshot v{fact['version']} lacks a proven start for round {n}"
                        )
                    fact["metadata"] = {**fact["metadata"], "current_attempt_id": n}
                elif fact["kind"] == "round_resumed":
                    fact["kind"] = "attempt_started"
            if n is None:
                conflicts.append(f"flow {fid} unowned ledger v{fact['version']}")
        if list(starts) != sorted(n for n in starts if n is not None):
            conflicts.append(
                f"flow {fid}: creation/resume facts out of legacy round order: {list(starts)}"
            )
        try:
            # Round labels are exact stand-ins solely for this read-only replay. Repeat with
            # the allocated, potentially nonmonotonic IDs before the cutover can commit.
            _verify_ledger(flow, facts, current)
        except (ValueError, KeyError) as error:
            conflicts.append(f"flow {fid}: {error}")
        labels = sorted(observed | candidates.keys() | jobs.keys() | {current})
        if len(labels) != current or labels[0] != 1 or labels[-1] != current:
            conflicts.append(f"flow {fid}: inconsistent round coverage {labels}, current={current}")
        for n in labels:
            evidence = starts[n]
            ids = [a["id"] for a in candidates[n]]
            job = jobs.get(n, {})
            boundary = evidence[0] if len(evidence) == 1 else None
            start = boundary["occurred_at"] if boundary else None
            if boundary and boundary["kind"] == "snapshot" and n == 1:
                start = flow["created_at"]  # Original creation, not the snapshot observation time.
            problems = []
            if not boundary or ((boundary["kind"] in ("created", "snapshot")) != (n == 1)):
                problems.append("missing or contradictory creation/resume boundary")
            if start and (
                (n == 1 and start != flow["created_at"])
                or any(a["created_at"] < start for a in candidates[n])
                or (job.get("first_job_at") and job["first_job_at"] < start)
            ):
                problems.append("timestamp contradicts start")
            if len(ids) > 1:
                problems.append("multiple attempts")
            # Old resume could reserve a branch before INITIAL prepared an Attempt.
            # A reset stub has no PR, base branch or head, and has state 'none'.
            prepared_delivery = current == n and (
                flow["delivery_state"] not in (None, "none")
                or any(
                    flow[col] is not None
                    for col in ("delivery_pr_number", "delivery_head_sha", "delivery_base_branch")
                )
            )
            if not ids and (job.get("prepared") or prepared_delivery):
                problems.append("prepared work without its attempt")
            if (
                current == n
                and flow["current_attempt_id"] is not None
                and flow["current_attempt_id"] not in ids
            ):
                problems.append(f"current pointer {flow['current_attempt_id']} belongs elsewhere")
            row = {
                "flow_id": fid,
                "round_no": n,
                "attempt_ids": ids,
                "start": start,
                "first_initial": job.get("first_initial"),
                "boundary_version": boundary["version"] if boundary else None,
                "history": [
                    (f["version"], f["kind"])
                    for f in facts
                    if f["attempt_id"] == n
                    and f["kind"] in ("archive", "merge", "archive_correction")
                ],
                "current_delivery": {
                    k: flow[k]
                    for k in (
                        "delivery_branch",
                        "delivery_pr_number",
                        "delivery_state",
                        "delivery_version",
                    )
                }
                if n == current and flow["delivery_branch"] is not None
                else None,
                "disposition": "conflict" if problems else ("preserve" if ids else "create"),
            }
            rounds.append(row)
            conflicts.extend(f"flow {fid} round {n} candidates={ids}: {p}" for p in problems)
    ownership = c.execute("""
        SELECT 'source job' AS source,j.id::text AS id FROM jobs j JOIN jobs p ON p.id=j.source_job_id
         WHERE (p.flow_id,p.round_no) IS DISTINCT FROM (j.flow_id,j.round_no)
        UNION ALL SELECT 'orphan attempt',a.id::text FROM attempts a WHERE NOT EXISTS
          (SELECT 1 FROM flows f WHERE f.id=a.flow_id)
        UNION ALL SELECT 'orphan job',j.id::text FROM jobs j WHERE j.flow_id IS NOT NULL AND NOT EXISTS
          (SELECT 1 FROM flows f WHERE f.id=j.flow_id)
        UNION ALL SELECT 'orphan activity',concat(s.flow_id,' v',s.version) FROM console_activity s WHERE NOT EXISTS
          (SELECT 1 FROM flows f WHERE f.id=s.flow_id)
        UNION ALL SELECT 'standalone job',id::text FROM jobs WHERE flow_id IS NULL AND kind<>'inference'
        UNION ALL SELECT 'inference job',j.id::text FROM jobs j LEFT JOIN attempts a
         ON a.id::text=j.inference->>'source_attempt_id' WHERE j.inference->>'source_attempt_id' IS NOT NULL
         AND (a.flow_id,a.round_no) IS DISTINCT FROM (j.flow_id,j.round_no)
        UNION ALL SELECT 'tool call',t.call_id FROM flow_tool_calls t JOIN jobs j ON j.id=t.job_id
         JOIN attempts a ON a.id=t.attempt_id WHERE a.flow_id<>t.flow_id OR
           (a.flow_id,a.round_no) IS DISTINCT FROM (j.flow_id,j.round_no)
        UNION ALL SELECT 'review batch',b.id::text FROM review_batches b JOIN attempts a ON a.id=b.attempt_id
         LEFT JOIN jobs j ON j.id IN (b.job_id,b.inference_job_id) WHERE a.flow_id<>b.flow_id OR
           (j.id IS NOT NULL AND (a.flow_id,a.round_no) IS DISTINCT FROM (j.flow_id,j.round_no))
        UNION ALL SELECT 'worker activity',concat(s.flow_id,' v',s.version) FROM console_activity s
          LEFT JOIN attempts a ON a.id::text=s.metadata->>'attempt_id' WHERE s.source='worker'
          AND (jsonb_typeof(s.metadata->'attempt_id') IS DISTINCT FROM 'number' OR
            a.flow_id IS DISTINCT FROM s.flow_id OR (s.round_no IS NOT NULL AND a.round_no<>s.round_no))
        UNION ALL SELECT 'activity key',concat(flow_id,':',key) FROM console_activity
          GROUP BY flow_id,key HAVING count(DISTINCT round_no)>1
        UNION ALL SELECT 'PR',concat(repo,'#',pr) FROM (
          SELECT lower(repo) AS repo,delivery_pr_number::text AS pr,id AS flow_id,current_round_no AS round_no
          FROM flows WHERE delivery_pr_number IS NOT NULL UNION ALL
          SELECT lower(metadata->>'repo'),metadata->>'pr_number',flow_id,round_no FROM console_activity
          WHERE source='flow' AND kind IN ('archive','merge','archive_correction')
          AND metadata->>'pr_number' IS NOT NULL) owners GROUP BY repo,pr
          HAVING count(DISTINCT (flow_id,round_no))>1
    """)
    conflicts.extend(f"{r['source']} {r['id']}: inconsistent owner" for r in ownership)
    counts = c.execute(
        "SELECT (SELECT count(*) FROM jobs WHERE flow_id IS NULL) AS standalone_jobs,"
        "(SELECT count(*) FROM console_activity WHERE round_no IS NULL AND source<>'worker')"
        " AS unattributed_activity"
    ).fetchone()
    assert counts
    return {"rounds": rounds, "conflicts": conflicts, **counts}


def _convert_rounds(c: Connection[Row], rounds: list[Row]) -> None:
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
    c.execute("""CREATE TEMP TABLE round_map(flow_id bigint,round_no integer,attempt_id bigint UNIQUE,
        PRIMARY KEY(flow_id,round_no)) ON COMMIT DROP""")
    for r in rounds:
        fid, n, start = r["flow_id"], r["round_no"], r["start"]
        if r["attempt_ids"]:
            (aid,) = r["attempt_ids"]
        else:
            created = c.execute(
                "INSERT INTO attempts(flow_id,round_no,created_at,updated_at) VALUES(%s,%s,%s,%s) RETURNING id",
                (fid, n, start, start),
            ).fetchone()
            assert created
            aid = created["id"]
        c.execute("INSERT INTO pg_temp.round_map VALUES(%s,%s,%s)", (fid, n, aid))
    c.execute("""
        UPDATE flows f SET current_attempt_id=m.attempt_id FROM pg_temp.round_map m
          WHERE (f.id,f.current_round_no)=(m.flow_id,m.round_no);
        UPDATE jobs j SET attempt_id=m.attempt_id FROM pg_temp.round_map m
          WHERE (j.flow_id,j.round_no)=(m.flow_id,m.round_no);
        UPDATE console_activity a SET attempt_id=m.attempt_id FROM pg_temp.round_map m
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


def _freeze(c: Connection[Row], status: str, rounds: list[Row]) -> None:
    # Do not let the old projection trigger publish a state transition for a change in authority.
    c.execute("DROP TRIGGER console_fact_changed ON flows")
    if status == "legacy":
        _convert_rounds(c, rounds)
    else:
        c.execute("DROP TABLE flow_attempt_legacy_map,flowtool_schema_version")
    c.execute("""ALTER TABLE flows ADD history_only boolean NOT NULL DEFAULT false,
        ADD continued_from_flow_id bigint REFERENCES flows(id),
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
        report: Row = {"status": status, "applied": False, "conflicts": [], "rounds": []}
        if status not in {"legacy", "v2"}:
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
        if status == "legacy":
            report.update(_inventory(c))
        else:
            for f in c.execute("SELECT * FROM flows ORDER BY id"):
                facts = c.execute(
                    """SELECT kind,attempt_id,occurred_at,metadata FROM console_activity
                    WHERE flow_id=%s AND source='flow' AND kind NOT IN ('state','baseline','inference_exhausted')
                    ORDER BY version""",
                    (f["id"],),
                ).fetchall()
                try:
                    _verify_ledger(f, facts, f["current_attempt_id"])
                except (ValueError, KeyError) as error:
                    report["conflicts"].append(f"flow {f['id']}: {error}")
        report["conflicts"].extend(_unsafe(c))
        if apply and not report["conflicts"]:
            _freeze(c, status, report["rounds"])
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
