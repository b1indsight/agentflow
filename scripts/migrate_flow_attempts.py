"""Offline v1 (unmarked round ledger) → v2 migration. Run as python -m scripts.migrate_flow_attempts."""

import argparse
import hashlib
import json
from collections import defaultdict

import psycopg
from psycopg import Connection, sql
from psycopg.rows import dict_row

from app.contracts.protocol import InferenceResult
from app.contracts.receipts import MergeArchiveNotice, decode_result_inbox
from app.contracts.result_models import ResultPlan
from app.domain.flow_policy import LEDGER_COLUMNS, FlowFact, replay_flow
from app.persistence.database import flow_schema_version, mark_flow_schema_current
from app.persistence.records import Row
from app.persistence.schema import SCHEMAS


def _receipts(c: Connection[Row], conflicts: list[str]) -> list[Row]:
    """Verify original-format digests before interpreting the round or frozen plan."""
    receipts = []
    for j in c.execute("""
        SELECT j.id,j.kind,j.flow_id,j.round_no,j.result_payload,j.result_digest,j.assignment_token,
               r.digest,r.plan,a.id AS candidate,f.repo FROM jobs j LEFT JOIN result_applications r ON r.job_id=j.id
               LEFT JOIN flows f ON f.id=j.flow_id
               LEFT JOIN attempts a ON a.flow_id=j.flow_id AND a.round_no=j.round_no
        WHERE j.result_payload IS NOT NULL OR j.result_digest IS NOT NULL OR r.job_id IS NOT NULL
        ORDER BY j.id
        """):
        receipts.append(
            {"job_id": j["id"], "digest": j["result_digest"], "frozen_plan": j["plan"] is not None}
        )
        try:
            payload = j["result_payload"]
            if not isinstance(payload, dict) or not isinstance(j["result_digest"], str):
                conflicts.append(f"job {j['id']}: missing receipt or digest")
                continue
            if j["kind"] == "inference":
                result = InferenceResult.model_validate(payload)
                if (
                    result.job_id != j["id"]
                    or result.assignment_token != j["assignment_token"]
                    or hashlib.sha256(result.model_dump_json().encode()).hexdigest()
                    != j["result_digest"]
                ):
                    raise ValueError("inference receipt owner or digest mismatch")
            else:
                inbox = decode_result_inbox(
                    payload,
                    job_id=j["id"],
                    assignment_token=j["assignment_token"],
                    result_digest=j["result_digest"],
                )
                if isinstance(inbox.terminal, MergeArchiveNotice) and (
                    inbox.version != 2
                    or j["kind"] != "archive"
                    or inbox.terminal.repo != j["repo"]
                    or (inbox.terminal.flow_id, inbox.terminal.round_no)
                    != (j["flow_id"], j["round_no"])
                ):
                    raise ValueError("merge receipt owner mismatch")
            plan = j["plan"]
            if plan is not None and (
                not isinstance(plan, dict)
                or plan.get("job_id") != j["id"]
                or plan.get("flow_id") != j["flow_id"]
                or plan.get("digest") != j["result_digest"]
                or j["digest"] != j["result_digest"]
                or (plan.get("attempt_id") is not None and plan["attempt_id"] != j["candidate"])
                or (
                    plan.get("archive_notice") is not None
                    and plan["archive_notice"] != payload.get("terminal")
                )
            ):
                raise ValueError("frozen plan owner, receipt or digest mismatch")
            if plan is not None:
                ResultPlan.from_legacy(plan, flow_id=j["flow_id"], attempt_id=j["candidate"])
        except ValueError as error:
            conflicts.append(f"job {j['id']} receipt/plan: {error}")
    return receipts


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
    receipts = _receipts(c, conflicts)
    counts = c.execute(
        "SELECT (SELECT count(*) FROM jobs WHERE flow_id IS NULL) AS standalone_jobs,"
        "(SELECT count(*) FROM console_activity WHERE round_no IS NULL AND source<>'worker')"
        " AS unattributed_activity"
    ).fetchone()
    assert counts
    return {"rounds": rounds, "receipt_jobs": receipts, "conflicts": conflicts, **counts}


def _apply(c: Connection[Row], rounds: list[Row]) -> None:
    """No projections during backfill: reinstall canonical triggers/view after their inputs switch."""
    for table in ("flows", "attempts", "jobs"):
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
    mark_flow_schema_current(c)
    for r in rounds:
        fid, n, start = r["flow_id"], r["round_no"], r["start"]
        if r["attempt_ids"]:
            (aid,) = r["attempt_ids"]
            c.execute("UPDATE attempts SET created_at=%s WHERE id=%s", (start, aid))
        else:
            created = c.execute(
                "INSERT INTO attempts(flow_id,round_no,created_at,updated_at) VALUES(%s,%s,%s,%s) RETURNING id",
                (fid, n, start, start),
            ).fetchone()
            assert created
            aid = created["id"]
        c.execute("INSERT INTO flow_attempt_legacy_map VALUES(%s,%s,%s)", (fid, n, aid))
    c.execute("""
        UPDATE flows f SET current_attempt_id=m.attempt_id FROM flow_attempt_legacy_map m
          WHERE (f.id,f.current_round_no)=(m.flow_id,m.round_no);
        UPDATE jobs j SET attempt_id=m.attempt_id FROM flow_attempt_legacy_map m
          WHERE (j.flow_id,j.round_no)=(m.flow_id,m.round_no);
        UPDATE console_activity a SET attempt_id=m.attempt_id,
          kind=CASE WHEN source='flow' AND kind='round_resumed' THEN 'attempt_started' ELSE kind END,
          metadata=CASE WHEN source='flow' AND kind='snapshot' THEN
              (metadata-'current_round_no')||jsonb_build_object('current_attempt_id',m.attempt_id)
            ELSE metadata END
          FROM flow_attempt_legacy_map m WHERE (a.flow_id,a.round_no)=(m.flow_id,m.round_no);
        UPDATE console_activity s SET attempt_id=a.id FROM attempts a WHERE s.source='worker'
          AND s.round_no IS NULL AND s.flow_id=a.flow_id AND s.metadata->>'attempt_id'=a.id::text;
        ALTER TABLE jobs ADD FOREIGN KEY(flow_id,attempt_id) REFERENCES attempts(flow_id,id) MATCH FULL;
        """)
    names = sql.SQL(",").join(sql.Identifier(col["attname"]) for col in columns)
    c.execute(
        sql.SQL(
            "UPDATE attempts a SET ({})=(SELECT {} FROM flows WHERE id=a.flow_id)"
            " FROM flows f WHERE f.current_attempt_id=a.id"
        ).format(names, names)
    )
    for flow in c.execute("SELECT * FROM flows ORDER BY id"):
        facts = c.execute(
            """SELECT kind,attempt_id,occurred_at,metadata FROM console_activity
            WHERE flow_id=%s AND source='flow' AND kind NOT IN ('state','baseline','inference_exhausted')
            ORDER BY version""",
            (flow["id"],),
        ).fetchall()
        _verify_ledger(flow, facts, flow["current_attempt_id"])
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
    for schema in SCHEMAS:
        c.execute(schema)


def migrate(database_url: str, *, apply: bool = False) -> Row:
    """Inspect a consistent read-only snapshot by default; --apply rechecks under offline locks."""
    with psycopg.connect(database_url, row_factory=dict_row) as c:
        if not apply:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        status = flow_schema_version(c)
        report: Row = {"status": status, "applied": False, "conflicts": [], "rounds": []}
        if status != "legacy":
            return report
        if apply:
            c.execute(
                "LOCK flows,attempts,jobs,console_activity,console_flows,flow_tool_calls,"
                "result_applications,review_batches IN ACCESS EXCLUSIVE MODE NOWAIT"
            )
        report.update(_inventory(c))
        if apply and not report["conflicts"]:
            _apply(c, report["rounds"])
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
