"""Offline upgrade against PostgreSQL, including rollback and opaque receipt preservation."""

import hashlib
import json

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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
            ALTER TABLE flows DROP history_only,DROP continued_from_flow_id;
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
            INSERT INTO console_flows(flow_id,facts,activity_version) VALUES(7,'{}',7),(9,'{}',2);
            INSERT INTO console_activity(flow_id,round_no,version,key,source,kind,occurred_at,updated_at,
                      author,title,body,metadata,digest)
              SELECT 9,1,1,'snapshot','flow','snapshot',now(),now(),'a','t','',
                jsonb_build_object('current_round_no',1,'repo',repo,'issue_number',issue_number,
                  'task_digest',null,'state',state,'title',title,'created_at',created_at,'updated_at',updated_at,
                  'archive_reason',archive_reason,'archived_at',archived_at),'original'
              FROM flows WHERE id=9;
            INSERT INTO console_activity(flow_id,round_no,version,key,source,kind,occurred_at,updated_at,
                       author,title,body,metadata,digest,awaiting_agent)
              VALUES(9,1,2,'original-note','agent_task','message',now(),now(),'owner','t','keep this text','{}','digest',true);
            INSERT INTO console_activity(flow_id,round_no,version,key,source,kind,occurred_at,updated_at,
                       author,title,body,metadata,digest,parent_key)
              SELECT 7,r,v,k,src,kind,ts::timestamptz,ts::timestamptz,'a','t','original body',m::jsonb,'original',parent
              FROM (VALUES
                (1,1,'origin','flow','created','2026-01-01','{"repo":"o/r","issue_number":1,"task_digest":null}',null),
                (1,2,'finish','flow','finalization_requested','2026-01-02','{"archive_reason":"abandoned"}',null),
                (1,3,'archive','flow','archive','2026-01-02','{"repo":"o/r","archive_reason":"abandoned","pr_number":17}',null),
                (2,4,'reopen','flow','round_resumed','2026-01-03','{"state":"queued"}',null),
                (null,5,'shared','github','comment','2026-01-03','{"origin":{"id":345}}','archive'),
                (2,6,'finish2','flow','finalization_requested','2026-01-05','{"archive_reason":"abandoned"}',null),
                (2,7,'archive2','flow','archive','2026-01-05','{"archive_reason":"abandoned"}',null)
              ) t(r,v,k,src,kind,ts,m,parent);
            INSERT INTO jobs(id,flow_id,kind,state,round_no,created_at,updated_at)
              VALUES(10,7,'archive','done',1,'2026-01-02','2026-01-02'),
                    (11,7,'initial','failed',2,'2026-01-03','2026-01-03');
            INSERT INTO jobs(kind,state,inference,created_at,updated_at)
              VALUES ('inference','failed','{}',now(),now());
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
        notice = {
            "type": "merge_archive",
            "job_id": 10,
            "flow_id": 7,
            "round_no": 1,
            "repo": "o/r",
            "pr_number": 17,
            "merged_at": "2026-01-02T00:00:00Z",
            "merge_commit_sha": "sha",
        }
        digest = hashlib.sha256(
            json.dumps(notice, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        receipt = {"version": 2, "source": "control", "terminal": notice}
        c.execute("ALTER TABLE jobs DISABLE TRIGGER console_fact_changed")
        c.execute(
            "UPDATE jobs SET result_payload=%s,result_digest=%s,result_received_at=now(),result_applied_at=now() WHERE id=10",
            (Jsonb(receipt), digest),
        )
        c.execute("ALTER TABLE jobs ENABLE TRIGGER console_fact_changed")
        c.execute(
            "INSERT INTO result_applications VALUES(10,%s,%s,now())",
            (
                digest,
                Jsonb(
                    {
                        "job_id": 10,
                        "flow_id": 7,
                        "attempt_id": 42,
                        "digest": digest,
                        "state": "original opaque plan field; no live decoder accepts this",
                        "archive_notice": notice,
                        "delivery": {"flow_id": 7, "branch": "old-pr-branch", "pr_number": 17},
                    }
                ),
            ),
        )
    return postgresql_url


def test_upgrade_preserves_boundaries_and_receipts(legacy):
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
    assert [r["attempt_ids"] for r in report["rounds"]] == [[42], [20], []]
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
        assert c.execute("SELECT to_regclass('flow_attempt_legacy_map') AS m").fetchone() == {
            "m": None
        }
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
            for r in c.execute(
                "SELECT * FROM console_activity WHERE flow_id=7 AND version<=5 ORDER BY version"
            )
        ][-2:] == [("reopen", second["id"], None), ("shared", None, "archive")]
        assert [r["attempt_id"] for r in c.execute("SELECT attempt_id FROM jobs ORDER BY id")] == [
            None,
            42,
            second["id"],
        ]


@pytest.mark.ledger_unverified
@pytest.mark.parametrize(
    ("change", "evidence"),
    [
        ("""ALTER TABLE flows DISABLE TRIGGER console_fact_changed;
            UPDATE flows SET state='blocked',archived_at=NULL WHERE id=9;
            ALTER TABLE flows ENABLE TRIGGER console_fact_changed""", "flow 9: not archived (blocked)"),
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


def test_interrupted_cutover_rolls_back_data_and_ddl(legacy):
    # A local reporting view can block the final column removal *after* backfill and replay.
    with psycopg.connect(legacy) as c:
        c.execute("CREATE VIEW local_report AS SELECT round_no FROM attempts")
    with pytest.raises(psycopg.errors.DependentObjectsStillExist):
        migrate(legacy, apply=True)
    with psycopg.connect(legacy) as c:
        assert c.execute("SELECT count(*) FROM attempts").fetchone() == (2,)
        assert c.execute("SELECT kind FROM console_activity WHERE key='reopen'").fetchone() == (
            "round_resumed",
        )
        assert c.execute("SELECT to_regclass('flow_attempt_legacy_map')").fetchone() == (None,)
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


def test_v2_direct_task_cutover(postgresql_url):
    db = Database(postgresql_url)
    try:
        db.init_db()
        a = FlowStore(db).create_flow("o/r", None, None, task_content="original\ndirect task")
        ControlService(database=db).cancel(a)
        with db.connection() as c:
            c.execute("""DROP VIEW console_facts;
                ALTER TABLE flows DROP history_only,DROP continued_from_flow_id;
                CREATE VIEW console_facts AS SELECT id,repo FROM flows;
                CREATE TABLE flow_attempt_legacy_map(flow_id bigint,round_no integer,attempt_id bigint UNIQUE,
                    PRIMARY KEY(flow_id,round_no),FOREIGN KEY(flow_id,attempt_id) REFERENCES attempts(flow_id,id));
                DROP TABLE flowtool_schema_version;
                CREATE TABLE flowtool_schema_version(version integer PRIMARY KEY CHECK(version=2));
                INSERT INTO flowtool_schema_version VALUES(2);
                INSERT INTO flow_attempt_legacy_map SELECT id,1,current_attempt_id FROM flows""")
            # Do not mistake a partial/manual v3 upgrade for an unmarked historical cohort.
            c.execute("ALTER TABLE flows ADD continued_from_flow_id bigint")
        with pytest.raises(ValueError, match="partial"):
            migrate(postgresql_url, apply=True)
        with db.connection() as c:
            c.execute("ALTER TABLE flows DROP continued_from_flow_id")
        with pytest.raises(ValueError, match="offline"):
            db.init_db()
        assert migrate(postgresql_url)["status"] == "v2"
        assert migrate(postgresql_url, apply=True)["applied"]
        archived = FlowStore(db).get_flow(a.id)
        assert archived and archived.history_only and archived.state == "archived"
        assert (
            archived.task_content == a.task_content
            and archived.current_attempt_id == a.current_attempt_id
        )
        assert not migrate(postgresql_url, apply=True)["applied"]
    finally:
        db.close()
