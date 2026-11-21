"""Offline upgrade against PostgreSQL, including rollback and opaque receipt preservation."""

import hashlib
import json

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.github.console import Observation
from app.persistence.activity import ActivityStore
from app.persistence.database import Database
from app.persistence.results import ResultStore
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
            DROP TABLE flow_attempt_legacy_map;
            DROP TABLE flowtool_schema_version;
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
                delivery_branch,delivery_state,delivery_created_at,delivery_updated_at,delivery_version)
              VALUES(7,'o/r',1,'queued',2,'2026-01-01','2026-01-03','existing-branch','none',now(),now(),3);
            INSERT INTO attempts(id,flow_id,round_no,created_at,updated_at,codex_session_id)
              VALUES(42,7,1,'2026-01-02','2026-01-02','old-session'),(20,7,2,'2026-01-04','2026-01-04',null);
            UPDATE flows SET current_attempt_id=20 WHERE id=7;
            INSERT INTO flows(id,repo,issue_number,state,created_at,updated_at) VALUES(9,'o/r',2,'queued','2026-01-01','2026-01-01');
            INSERT INTO console_flows(flow_id,facts,activity_version) VALUES(7,'{}',5),(9,'{}',1);
            INSERT INTO console_activity(flow_id,round_no,version,key,source,kind,occurred_at,updated_at,
                      author,title,body,metadata,digest)
              SELECT 9,1,1,'snapshot','flow','snapshot',now(),now(),'a','t','',
                jsonb_build_object('current_round_no',1,'repo',repo,'issue_number',issue_number,
                  'task_digest',null,'state',state,'created_at',created_at,'updated_at',updated_at),'original'
              FROM flows WHERE id=9;
            INSERT INTO console_activity(flow_id,round_no,version,key,source,kind,occurred_at,updated_at,
                       author,title,body,metadata,digest,parent_key)
              SELECT 7,r,v,k,src,kind,ts::timestamptz,ts::timestamptz,'a','t','',m::jsonb,'original',parent
              FROM (VALUES
                (1,1,'origin','flow','created','2026-01-01','{"repo":"o/r","issue_number":1,"task_digest":null}',null),
                (1,2,'finish','flow','finalization_requested','2026-01-02','{"archive_reason":"abandoned"}',null),
                (1,3,'archive','flow','archive','2026-01-02','{"repo":"o/r","archive_reason":"abandoned","pr_number":17}',null),
                (2,4,'reopen','flow','round_resumed','2026-01-03','{"state":"queued"}',null),
                (null,5,'shared','github','comment','2026-01-03','{}','archive')
              ) t(r,v,k,src,kind,ts,m,parent);
            INSERT INTO jobs(id,flow_id,kind,state,round_no,created_at,updated_at)
              VALUES(10,7,'archive','done',1,'2026-01-02','2026-01-02'),
                    (11,7,'initial','queued',2,'2026-01-03','2026-01-03');
            INSERT INTO jobs(kind,state,inference,created_at,updated_at)
              VALUES ('inference','queued','{}',now(),now());
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
            "UPDATE jobs SET result_payload=%s,result_digest=%s WHERE id=10",
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
                        "state": "done",
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
        original = c.execute(
            "SELECT result_payload::text,result_digest,plan::text FROM jobs"
            " JOIN result_applications ON jobs.id=job_id"
        ).fetchall()
    report = migrate(legacy)
    assert not report["conflicts"]
    assert [r["attempt_ids"] for r in report["rounds"]] == [[42], [20], []]
    assert migrate(legacy, apply=True)["applied"]
    assert migrate(legacy, apply=True)["status"] == "current"
    db = Database(legacy)
    try:
        plan = ResultStore(db).get_plan(10)
        assert plan is not None and plan.delivery is not None
        assert plan.delivery.attempt_id == plan.attempt_id == 42
    finally:
        db.close()
    with psycopg.connect(legacy, row_factory=dict_row) as c:
        attempts = c.execute(
            "SELECT a.*,f.current_attempt_id FROM attempts a JOIN flows f ON f.id=a.flow_id"
            " WHERE a.flow_id=7 ORDER BY a.created_at"
        ).fetchall()
        first, second = attempts
        assert first["id"] == 42 and first["created_at"].day == 1 and not first["delivery_branch"]
        assert second["created_at"].day == 3 and second["id"] == second["current_attempt_id"]
        assert second["delivery_branch"] == "existing-branch"
        assert [
            r["attempt_id"]
            for r in c.execute(
                "SELECT attempt_id FROM flow_attempt_legacy_map WHERE flow_id=7 ORDER BY round_no"
            )
        ] == [42, 20]
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
            "job 10 receipt/plan",
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


def test_first_rescan_recognizes_original_observation_hashes(legacy):
    # Frozen from ActivityStore.append at 007a86ab on PostgreSQL. The timestamp offset
    # and NUL below cannot be reconstructed from timestamptz and escaped display text.
    timestamp = "2026-01-01T18:11:01.123456-07:00"
    common = {"occurred_at": timestamp, "updated_at": timestamp, "author": "user"}
    thread = Observation.model_validate(
        {
            **common,
            "key": "gh:thread",
            "kind": "thread",
            "source": "pr.review_threads",
            "attempt_id": 20,
            "metadata": {"resolved": False},
        }
    )
    comment = Observation.model_validate(
        {
            **common,
            "key": "shared:issue",
            "kind": "comment",
            "source": "issue.comments",
            "body": "x\0y",
        }
    )
    worker = Observation.model_validate(
        {
            **common,
            "key": "worker:20:0:m",
            "kind": "message",
            "source": "worker",
            "attempt_id": 20,
            "metadata": {"attempt_id": 20, "record_id": "m"},
        }
    )
    metadata_only = worker.model_copy(
        update={
            "key": "worker:20:0:meta-only",
            "metadata": {"attempt_id": 20, "record_id": "meta-only"},
        }
    )
    originals = [
        (thread, "1fc9d9895d54e3fa9bf101a61d62cae7cd593562afeac11940a2eb9ed605ec76"),
        (comment, "88b9105a0c7cae5649496f043e2cde6fe74eb6615f596a6d1cd3a599c58f1c2c"),
        (worker, "de33f8b94e9917f8ec7d1c5135a68113a523d6da1e142d939f039e417fe627a7"),
        (metadata_only, "93c50629e7fabcf9f48cdaaa1a7a8cf39d9ca3a6c11f4f9398f5c3c130da5606"),
    ]
    with psycopg.connect(legacy) as c:
        for version, (item, digest) in enumerate(originals, 6):
            c.execute(
                "INSERT INTO console_activity(flow_id,round_no,key,kind,source,version,occurred_at,"
                "updated_at,author,title,body,metadata,digest) VALUES(7,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    2 if item in (thread, worker) else None,
                    item.key,
                    item.kind,
                    item.source,
                    version,
                    timestamp,
                    timestamp,
                    item.author,
                    item.title,
                    item.body.replace("\0", "\\u0000"),
                    Jsonb(
                        {"unsupported_characters_escaped": True}
                        if item is comment
                        else item.metadata
                    ),
                    digest,
                ),
            )
        c.execute("UPDATE console_flows SET activity_version=9 WHERE flow_id=7")
    assert migrate(legacy, apply=True)["applied"]
    db = Database(legacy)
    try:
        activity = ActivityStore(db)
        for item, _ in originals:
            assert not activity.append(7, item)
        # Literal escape is a genuine edit; an older comment version is still a duplicate.
        assert activity.append(7, comment.model_copy(update={"body": r"x\u0000y"}))
        assert not activity.append(7, comment)
        # In contrast, thread/check resolution compares only with the latest version.
        assert activity.append(7, thread.model_copy(update={"metadata": {"resolved": True}}))
        assert activity.append(7, thread)
        assert not activity.append(7, thread)
        with db.connection() as c:
            assert [
                (r["attempt_id"], r["digest"])
                for r in c.execute(
                    "SELECT attempt_id,digest FROM console_activity WHERE flow_id=7 AND version BETWEEN 6 AND 9 ORDER BY version"
                )
            ] == [(item.attempt_id, digest) for item, digest in originals]
    finally:
        db.close()
