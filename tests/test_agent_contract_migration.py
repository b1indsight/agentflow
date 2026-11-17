"""Upgrade fixed v26 database JSON, not legacy runtime models or live worker traffic."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from threading import Barrier

import pytest
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.contracts.protocol import AgentResult
from app.domain.inference_models import InferenceJob
from app.domain.models import CodingExecutionResult, Job, JobKind, JobState, WorkerRecord
from app.persistence.database import Database
from app.persistence.records import Row, required
from app.persistence.store import Store
from tests.flow_fixtures import enqueue_job

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
DEADLINE = "2026-09-01T12:01:30Z"
MESSAGES = [
    {"role": "system", "content": "保持角色与原文。"},
    {"role": "assistant", "content": "Earlier answer\n"},
    {"role": "user", "content": "  Follow-up 🦉\n"},
]
OLD_TASK: Row = {
    "version": 1, "request_id": "1" * 32, "messages": MESSAGES,
    "output_schema": {"type": "object"}, "max_output_bytes": 4096, "timeout_seconds": 90.0,
}
OLD_CALL: Row = {
    "task": OLD_TASK, "current_task": None, "deadline_at": DEADLINE,
    "validation": "text", "batch_id": None, "summary": None, "consumed": False,
    "cancel_failure": None, "failures": [{"code": "busy", "retry_after_seconds": 4.5}],
    "avoid_workers": ["old-worker"], "excluded_workers": ["lost-worker"],
    "next_attempt_at": "2026-09-01T12:00:05Z", "assignment_epoch": "persisted-epoch",
    "backend": "codex/v1", "source_attempt_id": 18, "context_digest": "context-evidence",
}
OLD_CAPABILITY = {
    "adapter_version": "codex/v1", "formats": ["text", "json"],
    "max_input_bytes": 128_000, "max_output_bytes": 64_000,
}


def _old_index(store: Store) -> None:
    with store.connection() as connection:
        connection.execute(
            "DROP INDEX ux_jobs_inference_request;"
            " CREATE UNIQUE INDEX ux_jobs_inference_request"
            " ON jobs ((inference->'task'->>'request_id')) WHERE kind='inference'"
        )


def _insert_old_job(
    store: Store, number: int, *, state: str = "queued", outcome: str | None = None,
    backend: str = "codex/v1",
) -> int:
    call = deepcopy(OLD_CALL)
    call["task"]["request_id"] = f"{number:032x}"
    call["backend"] = backend
    if state != "queued":
        call["current_task"] = {**call["task"], "request_id": f"{number + 100:032x}",
                                "timeout_seconds": 17.25}
    if state == "failed":
        call["summary"] = {
            "logical_call_id": call["task"]["request_id"], "attempts": 3, "max_attempts": 3,
            "stop_reason": "max_attempts", "last_failure_code": "execution_timeout",
            "last_worker_id": "codex-worker", "backend": backend,
            "finished_at": "2026-09-01T12:00:40Z", "elapsed_ms": 40_000,
        }
    with store.connection() as connection:
        row = required(connection.execute(
            "INSERT INTO jobs(kind,state,inference,attempt,assigned_worker_id,workspace,generation,"
            " assignment_token,started_at,dispatch_authorized_at,lease_expires_at,recovery_deadline,"
            " heartbeat_at,prepare_failures,verify_failures,created_at,updated_at)"
            " VALUES ('inference',%s,%s,%s,'codex-worker',%s,7,'persisted-token',%s,%s,%s,%s,%s,2,1,%s,%s)"
            " RETURNING id",
            (state, Jsonb(call), 3 if state == "failed" else 2, "a" * 32,
             NOW, NOW, NOW, NOW, NOW, NOW, NOW),
        ).fetchone())
        job_id = row["id"]
        if outcome is not None:
            task = call["current_task"]
            task_json = json.dumps(task, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            payload = {
                "type": "tool_result", "message_id": f"{number + 200:032x}",
                "reply_to": task["request_id"], "method": "worker.infer",
                "job_id": job_id, "assignment_token": "persisted-token",
                "output": {
                    "request_id": task["request_id"],
                    "request_digest": hashlib.sha256(task_json.encode()).hexdigest(),
                    "text": '{"answer":"历史 🦉"}', "adapter_version": backend, "model": "old-model",
                } if outcome == "success" else None,
                "failure": {"code": "execution_timeout", "retry_after_seconds": 2.0}
                if outcome == "failure" else None,
            }
            old_digest = hashlib.sha256(json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"),
            ).encode()).hexdigest()
            connection.execute(
                "UPDATE jobs SET result_payload=%s,result_digest=%s,result_received_at=%s,"
                " result_applied_at=%s WHERE id=%s",
                (Jsonb(payload), old_digest, NOW, NOW if state in ("done", "failed") else None, job_id),
            )
    return job_id


def _insert_worker(store: Store, name: str, capability: object, *, pending: int | None = None) -> None:
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO workers(worker_id,workspace,generation,protocol_version,status,inference,"
            " connection_epoch,current_inference_job_id,first_connected_at,last_connected_at,last_seen_at)"
            " VALUES (%s,%s,7,26,'online',%s,'persisted-epoch',%s,%s,%s,%s)",
            (name, "a" * 32, Jsonb(capability), pending, NOW, NOW, NOW),
        )


def _snapshot(store: Store) -> tuple[list[Row], list[Row], Row]:
    with store.connection() as connection:
        return (
            connection.execute("SELECT *,xmin::text AS row_version FROM jobs ORDER BY id").fetchall(),
            connection.execute(
                "SELECT *,xmin::text AS row_version FROM workers ORDER BY worker_id,workspace"
            ).fetchall(),
            required(connection.execute(
                "SELECT 'ux_jobs_inference_request'::regclass::oid AS oid,"
                " pg_get_indexdef('ux_jobs_inference_request'::regclass) AS definition"
            ).fetchone()),
        )


def test_initialize_upgrades_all_durable_states_without_replenishing_work(store: Store) -> None:
    # Accepted but unapplied receipts must survive a restart, including their exact RPC fences.
    _old_index(store)
    queued = _insert_old_job(store, 1)
    _insert_old_job(store, 2, state="running", backend="amp/v1")
    pending = _insert_old_job(store, 3, state="running", outcome="success")
    _insert_old_job(store, 4, state="done", outcome="success", backend="bub-0.4.1/v1")
    _insert_old_job(store, 5, state="failed", outcome="failure")
    _insert_worker(store, "codex-worker", OLD_CAPABILITY, pending=pending)
    _insert_worker(store, "amp-worker", {**OLD_CAPABILITY, "adapter_version": "amp/v1"})
    _insert_worker(store, "bub-worker", {**OLD_CAPABILITY, "adapter_version": "bub-0.4.1/v1"})
    _insert_worker(store, "json-null-worker", None)
    _insert_worker(store, "sql-null-worker", None)
    _insert_worker(store, "current-worker", "codex")
    current = store.inference.enqueue_inference("Already current", request_id="f" * 32)
    flow = store.flows.create_flow("owner/coding-control", 1, None)
    coding = enqueue_job(store, flow.id, JobKind.INITIAL, payload={"keep": "coding work"})
    new_receipt = AgentResult(
        type="tool_result", message_id="d" * 32, reply_to="e" * 32, method="worker.start",
        job_id=current.id, assignment_token="new-token",
        result=CodingExecutionResult(None, "Already current", 0, backend="codex"),
    )
    with store.connection() as connection:
        connection.execute("UPDATE workers SET inference=NULL WHERE worker_id='sql-null-worker'")
        connection.execute(
            "UPDATE jobs SET result_payload=%s,result_digest=%s WHERE id=%s",
            (Jsonb(new_receipt.model_dump(mode="json")), "existing-receipt-must-not-be-rehashed", current.id),
        )
        connection.execute("UPDATE jobs SET source_job_id=%s,source_key='answer' WHERE id=%s", (queued, pending))
    before_jobs, before_workers, old_index = _snapshot(store)

    store.init_db()

    after_jobs, after_workers, index = _snapshot(store)
    assert "'task'" not in index["definition"] and index["oid"] != old_index["oid"]
    changed = {"inference", "result_payload", "result_digest", "row_version"}
    for before, after in zip(before_jobs, after_jobs, strict=True):
        assert {k: v for k, v in before.items() if k not in changed} == {
            k: v for k, v in after.items() if k not in changed
        }
        job = Job.model_validate(after)
        if job.id in (current.id, coding.id):
            assert after == before
            continue
        call = required(job.inference)
        old_call = before["inference"]
        assert call.request_id == old_call["task"]["request_id"]
        assert call.current_request_id == (
            old_call["current_task"]["request_id"] if old_call["current_task"] else None
        )
        assert call.task == json.dumps(MESSAGES, ensure_ascii=False, separators=(",", ":"))
        assert call.deadline_at == datetime(2026, 9, 1, 12, 1, 30, tzinfo=UTC)
        assert call.timeout_seconds == 90 and call.max_output_bytes == 4096
        assert call.model is None and call.effort is None
        assert call.output_schema == OLD_TASK["output_schema"]
        assert {k: v for k, v in old_call.items() if k not in ("task", "current_task", "backend")} == {
            k: v for k, v in after["inference"].items()
            if k not in ("task", "request_id", "current_request_id", "output_schema", "max_output_bytes",
                         "timeout_seconds", "model", "effort", "backend")
        }
        assert call.backend == {"codex/v1": "codex", "amp/v1": "amp", "bub-0.4.1/v1": "bub"}[old_call["backend"]]
        if before["result_payload"] is not None:
            receipt = AgentResult.model_validate(job.result_payload)
            assert job.result_digest == hashlib.sha256(receipt.model_dump_json().encode()).hexdigest()
            for key in ("type", "message_id", "reply_to", "job_id", "assignment_token", "failure"):
                assert after["result_payload"][key] == before["result_payload"][key]
            assert "output" not in after["result_payload"]
            if receipt.result is not None:
                assert receipt.result == CodingExecutionResult(
                    None, '{"answer":"历史 🦉"}', 0, backend=call.backend, model="old-model",
                )
    expected_workers = {"codex-worker": "codex", "amp-worker": "amp", "bub-worker": "bub",
                        "json-null-worker": None, "sql-null-worker": None, "current-worker": "codex"}
    for before, after in zip(before_workers, after_workers, strict=True):
        assert WorkerRecord.model_validate(after).inference == expected_workers[after["worker_id"]]
        assert {k: v for k, v in before.items() if k not in ("inference", "row_version")} == {
            k: v for k, v in after.items() if k not in ("inference", "row_version")
        }
    with store.connection() as connection:
        assert connection.execute("SELECT 1 FROM workers WHERE inference='null'::jsonb").fetchone() is None

    store.init_db()
    assert _snapshot(store) == (after_jobs, after_workers, index)
    with pytest.raises(UniqueViolation), store.connection() as connection:
        connection.execute(
            "INSERT INTO jobs(kind,state,inference,created_at,updated_at)"
            " SELECT kind,state,inference,created_at,updated_at FROM jobs WHERE id=%s", (queued,),
        )
    # The rewritten digest/backend must satisfy the actual receipt application path.
    assert store.inference.complete_inference_result(pending)
    assert required(store.jobs.get_job(pending)).state is JobState.DONE


@pytest.mark.parametrize("corruption", [
    "task", "current", "hybrid", "output", "outcomes", "failure", "method", "receipt", "worker", "duplicate",
])
def test_invalid_legacy_data_rolls_back_every_rewrite_and_index(store: Store, corruption: str) -> None:
    _old_index(store)
    first = _insert_old_job(store, 1)
    bad = _insert_old_job(store, 2, state="running", outcome="success")
    _insert_worker(store, "codex-worker", OLD_CAPABILITY)
    with store.connection() as connection:
        if corruption in ("task", "current"):
            path = "{task,messages}" if corruption == "task" else "{current_task,request_id}"
            value = [] if corruption == "task" else "not-a-request-id"
            connection.execute("UPDATE jobs SET inference=jsonb_set(inference,%s,%s) WHERE id=%s",
                               (path, Jsonb(value), bad))
        elif corruption == "hybrid":
            connection.execute("UPDATE jobs SET inference=inference || %s WHERE id=%s",
                               (Jsonb({"request_id": "f" * 32}), bad))
        elif corruption in ("output", "outcomes", "failure"):
            if corruption == "output":
                connection.execute("UPDATE jobs SET result_payload=result_payload #- '{output,text}' WHERE id=%s", (bad,))
            else:
                failure = {"code": "busy" if corruption == "outcomes" else "not-a-failure"}
                connection.execute(
                    "UPDATE jobs SET result_payload=jsonb_set(result_payload,'{failure}',%s) WHERE id=%s",
                    (Jsonb(failure), bad),
                )
        elif corruption == "method":
            connection.execute(
                "UPDATE jobs SET result_payload=jsonb_set(result_payload,'{method}',%s) WHERE id=%s",
                (Jsonb("worker.unknown"), bad),
            )
        elif corruption == "receipt":
            connection.execute("UPDATE jobs SET result_payload='{}'::jsonb WHERE id=%s", (bad,))
        elif corruption == "worker":
            connection.execute("UPDATE workers SET inference='{}'::jsonb")
        else:
            flat = InferenceJob(task="Duplicate logical call", request_id=f"{1:032x}", deadline_at=NOW)
            connection.execute(
                "INSERT INTO jobs(kind,state,inference,created_at,updated_at) VALUES ('inference','queued',%s,%s,%s)",
                (Jsonb(flat.model_dump(mode="json")), NOW, NOW),
            )
    before = _snapshot(store)
    with pytest.raises((ValueError, UniqueViolation)):
        store.init_db()
    assert _snapshot(store) == before
    assert before[0][0]["id"] == first


@pytest.mark.parametrize("existing", [False, True])
def test_concurrent_initializers_serialize_fresh_schema_and_upgrade(postgresql_url: str, existing: bool) -> None:
    with closing(Database(postgresql_url)) as left, closing(Database(postgresql_url)) as right:
        if existing:
            with closing(Store(postgresql_url)) as store:
                store.init_db()
                _old_index(store)
                _insert_old_job(store, 1)
        barrier = Barrier(2)

        def initialize(database: Database) -> None:
            barrier.wait(timeout=10)
            database.init_db()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(initialize, database) for database in (left, right)]
            for future in futures:
                future.result(timeout=30)
        with left.connection() as connection:
            assert connection.execute(
                "SELECT 1 FROM jobs WHERE jsonb_typeof(inference->'task')='object'"
            ).fetchone() is None
