"""One-shot, offline migration of the pre-round Flow schema (never a startup hook).

Stop ALL controllers, workers, webhook consumers and console writers first; a database
lock cannot fence a worker already making an external call. Take a database backup.
Run from the checkout using the new application environment::

    uv run python -m scripts.migrate_flow_states --database-url "$DATABASE_URL" --schema public
    uv run python -m scripts.migrate_flow_states --database-url "$DATABASE_URL" --schema public \
        --apply --expect-report <fingerprint-from-the-reviewed-dry-run>

Both invocations lock and inspect the whole selected schema. The second refuses changed
facts, any ambiguous owner, incomplete disposition, and EVERY unapplied ResultPlan.
There is no force option. Resolve evidence with the old deployment/operator runbook,
then stop it and repeat the dry-run. This tool performs no GitHub or worker operations.

Original rows are retained in flow_state_migration_backup, independently readable as
JSONB without runtime enums. Only applied plans' top-level flow_state is translated;
inboxes, digests, effects, events and existing console activity are never rewritten.
Archive timestamps denote this migration's proven archive boundary, not an invented
historical completion time. No execution binding is released by the migration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from app.contracts.protocol import ArchiveResult, ArchiveTask, Succeeded
from app.contracts.receipts import decode_result_inbox
from app.contracts.result_models import EffectRecord, ResultPlan
from app.persistence.database import Database
from app.persistence.schema import SCHEMA

Row = dict[str, Any]
REQUIRED_TABLES = {
    "flows",
    "attempts",
    "jobs",
    "workers",
    "flow_deliveries",
    "flow_executions",
    "execution_stops",
    "result_applications",
    "result_effects",
    "events",
    "console_flows",
    "console_activity",
    "review_batches",
}
BACKUP_TABLES = REQUIRED_TABLES - {"events", "console_activity"}
NEW_TABLES = {
    "flow_rounds",
    "flow_round_corrections",
    "issue_flow_owners",
    "flow_state_migration_backup",
}
OPERATOR_CHECKS = [
    "Keep all controllers, workers, webhook and console writers stopped until migration completes.",
    (
        "Reconcile pending results/plans and dispatched effects using their original receipts; "
        "never manufacture acknowledgements or discard history."
    ),
    "Verify stop fences, worker slots and workspace cleanup/retention/release evidence.",
    (
        "Resolve conflicting Issue owners and PR identities explicitly in the old operational "
        "workflow; failed Flows remain owners (blocked), not archived."
    ),
    (
        "An old closed PR is not abandonment consent; an old display-only console archive "
        "is not workspace disposition evidence. Review business intent separately."
    ),
]


@dataclass
class Report:
    """Machine-readable review artifact; refusals include every affected Flow/job ID."""

    fingerprint: str = ""
    status: str = "ready"
    flows: list[Row] = field(default_factory=list)
    owners: list[Row] = field(default_factory=list)
    plan_conversions: list[Row] = field(default_factory=list)
    problems: list[Row] = field(default_factory=list)

    def refuse(self, message: str, *, flows: Sequence[int] = (), jobs: Sequence[int] = ()) -> None:
        self.status = "refused"
        self.problems.append(
            {"flow_ids": sorted(flows), "job_ids": sorted(jobs), "message": message}
        )

    def as_dict(self) -> Row:
        return {**vars(self), "operator_checks": OPERATOR_CHECKS}


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _terminal(job: Row):
    return decode_result_inbox(
        job["result_payload"],
        job_id=job["id"],
        assignment_token=job["assignment_token"],
        result_digest=job["result_digest"],
    ).terminal


def _archive_proof(
    flow: Row, job: Row, plan: Row, delivery: Row | None, execution: Row | None
) -> bool:
    """Validate cleanup against the original command, result digest and released binding."""
    if not isinstance(plan, dict) or not delivery or not execution or not execution["released_at"]:
        return False
    if (
        job["kind"] != "archive"
        or job["state"] != "done"
        or not job["started_at"]
        or not job["result_applied_at"]
        or not job["result_received_at"]
    ):
        return False
    try:
        result = ArchiveResult.model_validate(plan.get("archive"))
        if not isinstance(job["payload"], dict):
            return False
        frozen = ArchiveTask.model_validate((job["payload"] or {}).get("archive"))
        expected = ArchiveTask(
            flow_id=flow["id"],
            attempt_id=flow["current_attempt_id"],
            repo=flow["repo"],
            worker_id=execution["worker_id"],
            repo_identity=execution["repo_identity"],
            delivery_version=delivery["version"],
        )
        terminal = _terminal(job)
        return (
            frozen == expected
            and ArchiveTask.model_validate(result.model_dump(exclude={"cleanup"})) == frozen
            and isinstance(terminal, Succeeded)
            and terminal.result == result
            and job["assigned_worker_id"] == execution["worker_id"]
            and execution["repo_identity"] == f"{execution['worker_id']}:{flow['repo']}"
            and _time(execution["bound_at"])
            <= _time(job["started_at"])
            <= _time(job["result_received_at"])
            <= _time(execution["released_at"])
            # The old _commit released BEFORE complete_persisted_result, in the
            # same transaction. Reversing that order would reject valid archives.
            and _time(job["result_received_at"]) <= _time(job["result_applied_at"])
        )
    except (ValueError, TypeError):
        return False


def _merged(delivery: Row | None) -> bool:
    return bool(
        delivery
        and delivery["state"] == "merged"
        and delivery["pr_number"]
        and delivery["merged_at"]
        and (delivery["merge_commit_sha"] or "").strip()
    )


def _check_archive(
    flow: Row,
    jobs: list[Row],
    stops: list[Row],
    effects: list[Row],
    plans: dict[int, Row],
    delivery: Row | None,
    execution: Row | None,
    workers: list[Row],
    report: Report,
) -> bool:
    """Terminal labels alone prove neither quiescence nor a workspace disposition."""
    before = len(report.problems)

    def reject(message: str, ids: Sequence[int] = ()) -> None:
        report.refuse(message, flows=[flow["id"]], jobs=ids)

    unsettled = [j["id"] for j in jobs if j["state"] in {"queued", "running"}]
    if unsettled:
        reject("Archive refused: queued/running jobs must settle first.", unsettled)
    by_id = {j["id"]: j for j in jobs}
    unconfirmed = [
        s
        for s in stops
        if s["state"] != "confirmed"
        or not s["confirmed_at"]
        or len(s["receipt_digest"] or "") != 64
        or any(c not in "0123456789abcdef" for c in (s["receipt_digest"] or ""))
        or s["job_id"] not in by_id
        or s["assignment_token"] != by_id[s["job_id"]]["assignment_token"]
        or s["worker_id"] != by_id[s["job_id"]]["assigned_worker_id"]
        or not execution
        or s["binding_at"] != execution["bound_at"]
        or _time(s["confirmed_at"]) < _time(s["requested_at"])
    ]
    if unconfirmed:
        reject(
            "Archive refused: unconfirmed stop or missing confirmation receipt: "
            + ", ".join(s["request_id"] for s in unconfirmed),
            [s["job_id"] for s in unconfirmed],
        )
    unresolved = [
        e
        for e in effects
        if e["state"] not in {"succeeded", "skipped"}
        or e["claimed_by"] is not None
        or (e["dispatched_at"] is not None and e["receipt"] is None)
    ]
    if unresolved:
        reject(
            "Archive refused: effects need confirmed receipts/settlement: "
            + ", ".join(e["effect_key"] for e in unresolved),
            sorted({e["job_id"] for e in unresolved}),
        )
    job_ids = {j["id"] for j in jobs}
    if any(w["current_job_id"] in job_ids for w in workers):
        reject("Archive refused: a worker still holds a Flow job slot.")
    if execution and not execution["released_at"]:
        reject("Archive refused: workspace binding has not been released; verify disposition.")
    if not execution and any(j["assigned_worker_id"] for j in jobs):
        reject("Archive refused: assigned work has no workspace disposition record.")
    for job in jobs:
        if not job["started_at"] and not job["dispatch_authorized_at"]:
            continue
        # A dispatch may have executed even when Started was lost. Absence of a
        # result/stop is not evidence of a stopped executor, even for 'failed'.
        stop = next(
            (
                s
                for s in stops
                if s["job_id"] == job["id"]
                and s not in unconfirmed
                and execution
                and s["worker_id"] == job["assigned_worker_id"] == execution["worker_id"]
                and s["assignment_token"] == job["assignment_token"]
                and s["binding_at"] == execution["bound_at"]
            ),
            None,
        )
        if not execution:
            reject(
                "Archive refused: dispatched job has no workspace disposition record.", [job["id"]]
            )
        elif (
            job["assigned_worker_id"] != execution["worker_id"]
            or execution["repo_identity"] != f"{execution['worker_id']}:{flow['repo']}"
        ):
            reject(
                "Archive refused: released workspace does not match the dispatched assignment.",
                [job["id"]],
            )
        elif not job["result_applied_at"] and stop is None:
            reject(
                "Archive refused: dispatched job has neither applied result nor confirmed stop.",
                [job["id"]],
            )
        if (
            execution
            and execution["released_at"]
            and job["result_received_at"]
            and (_time(job["result_received_at"]) > _time(execution["released_at"]))
        ):
            reject("Archive refused: workspace was released before result receipt.", [job["id"]])
        if (
            stop
            and execution
            and execution["released_at"]
            and (_time(stop["confirmed_at"]) > _time(execution["released_at"]))
        ):
            reject("Archive refused: workspace was released before stop confirmation.", [job["id"]])
    if flow["state"] == "succeeded":
        if not _merged(delivery) or not any(
            _archive_proof(flow, j, plans.get(j["id"], {}), delivery, execution) for j in jobs
        ):
            reject(
                "Legacy succeeded needs a complete merged delivery and an applied, "
                "digest-verified archive receipt matching the frozen command and released workspace."
            )
    elif delivery and delivery["state"] in {"merged", "opening"}:
        reject(
            "Legacy cancelled has a merged/uncertain opening delivery; reconcile its outcome first."
        )
    return len(report.problems) == before


def _active_state(
    flow: Row, jobs: list[Row], delivery: Row | None, execution: Row | None
) -> tuple[str, str | None, str]:
    """Classify persisted work, never reconstruct a stage from console display phases."""
    live = [j for j in jobs if j["state"] in {"queued", "running"}]
    if delivery and delivery["state"] == "merged":
        archives = [j for j in live if j["kind"] == "archive"]
        if (
            _merged(delivery)
            and len(archives) == 1
            and execution
            and execution["released_at"] is None
        ):
            return (
                "finalizing",
                "succeeded",
                "Verified merge, retained original binding and archive job",
            )
        raise ValueError(
            "Merged active Flow needs complete merge facts, original binding and one archive job"
        )
    if delivery and delivery["state"] == "closed":
        raise ValueError(
            "Closed delivery does not prove abandonment consent; verify operator intent"
        )
    if len(live) == 1 and live[0]["kind"] in {"initial", "revision"}:
        job = live[0]
        if job["state"] == "queued" and not any(
            job[key]
            for key in (
                "started_at",
                "dispatch_authorized_at",
                "result_payload",
                "assigned_worker_id",
                "assignment_token",
            )
        ):
            return "queued", None, f"Undispatched queued job {job['id']}"
        if (
            job["state"] == "running"
            and job["assignment_token"]
            and execution
            and execution["released_at"] is None
            and execution["worker_id"] == job["assigned_worker_id"]
        ):
            return "running", None, f"Running assigned job {job['id']} on retained binding"
    if not live and delivery and delivery["state"] == "open" and delivery["pr_number"]:
        last = max(jobs, key=lambda j: j["id"], default=None)
        if (
            last
            and last["kind"] in {"initial", "revision"}
            and last["state"] == "done"
            and last["result_applied_at"]
            and execution
            and not execution["released_at"]
        ):
            return "review", None, "Open delivery and settled coding result on retained binding"
    raise ValueError(
        "Active stage is ambiguous: inspect queued/running jobs, binding and verified delivery"
    )


def _preflight(data: dict[str, list[Row]], report: Report) -> dict[int, Row]:
    """Collect all blockers before deciding whether any row may be written."""
    jobs_by_flow: dict[int, list[Row]] = defaultdict(list)
    jobs = {j["id"]: j for j in data["jobs"]}
    deliveries = {d["flow_id"]: d for d in data["flow_deliveries"]}
    executions = {x["flow_id"]: x for x in data["flow_executions"]}
    attempts = {a["id"]: a for a in data["attempts"]}
    plans = {p["job_id"]: p["plan"] for p in data["result_applications"]}
    for job in jobs.values():
        ids = [job["flow_id"]] if job["flow_id"] is not None else []
        if job["kind"] != "inference":
            jobs_by_flow[job["flow_id"]].append(job)
        if job["kind"] not in {"initial", "revision", "archive", "inference"} or (
            job["state"] not in {"queued", "running", "done", "failed"}
        ):
            report.refuse(
                "Unknown job kind/state; verify its historical contract.",
                flows=ids,
                jobs=[job["id"]],
            )
        if job["state"] == "done" and not job["result_applied_at"]:
            report.refuse(
                "Done job lacks its applied result boundary.", flows=ids, jobs=[job["id"]]
            )
        if job["result_applied_at"] and job["state"] not in {"done", "failed"}:
            report.refuse("Applied result still has a live job state.", flows=ids, jobs=[job["id"]])
        if (job["result_received_at"] or job["result_payload"] is not None) and not job[
            "result_applied_at"
        ]:
            report.refuse(
                "Pending result: apply/reconcile it under the old deployment before migration.",
                flows=ids,
                jobs=[job["id"]],
            )
        if job["result_applied_at"]:
            try:
                if (
                    not job["result_received_at"]
                    or not job["result_payload"]
                    or not job["result_digest"]
                ):
                    raise ValueError("incomplete applied inbox")
                # Inference has a separate inbox contract, containing no FlowState.
                if job["kind"] != "inference":
                    terminal = _terminal(job)
                    if isinstance(terminal, Succeeded):
                        if terminal.result.kind != job["kind"]:
                            raise ValueError("applied result kind differs from job")
                    elif job["state"] == "done":
                        raise ValueError("failed terminal cannot prove a done job")
            except (ValueError, TypeError):
                report.refuse(
                    "Applied result has invalid/missing inbox identity or digest.",
                    flows=ids,
                    jobs=[job["id"]],
                )
    flows = {f["id"]: f for f in data["flows"]}
    decisions = {}
    for flow in flows.values():
        identity = flow["id"]
        decision = {
            "id": identity,
            "old_state": flow["state"],
            "new_state": None,
            "archive_reason": None,
            "evidence": None,
        }
        decisions[identity] = decision
        report.flows.append(decision)
        flow_jobs = jobs_by_flow[identity]
        delivery, execution = deliveries.get(identity), executions.get(identity)
        attempt = attempts.get(flow["current_attempt_id"])
        if flow["current_attempt_id"] is not None and (
            not attempt or attempt["flow_id"] != identity
        ):
            report.refuse("Current attempt belongs to another/missing Flow.", flows=[identity])
        if delivery and delivery["state"] not in {"none", "opening", "open", "closed", "merged"}:
            report.refuse("Unknown delivery state.", flows=[identity])
        if execution and execution["repo_identity"] != f"{execution['worker_id']}:{flow['repo']}":
            report.refuse(
                "Workspace binding does not identify this worker/repository.", flows=[identity]
            )
        if flow["state"] == "failed":
            decision.update(
                new_state="blocked",
                evidence="Legacy failure; retain binding and Issue ownership",
                archive_reason="succeeded" if _merged(delivery) else None,
            )
        elif flow["state"] == "active":
            try:
                if not attempt:
                    raise ValueError(
                        "Active Flow lacks a current attempt; verify execution identity"
                    )
                if not _merged(delivery) and any(
                    s["flow_id"] == identity and s["state"] != "confirmed"
                    for s in data["execution_stops"]
                ):
                    raise ValueError(
                        "Active coding stage conflicts with an unconfirmed stop; verify intent"
                    )
                state, reason, evidence = _active_state(flow, flow_jobs, delivery, execution)
                decision.update(new_state=state, archive_reason=reason, evidence=evidence)
            except ValueError as error:
                report.refuse(str(error), flows=[identity], jobs=[j["id"] for j in flow_jobs])
        elif flow["state"] in {"cancelled", "succeeded"}:
            stops = [s for s in data["execution_stops"] if s["flow_id"] == identity]
            effects = [
                e for e in data["result_effects"] if jobs[e["job_id"]]["flow_id"] == identity
            ]
            if _check_archive(
                flow, flow_jobs, stops, effects, plans, delivery, execution, data["workers"], report
            ):
                decision.update(
                    new_state="archived",
                    archive_reason="succeeded" if flow["state"] == "succeeded" else "abandoned",
                    evidence="Settled results/stops/effects and proven workspace disposition",
                )
        else:
            report.refuse(
                "Not a supported legacy Flow state; partial/new migrations are not accepted.",
                flows=[identity],
            )

    issues: dict[tuple[str, int], list[Row]] = defaultdict(list)
    prs: dict[tuple[str, int], list[int]] = defaultdict(list)
    for flow in flows.values():
        issues[flow["repo"], flow["issue_number"]].append(flow)
        delivery = deliveries.get(flow["id"])
        if delivery and delivery["pr_number"] is not None:
            prs[flow["repo"], delivery["pr_number"]].append(flow["id"])
    for (repo, number), members in issues.items():
        outstanding = [f for f in members if decisions[f["id"]]["new_state"] != "archived"]
        newest_time = max(_time(f["created_at"]) for f in members)
        newest = [f for f in members if _time(f["created_at"]) == newest_time]
        if len(outstanding) > 1 or len(newest) != 1 or (outstanding and outstanding != newest):
            report.refuse(
                f"Ambiguous Issue owner for {repo}#{number}; no winner selected.",
                flows=[f["id"] for f in members],
            )
        else:
            report.owners.append({"repo": repo, "issue_number": number, "flow_id": newest[0]["id"]})
    for (repo, number), identities in prs.items():
        if len(identities) > 1:
            report.refuse(
                f"Duplicate delivery identity {repo} PR #{number}; verify ownership.",
                flows=identities,
            )

    converted = {}
    for application in data["result_applications"]:
        job = jobs[application["job_id"]]
        identity = job["flow_id"]
        ids = [identity] if identity is not None else []
        if not job["result_applied_at"]:
            report.refuse(
                "Unapplied ResultPlan must be reconciled with the old deployment; not replayed/translated.",
                flows=ids,
                jobs=[job["id"]],
            )
            continue
        raw = application["plan"]
        if not isinstance(raw, dict):
            report.refuse("ResultPlan is not an object.", flows=ids, jobs=[job["id"]])
            continue
        old_state = raw.get("flow_state")
        if old_state is not None and not isinstance(old_state, str):
            report.refuse(
                "ResultPlan flow_state is not a string/null.", flows=ids, jobs=[job["id"]]
            )
            continue
        new_state = old_state
        if old_state == "failed":
            new_state = "blocked"
        elif old_state in {"succeeded", "cancelled"}:
            decision = decisions.get(identity, {})
            if decision.get("new_state") != "archived" or decision.get("archive_reason") != (
                "succeeded" if old_state == "succeeded" else "abandoned"
            ):
                report.refuse(
                    "Historical terminal plan lacks proven matching archive/disposition evidence.",
                    flows=ids,
                    jobs=[job["id"]],
                )
                continue
            if old_state == "succeeded" and not _archive_proof(
                flows[identity], job, raw, deliveries.get(identity), executions.get(identity)
            ):
                report.refuse(
                    "Historical succeeded plan lacks its own verified archive receipt.",
                    flows=ids,
                    jobs=[job["id"]],
                )
                continue
            new_state = "archived"
        elif old_state == "active":
            # This enum was not emitted by the old result engine. Current Flow facts
            # cannot establish the stage of a historical plan after later executions.
            report.refuse(
                "Historical active plan has no unambiguous stage at application time; "
                "verify its original frozen intent (current stage is not evidence).",
                flows=ids,
                jobs=[job["id"]],
            )
            continue
        candidate = {**raw, "flow_state": new_state} if old_state != new_state else raw
        try:
            plan = ResultPlan.model_validate(candidate)
            if (
                plan.job_id != job["id"]
                or plan.flow_id != identity
                or plan.digest != job["result_digest"]
                or plan.digest != application["digest"]
                or (
                    plan.attempt_id is not None
                    and (
                        plan.attempt_id not in attempts
                        or attempts[plan.attempt_id]["flow_id"] != identity
                    )
                )
            ):
                raise ValueError("plan ownership/digest mismatch")
            records = sorted(
                (e for e in data["result_effects"] if e["job_id"] == job["id"]),
                key=lambda e: e["position"],
            )
            if len(records) != len(plan.effects):
                raise ValueError("missing/extra materialized effects")
            for position, (effect, raw_record) in enumerate(
                zip(plan.effects, records, strict=True)
            ):
                record = EffectRecord.model_validate(raw_record)
                if (
                    record.position != position
                    or record.effect_key != effect.key
                    or record.payload != effect
                ):
                    raise ValueError("materialized effect differs from frozen plan")
        except (ValueError, TypeError):
            report.refuse(
                "ResultPlan fails the new reader or its job/Flow/attempt/digest/effects identity.",
                flows=ids,
                jobs=[job["id"]],
            )
            continue
        if old_state != new_state:
            converted[job["id"]] = candidate
            report.plan_conversions.append(
                {"job_id": job["id"], "from": old_state, "to": new_state}
            )
    return converted


def _apply(tx: Database, report: Report, converted: dict[int, Row]) -> None:
    """Commit evidence backups, authority, DDL and console replacement on one connection."""
    with tx.connection() as connection:
        connection.execute("""
            CREATE TABLE flow_state_migration_backup (
              table_name TEXT NOT NULL, row_data JSONB NOT NULL,
              report_fingerprint TEXT NOT NULL, captured_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        for table in sorted(BACKUP_TABLES):
            connection.execute(
                sql.SQL(
                    "INSERT INTO flow_state_migration_backup(table_name,row_data,report_fingerprint) "
                    "SELECT %s,to_jsonb(t),%s FROM {} t"
                ).format(sql.Identifier(table)),
                (table, report.fingerprint),
            )
        # Never DROP ... CASCADE or rebuild console tables: activity is append-only.
        for table in (
            "flows",
            "jobs",
            "attempts",
            "flow_deliveries",
            "flow_executions",
            "result_effects",
        ):
            connection.execute(
                sql.SQL("DROP TRIGGER IF EXISTS console_fact_changed ON {}").format(
                    sql.Identifier(table)
                )
            )
        connection.execute("DROP VIEW console_facts")
        connection.execute("""
            DROP INDEX ux_flows_active;
            ALTER TABLE flows
              ADD COLUMN current_round_no INTEGER NOT NULL DEFAULT 1 CHECK(current_round_no>0),
              ADD COLUMN archive_reason TEXT CHECK(archive_reason IN ('succeeded','abandoned')),
              ADD COLUMN archive_summary TEXT,
              ADD COLUMN archived_at TIMESTAMPTZ;
            ALTER TABLE attempts ADD COLUMN round_no INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE jobs ADD COLUMN round_no INTEGER NOT NULL DEFAULT 1;
        """)
        for change in report.flows:
            connection.execute(
                "UPDATE flows SET state=%s,archive_reason=%s,"
                " archived_at=CASE WHEN %s='archived' THEN now() END WHERE id=%s",
                (change["new_state"], change["archive_reason"], change["new_state"], change["id"]),
            )
        # Canonical table definitions (including round/owner tables and the new unique
        # index), after state conversion and before restoring projection triggers.
        connection.execute(SCHEMA)
        connection.execute("""
            DO $$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='flows'::regclass
                             AND conname='ck_flows_state') THEN
                ALTER TABLE flows ADD CONSTRAINT ck_flows_state CHECK
                  (state IN ('queued','running','review','blocked','finalizing','archived'));
              END IF;
            END $$;
            ALTER TABLE flows
              ADD CONSTRAINT ck_flows_archive_time CHECK ((state='archived') = (archived_at IS NOT NULL)),
              ADD CONSTRAINT ck_flows_archive_reason CHECK
                (state NOT IN ('finalizing','archived') OR archive_reason IS NOT NULL),
              ADD CONSTRAINT ck_flows_archive_stage CHECK
                (archive_reason IS NULL OR state IN ('blocked','finalizing','archived'));
            INSERT INTO flow_rounds(flow_id,round_no,repo,pr_number,original_reason,archive_reason,
                                   archive_summary,archived_at,delivery,execution,merged_at,merge_commit_sha)
              SELECT f.id,1,f.repo,d.pr_number,f.archive_reason,f.archive_reason,
                     f.archive_summary,f.archived_at,to_jsonb(d),to_jsonb(x),d.merged_at,d.merge_commit_sha
              FROM flows f LEFT JOIN flow_deliveries d ON d.flow_id=f.id
              LEFT JOIN flow_executions x ON x.flow_id=f.id WHERE f.state='archived';
        """)
        for owner in report.owners:
            connection.execute(
                "INSERT INTO issue_flow_owners(repo,issue_number,flow_id) VALUES (%s,%s,%s)",
                (owner["repo"], owner["issue_number"], owner["flow_id"]),
            )
        for job_id, plan in converted.items():
            connection.execute(
                "UPDATE result_applications SET plan=%s WHERE job_id=%s", (Jsonb(plan), job_id)
            )
        # Uses the bound transaction; failure here also restores the old view, index,
        # triggers, states and plans. init_db's refresh appends, never erases activity.
        tx.init_db()


def migrate(
    database_url: str,
    *,
    schema: str | None = None,
    apply: bool = False,
    expected_fingerprint: str | None = None,
) -> Report:
    """Dry-run by default; --apply requires the unchanged, previously reviewed report."""
    report = Report()
    database = Database(database_url)
    try:
        with database.transaction() as tx, tx.connection() as connection:
            if schema is None:
                row = connection.execute("SELECT current_schema() AS name").fetchone()
                assert row is not None
                schema = row["name"]
            if not schema or schema.startswith("pg_") or schema == "information_schema":
                report.refuse("Select an existing application schema explicitly.")
                return report
            if (
                connection.execute(
                    "SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)
                ).fetchone()
                is None
            ):
                report.refuse("Select an existing application schema explicitly.")
                return report
            # No accidental fallback into public/another schema when a table is absent.
            connection.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema))
            )
            tables = [
                r["tablename"]
                for r in connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename",
                    (schema,),
                ).fetchall()
            ]
            if not REQUIRED_TABLES <= set(tables) or NEW_TABLES & set(tables):
                report.refuse(
                    "Expected the complete legacy schema, without new/partially migrated tables. "
                    f"Missing: {sorted(REQUIRED_TABLES - set(tables))}; "
                    f"already present: {sorted(NEW_TABLES & set(tables))}"
                )
                return report
            # NOWAIT refuses live database activity rather than queueing behind it.
            connection.execute(
                sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE NOWAIT").format(
                    sql.SQL(",").join(sql.Identifier(t) for t in tables)
                )
            )
            columns = connection.execute(
                "SELECT table_name,column_name,data_type,is_nullable,column_default "
                "FROM information_schema.columns WHERE table_schema=%s ORDER BY table_name,ordinal_position",
                (schema,),
            ).fetchall()
            if any(
                c["table_name"] == "flows"
                and c["column_name"]
                in {
                    "current_round_no",
                    "archive_reason",
                    "archive_summary",
                    "archived_at",
                }
                or c["table_name"] in {"jobs", "attempts"}
                and c["column_name"] == "round_no"
                for c in columns
            ):
                report.refuse("New columns already exist; refuse partial/unknown schema migration.")
                return report
            data = {
                table: [
                    r["data"]
                    for r in connection.execute(
                        sql.SQL(
                            "SELECT to_jsonb(t) AS data FROM {} t ORDER BY to_jsonb(t)::text"
                        ).format(sql.Identifier(table))
                    ).fetchall()
                ]
                for table in tables
            }
            definitions = connection.execute(
                "SELECT 'index' AS kind,indexname AS name,indexdef AS definition FROM pg_indexes WHERE schemaname=%s "
                "UNION ALL SELECT 'view',viewname,definition FROM pg_views WHERE schemaname=%s "
                "UNION ALL SELECT 'constraint',conname,pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname=%s "
                "UNION ALL SELECT 'function',p.proname,pg_get_functiondef(p.oid) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=%s AND p.prokind='f' "
                "UNION ALL SELECT 'trigger',t.tgname,pg_get_triggerdef(t.oid) FROM pg_trigger t "
                "JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND NOT t.tgisinternal ORDER BY kind,name,definition",
                (schema,) * 5,
            ).fetchall()
            fingerprint_input = {
                "version": 1,
                "schema": schema,
                "columns": columns,
                "definitions": definitions,
                "data": data,
            }
            report.fingerprint = hashlib.sha256(
                json.dumps(
                    fingerprint_input,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            converted = _preflight(data, report)
            if apply and expected_fingerprint != report.fingerprint:
                report.refuse(
                    "--apply requires --expect-report from an unchanged, reviewed dry-run. "
                    "Run dry-run again; do not reuse a stale report."
                )
            if apply and report.status == "ready":
                _apply(tx, report, converted)
                report.status = "applied"
        return report
    finally:
        database.close()


def main(argv: list[str] | None = None) -> int:
    """Emit JSON and nonzero status on refusals; never infer a production URL from env."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--schema", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="default; no persistent changes")
    mode.add_argument("--apply", action="store_true", help="all producers must remain stopped")
    parser.add_argument("--expect-report", help="fingerprint from the previously reviewed dry-run")
    args = parser.parse_args(argv)
    try:
        report = migrate(
            args.database_url,
            schema=args.schema,
            apply=args.apply,
            expected_fingerprint=args.expect_report,
        )
    except psycopg.Error as error:
        # SQL failures roll back, but a lost connection during COMMIT has an unknown
        # outcome. Never encourage blind retry or print connection credentials.
        print(
            json.dumps(
                {
                    "status": "refused",
                    "sqlstate": error.sqlstate,
                    "message": "Database operation failed. SQL errors roll back atomically; "
                    "a connection failure during commit can have an unknown outcome. Inspect "
                    "the schema and migration backup before retrying; keep producers stopped.",
                    "operator_checks": OPERATOR_CHECKS,
                }
            )
        )
        return 2
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 2 if report.status == "refused" else 0


if __name__ == "__main__":
    raise SystemExit(main())
