"""Offline upgrade against PostgreSQL, including rollback and opaque receipt preservation."""

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.contracts.protocol import Failed, terminal_digest
from app.controler.workflow.service import ControlService
from app.persistence.database import Database
from app.persistence.flows import FlowStore
from scripts.migrate_flow_attempts import migrate


@pytest.fixture
def legacy(postgresql_url):
    # Start with the real unrelated tables. Restore the previous shapes only for the
    # four versioned tables; exercise real DDL dependencies, FKs and immutable evidence.
    db = Database(postgresql_url)
    try:
        db.init_db()
    finally:
        db.close()
    with psycopg.connect(postgresql_url, row_factory=dict_row) as c:
        c.execute("""
            DROP VIEW console_facts;
            DROP TABLE flowtool_schema_version;
            ALTER TABLE flows DROP history_only;
            DROP TRIGGER preserve_job_owner ON jobs;
            DROP TRIGGER require_selected_attempt ON flows;
            ALTER TABLE flows ADD current_round_no integer NOT NULL DEFAULT 1;
            ALTER TABLE attempts ADD round_no integer NOT NULL DEFAULT 1;
            ALTER TABLE jobs DROP attempt_id, ADD round_no integer NOT NULL DEFAULT 1;
            ALTER TABLE console_activity DROP attempt_id, ADD round_no integer;
            ALTER TABLE flows DROP CONSTRAINT fk_flows_current_attempt;
            ALTER TABLE attempts DROP CONSTRAINT attempts_flow_id_id_key;
            ALTER TABLE flows ADD CONSTRAINT fk_flows_current_attempt FOREIGN KEY(current_attempt_id) REFERENCES attempts(id);
            DO $$ DECLARE col record; complete text; BEGIN
              SELECT pg_get_constraintdef(oid) INTO STRICT complete FROM pg_constraint
                WHERE conrelid='attempts'::regclass AND conname='attempts_delivery_complete';
              FOR col IN SELECT attname,format_type(atttypid,atttypmod) AS typ FROM pg_attribute
                WHERE attrelid='attempts'::regclass AND attname LIKE 'delivery_%' AND NOT attisdropped LOOP
                EXECUTE format('ALTER TABLE flows ADD %I %s',col.attname,col.typ);
                EXECUTE format('ALTER TABLE attempts DROP %I',col.attname);
              END LOOP;
              EXECUTE 'ALTER TABLE flows ADD CONSTRAINT flows_delivery_complete '||complete;
            END $$;
            ALTER TABLE flows DISABLE TRIGGER console_fact_changed;
            ALTER TABLE jobs DISABLE TRIGGER console_fact_changed;
            ALTER TABLE attempts DISABLE TRIGGER console_fact_changed;
            INSERT INTO flows(id,repo,issue_number,state,current_round_no,created_at,updated_at,
                delivery_branch,delivery_state,delivery_created_at,delivery_updated_at,delivery_version,archived_at,archive_reason)
              VALUES(7,'o/r',1,'archived',2,'2026-01-01','2026-01-05','existing-branch','none',now(),now(),3,'2026-01-05','abandoned');
            INSERT INTO attempts(id,flow_id,round_no,created_at,updated_at,codex_session_id)
              VALUES(42,7,1,'2026-01-02','2026-01-02','old-session'),(20,7,2,'2026-01-04','2026-01-04',null);
            UPDATE flows SET current_attempt_id=20 WHERE id=7;
            INSERT INTO flows(id,repo,issue_number,state,is_issue_owner,title,created_at,updated_at,archived_at,archive_reason)
              VALUES(9,'o/r',2,'archived',true,'original title','2026-01-01','2026-01-01','2026-01-01','abandoned');
            INSERT INTO console_flows(flow_id,facts,activity_version) VALUES(7,'{}',3),(9,'{}',1);
            INSERT INTO console_activity(flow_id,round_no,version,key,source,kind,occurred_at,updated_at,
                       author,title,body,metadata,digest,awaiting_agent)
              VALUES(9,1,1,'original-note','agent_task','message',now(),now(),'owner','t','keep this text','{}','digest',true);
            INSERT INTO console_activity(flow_id,round_no,version,key,source,kind,occurred_at,updated_at,
                       author,title,body,metadata,digest,parent_key)
              SELECT 7,r,v,k,src,kind,ts::timestamptz,ts::timestamptz,'a','t','original body',m::jsonb,'original',parent
              FROM (VALUES
                (1,1,'archive','flow','archive','2026-01-02','{"pr_number":17}',null),
                (2,2,'reopen','flow','round_resumed','2026-01-03','{"state":"queued"}',null),
                (null,3,'shared','github','comment','2026-01-03','{"origin":{"id":345}}','archive')
              ) t(r,v,k,src,kind,ts,m,parent);
            INSERT INTO jobs(id,flow_id,kind,state,round_no,created_at,updated_at)
              VALUES(10,7,'archive','done',1,'2026-01-02','2026-01-02'),
                    (11,7,'initial','failed',2,'2026-01-03','2026-01-03');
            INSERT INTO jobs(kind,state,inference,created_at,updated_at)
              VALUES ('inference','failed','{}',now(),now());
            UPDATE jobs SET result_payload='{"old":"receipt"}',result_digest='original',
              result_received_at=now(),result_applied_at=now() WHERE id=10;
            INSERT INTO result_applications VALUES(10,'original','{"old":"plan"}',now());
            INSERT INTO console_sync(flow_id,source,pending,dirty,claim_token,claim_until,status,limitations,reason)
              VALUES(9,'worker',true,true,'stale',now(),'syncing','["old gap"]','unreachable'),
                    (9,'worker:old',false,false,NULL,NULL,'partial','["old gap"]','unreachable');
            INSERT INTO reconcile_checks(flow_id,source,next_at) VALUES(9,'pull_request',now());
            INSERT INTO project_projections(repo,issue_number,project_id,flow_id,config_key,desired_status,next_at)
              VALUES('o/r',2,'project',9,'v','blocked',now());
            INSERT INTO review_buffers VALUES(9,now());
            ALTER TABLE flows ENABLE TRIGGER console_fact_changed;
            ALTER TABLE jobs ENABLE TRIGGER console_fact_changed;
            ALTER TABLE attempts ENABLE TRIGGER console_fact_changed;
            CREATE VIEW console_facts AS SELECT id,current_round_no,delivery_branch FROM flows;
            """)
    return postgresql_url


def test_upgrade_preserves_archived_history(legacy):
    db = Database(legacy)
    try:
        with pytest.raises(ValueError, match="offline"):
            db.init_db()
    finally:
        db.close()
    with psycopg.connect(legacy, row_factory=dict_row) as c:
        history_query = "SELECT flow_id,version,key,kind,body,metadata,digest FROM console_activity ORDER BY flow_id,version"
        history = c.execute(history_query).fetchall()
        original = c.execute(
            "SELECT result_payload::text,result_digest,plan::text FROM jobs"
            " JOIN result_applications ON jobs.id=job_id"
        ).fetchall()
    report = migrate(legacy)
    assert not report["conflicts"]
    assert migrate(legacy, apply=True)["applied"]
    assert migrate(legacy, apply=True)["status"] == "current"
    with psycopg.connect(legacy, row_factory=dict_row) as c:
        attempts = c.execute(
            "SELECT a.*,f.current_attempt_id FROM attempts a JOIN flows f ON f.id=a.flow_id"
            " WHERE a.flow_id=7 ORDER BY a.created_at"
        ).fetchall()
        first, second = attempts
        assert first["id"] == 42 and first["created_at"].day == 2 and not first["delivery_branch"]
        assert second["created_at"].day == 4 and second["id"] == second["current_attempt_id"]
        assert second["delivery_branch"] == "existing-branch"
        assert c.execute(history_query).fetchall() == history
        assert not c.execute(
            "SELECT id FROM flows WHERE NOT history_only OR is_issue_owner"
        ).fetchall()
        assert c.execute("SELECT state,title FROM flows WHERE id=9").fetchone() == {
            "state": "archived",
            "title": "original title",
        }
        assert not c.execute("""SELECT flow_id FROM console_sync WHERE pending OR dirty OR claim_token IS NOT NULL
            OR claim_until IS NOT NULL UNION ALL SELECT flow_id FROM project_projections UNION ALL SELECT
            flow_id FROM review_buffers WHERE due_at IS NOT NULL UNION ALL SELECT flow_id FROM reconcile_checks
            UNION ALL SELECT flow_id FROM console_activity WHERE awaiting_agent""").fetchall()
        assert c.execute(
            "SELECT status,limitations,reason FROM console_sync WHERE source='worker:old'"
        ).fetchone() == {"status": "partial", "limitations": ["old gap"], "reason": "unreachable"}
        assert (
            c.execute(
                "SELECT result_payload::text,result_digest,plan::text FROM jobs"
                " JOIN result_applications ON jobs.id=job_id"
            ).fetchall()
            == original
        )
        assert [
            (r["key"], r["attempt_id"], r["parent_key"])
            for r in c.execute("SELECT * FROM console_activity WHERE flow_id=7 ORDER BY version")
        ] == [("archive", 42, None), ("reopen", second["id"], None), ("shared", None, "archive")]
        assert [r["attempt_id"] for r in c.execute("SELECT attempt_id FROM jobs ORDER BY id")] == [
            None,
            42,
            second["id"],
        ]


@pytest.mark.ledger_unverified
@pytest.mark.parametrize(
    ("change", "evidence"),
    [
        (
            """ALTER TABLE flows DISABLE TRIGGER console_fact_changed;
            UPDATE flows SET state='blocked',archived_at=NULL WHERE id=9;
            ALTER TABLE flows ENABLE TRIGGER console_fact_changed""",
            "flow 9: not archived (blocked)",
        ),
        (
            (
                "ALTER TABLE attempts DISABLE TRIGGER console_fact_changed;"
                "INSERT INTO attempts(flow_id,round_no,created_at,updated_at) VALUES(7,1,now(),now());"
                "ALTER TABLE attempts ENABLE TRIGGER console_fact_changed"
            ),
            "multiple attempts",
        ),
        (
            (
                "ALTER TABLE jobs DISABLE TRIGGER console_fact_changed;"
                "UPDATE jobs SET result_digest='mismatch' WHERE id=10;"
                "ALTER TABLE jobs ENABLE TRIGGER console_fact_changed"
            ),
            "result application 10",
        ),
    ],
)
def test_conflict_is_reported_without_partial_upgrade(legacy, change, evidence):
    with psycopg.connect(legacy) as c:
        c.execute(change)
    assert any(evidence in item for item in migrate(legacy)["conflicts"])
    assert not migrate(legacy, apply=True)["applied"]
    with psycopg.connect(legacy) as c:
        assert c.execute(
            "SELECT current_round_no,current_attempt_id FROM flows WHERE id=7"
        ).fetchone() == (2, 20)
        assert c.execute("SELECT to_regclass('flowtool_schema_version')").fetchone() == (None,)


@pytest.mark.ledger_unverified
@pytest.mark.parametrize(
    ("version", "epoch", "tampered", "accepted"),
    [
        (14, "old-worker", False, True),
        (15, "old-worker", False, True),
        (14, "control", False, False),
        (15, "old-worker", True, False),
    ],
)
def test_cutover_validates_legacy_worker_receipt(legacy, version, epoch, tampered, accepted):
    terminal = Failed(job_id=11, assignment_token="old-token", error="failed")
    receipt = dict(
        terminal.model_dump(mode="json"), protocol_version=version, connection_epoch=epoch
    )
    with psycopg.connect(legacy) as c:
        c.execute("ALTER TABLE jobs DISABLE TRIGGER console_fact_changed")
        c.execute(
            """UPDATE jobs SET started_at=now(),assignment_token=%s,result_payload=%s,
            result_digest=%s,result_received_at=now(),result_applied_at=now() WHERE id=11""",
            (
                terminal.assignment_token,
                Jsonb(receipt),
                "wrong" if tampered else terminal_digest(terminal),
            ),
        )
        c.execute("ALTER TABLE jobs ENABLE TRIGGER console_fact_changed")
    assert migrate(legacy)["conflicts"] == ([] if accepted else ["job 11: not settled"])
    assert migrate(legacy, apply=True)["applied"] == accepted


def test_interrupted_cutover_rolls_back_data_and_ddl(legacy):
    # A local reporting view can block the final column removal after backfill.
    with psycopg.connect(legacy) as c:
        c.execute("CREATE VIEW local_report AS SELECT round_no FROM attempts")
    with pytest.raises(psycopg.errors.DependentObjectsStillExist):
        migrate(legacy, apply=True)
    with psycopg.connect(legacy) as c:
        assert c.execute("SELECT count(*) FROM attempts").fetchone() == (2,)
        assert c.execute("SELECT kind FROM console_activity WHERE key='reopen'").fetchone() == (
            "round_resumed",
        )
        c.execute("DROP VIEW local_report")
    assert migrate(legacy, apply=True)["applied"]


@pytest.mark.ledger_unverified  # The failed cutover deliberately leaves the round schema in place.
def test_preflight_rechecks_unsafe_work_under_lock(legacy):
    assert not migrate(legacy)["conflicts"]
    with psycopg.connect(legacy) as c:
        c.execute("LOCK result_effects IN ROW EXCLUSIVE MODE")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            migrate(legacy, apply=True)
    # The read-only report does not authorize writes: new work appeared before cutover.
    with psycopg.connect(legacy) as c:
        c.execute("""ALTER TABLE jobs DISABLE TRIGGER console_fact_changed;
            UPDATE jobs SET state='queued' WHERE id=11;
            ALTER TABLE jobs ENABLE TRIGGER console_fact_changed;
            ALTER TABLE flow_executions DISABLE TRIGGER console_fact_changed;
            INSERT INTO flow_executions VALUES(7,'w',repeat('a',32),1,'o/r',now(),NULL,NULL);
            ALTER TABLE flow_executions ENABLE TRIGGER console_fact_changed;
            INSERT INTO execution_stops(request_id,flow_id,job_id,worker_id,workspace,generation,
                assignment_token,binding_at,requested_at) VALUES('stop',7,11,'w',repeat('a',32),1,'t',now(),now());
            ALTER TABLE result_effects DISABLE TRIGGER console_fact_changed;
            INSERT INTO result_effects(job_id,effect_key,position,payload,state,dispatched_at,next_attempt_at)
              VALUES(10,'send',1,'{}','failed',now(),now());
            ALTER TABLE result_effects ENABLE TRIGGER console_fact_changed""")
    report = migrate(legacy, apply=True)
    assert not report["applied"] and set(report["conflicts"]) == {
        "binding 7: not settled",
        "job 11: not settled",
        "stop stop: not settled",
        "effect 10:send: not settled",
    }
    with psycopg.connect(legacy) as c:
        assert c.execute("SELECT is_issue_owner FROM flows WHERE id=9").fetchone() == (True,)
        assert c.execute(
            "SELECT claim_token FROM console_sync WHERE source='worker'"
        ).fetchone() == ("stale",)


def test_cutover_does_not_freeze_later_archives(legacy):
    assert migrate(legacy, apply=True)["applied"]
    db = Database(legacy)
    try:
        archived = FlowStore(db).get_flow(7)
        assert archived and archived.history_only and archived.state == "archived"
        service = ControlService(database=db)
        new = service.create_flow(archived.repo, None, None, task_content="new task")
        assert new and new.id != archived.id
        service.cancel(new)
        assert not migrate(legacy, apply=True)["applied"]
        reopened = service.resume(new, expected_attempt_id=new.required_attempt_id)
        assert reopened.id == new.id and reopened.required_attempt_id != new.required_attempt_id
        assert FlowStore(db).get_flow(archived.id) == archived
    finally:
        db.close()
