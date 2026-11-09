"""Offline round-history conversion on disposable PostgreSQL schemas only.

The removed tables, views and trigger below are frozen from the six-state deployment,
not reconstructed from the new schema or from a runtime fallback.
"""

import json
from datetime import timedelta

import psycopg
import pytest
from psycopg.errors import CheckViolation, LockNotAvailable
from psycopg.types.json import Jsonb

from app.persistence.activity import ActivityStore
from app.persistence.database import Database
from scripts.migrate_flow_states import main, migrate
from tests import test_flow_state_migration
from tests.test_flow_state_migration import NOW, binding, delivery, flow, job, snapshot

legacy = test_flow_state_migration.legacy

SIX_STATE_DDL = """
DROP INDEX ux_flows_active;
ALTER TABLE flows
 ADD COLUMN current_round_no INTEGER NOT NULL DEFAULT 1 CHECK(current_round_no>0),
 ADD COLUMN archive_reason TEXT CHECK(archive_reason IN ('succeeded','abandoned')),
 ADD COLUMN archive_summary TEXT, ADD COLUMN archived_at TIMESTAMPTZ,
 ADD CHECK(state IN ('queued','running','review','blocked','finalizing','archived')),
 ADD CHECK((state='archived')=(archived_at IS NOT NULL)),
 ADD CHECK(state NOT IN ('finalizing','archived') OR archive_reason IS NOT NULL),
 ADD CHECK(archive_reason IS NULL OR state IN ('blocked','finalizing','archived'));
CREATE UNIQUE INDEX ux_flows_active ON flows(repo,issue_number) WHERE state<>'archived';
ALTER TABLE attempts ADD COLUMN round_no INTEGER NOT NULL DEFAULT 1;
ALTER TABLE jobs ADD COLUMN round_no INTEGER NOT NULL DEFAULT 1;
CREATE TABLE issue_flow_owners (
 repo TEXT NOT NULL, issue_number INTEGER NOT NULL,
 flow_id BIGINT NOT NULL REFERENCES flows(id), version BIGINT NOT NULL DEFAULT 1,
 PRIMARY KEY(repo,issue_number)
);
CREATE TABLE flow_rounds (
 flow_id BIGINT NOT NULL REFERENCES flows(id), round_no INTEGER NOT NULL,
 repo TEXT NOT NULL, pr_number INTEGER,
 original_reason TEXT NOT NULL CHECK(original_reason IN ('succeeded','abandoned')),
 archive_reason TEXT NOT NULL CHECK(archive_reason IN ('succeeded','abandoned')),
 archive_summary TEXT, archived_at TIMESTAMPTZ NOT NULL,
 delivery JSONB, execution JSONB, merged_at TIMESTAMPTZ, merge_commit_sha TEXT,
 version BIGINT NOT NULL DEFAULT 0, PRIMARY KEY(flow_id,round_no)
);
CREATE UNIQUE INDEX ux_flow_round_pr ON flow_rounds(repo,pr_number) WHERE pr_number IS NOT NULL;
CREATE TABLE flow_round_corrections (
 flow_id BIGINT NOT NULL, round_no INTEGER NOT NULL, version BIGINT NOT NULL,
 actor TEXT NOT NULL, reason TEXT NOT NULL, evidence JSONB NOT NULL,
 corrected_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(flow_id,round_no,version),
 FOREIGN KEY(flow_id,round_no) REFERENCES flow_rounds(flow_id,round_no)
);
CREATE OR REPLACE VIEW console_facts AS
SELECT f.id,f.repo,f.issue_number,f.state AS lifecycle,f.created_at,
 GREATEST(f.updated_at,COALESCE(d.updated_at,f.updated_at),
          COALESCE(j.updated_at,f.updated_at),COALESCE(a.updated_at,f.updated_at)) AS updated_at,
 CASE WHEN f.state='finalizing' THEN 'running' ELSE f.state END AS phase,
 CASE WHEN f.state='archived' THEN 'archived'
 WHEN f.state='blocked' AND f.archive_reason IS NOT NULL THEN 'archive_blocked'
 WHEN f.state='blocked' THEN 'execution_failed' WHEN f.state='finalizing' THEN 'finalizing'
 WHEN j.result_received_at IS NOT NULL AND j.result_applied_at IS NULL THEN 'applying_result'
 WHEN j.state='running' AND j.recovery_deadline IS NOT NULL THEN 'recovering'
 WHEN j.state IN ('queued','running') THEN j.kind || ':' || j.state
 WHEN j.state='failed' THEN 'execution_failed' WHEN d.state='merged' THEN 'awaiting_archive'
 WHEN d.state='open' THEN 'awaiting_review' ELSE 'unknown' END AS action,
 COALESCE(x.worker_id,j.assigned_worker_id) AS worker_id,
 (f.state='archived' OR x.released_at IS NOT NULL) AS worker_historical,
 COALESCE(a.revision_cycles,0) AS revision_cycles,
 d.pr_number,d.branch,d.state AS delivery_state,d.head_sha,d.title AS delivery_title,d.body AS delivery_body,
 j.id AS job_id,j.state AS job_state,j.kind AS job_kind,j.attempt AS job_attempt,j.started_at,j.result_applied_at,
 CASE WHEN j.last_error IS NOT NULL OR j.prepare_last_error IS NOT NULL
 OR j.verify_last_error IS NOT NULL THEN 'execution_error' ELSE NULL END AS error_code,
 a.id AS attempt_id,a.codex_session_id,a.session_generation,a.last_revision_summary,
 f.current_round_no,COALESCE(r.archive_reason,f.archive_reason) AS archive_reason,
 f.archive_summary,f.archived_at
FROM flows f LEFT JOIN flow_deliveries d ON d.flow_id=f.id
LEFT JOIN flow_executions x ON x.flow_id=f.id LEFT JOIN attempts a ON a.id=f.current_attempt_id
LEFT JOIN flow_rounds r ON r.flow_id=f.id AND r.round_no=f.current_round_no
LEFT JOIN LATERAL (SELECT candidate.* FROM jobs candidate
 WHERE flow_id=f.id AND round_no=f.current_round_no AND kind<>'inference'
 ORDER BY id DESC LIMIT 1) j ON TRUE;
CREATE VIEW flow_label_desired AS
 SELECT lower(o.repo) AS repo,o.issue_number AS number,'issue'::text AS kind,
 f.id AS flow_id,f.current_round_no AS round_no,o.version AS owner_version,f.installation_id,f.state
 FROM issue_flow_owners o JOIN flows f ON f.id=o.flow_id
 AND lower(f.repo)=lower(o.repo) AND f.issue_number=o.issue_number
 UNION ALL
 SELECT lower(f.repo),d.pr_number,'pr',f.id,f.current_round_no,0,f.installation_id,f.state
 FROM flows f JOIN flow_deliveries d ON d.flow_id=f.id
 WHERE d.pr_number IS NOT NULL AND NOT EXISTS (SELECT 1 FROM flow_rounds r
 WHERE lower(r.repo)=lower(f.repo) AND r.pr_number=d.pr_number)
 UNION ALL
 SELECT lower(r.repo),r.pr_number,'pr',f.id,r.round_no,0,f.installation_id,'archived'
 FROM flow_rounds r JOIN flows f ON f.id=r.flow_id WHERE r.pr_number IS NOT NULL;
CREATE FUNCTION console_round_changed() RETURNS TRIGGER AS $$
DECLARE previous_round JSONB; correction JSONB; event JSONB;
 next_version BIGINT; event_time TIMESTAMPTZ;
BEGIN
 IF TG_OP='UPDATE' THEN
   IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
   previous_round=to_jsonb(OLD);
 END IF;
 SELECT to_jsonb(c) INTO correction FROM flow_round_corrections c
 WHERE c.flow_id=NEW.flow_id AND c.round_no=NEW.round_no AND c.version=NEW.version;
 event=jsonb_build_object('before',previous_round,'after',to_jsonb(NEW),'correction',correction);
 event_time=CASE WHEN TG_OP='INSERT' THEN NEW.archived_at
   ELSE COALESCE((correction->>'corrected_at')::timestamptz,clock_timestamp()) END;
 INSERT INTO console_flows(flow_id,facts) VALUES(NEW.flow_id,'{}') ON CONFLICT DO NOTHING;
 PERFORM flow_id FROM console_flows WHERE flow_id=NEW.flow_id FOR UPDATE;
 UPDATE console_flows SET activity_version=activity_version+1 WHERE flow_id=NEW.flow_id
 RETURNING activity_version INTO next_version;
 INSERT INTO console_activity(flow_id,version,key,source,kind,occurred_at,updated_at,
 author,title,body,metadata,digest) VALUES(NEW.flow_id,next_version,
 'flow:'||NEW.flow_id||':round:'||NEW.round_no||':'||next_version,'flow','round',
 event_time,event_time,COALESCE(correction->>'actor','FlowTool'),'轮次归档记录变更',event::text,
 jsonb_build_object('round_no',NEW.round_no,'round_version',NEW.version),
 encode(sha256(convert_to(event::text,'UTF8')),'hex'));
 PERFORM console_refresh(NEW.flow_id);
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER console_round_changed AFTER INSERT OR UPDATE ON flow_rounds
 FOR EACH ROW EXECUTE FUNCTION console_round_changed();
"""


@pytest.fixture
def six_state(legacy):
    legacy.execute(SIX_STATE_DDL)
    return legacy


def archived(connection, *, issue=1, reason="abandoned", number=17, repo="owner/repo"):
    """Create the old immutable snapshot through its frozen SQL publication trigger."""
    identity, _ = flow(connection, "blocked", issue=issue)
    connection.execute("UPDATE flows SET repo=%s WHERE id=%s", (repo, identity))
    binding(connection, identity, released=True)
    connection.execute(
        "UPDATE flow_executions SET repo_identity=worker_id||':'||%s WHERE flow_id=%s",
        (repo, identity),
    )
    if number is not None:
        delivery(
            connection,
            identity,
            state="merged" if reason == "succeeded" else "closed",
            number=number,
        )
    connection.execute(
        "UPDATE flows SET state='archived',archive_reason=%s,archive_summary='original summary',"
        "archived_at=%s WHERE id=%s",
        (reason, NOW + timedelta(minutes=2), identity),
    )
    connection.execute(
        "INSERT INTO issue_flow_owners(repo,issue_number,flow_id) VALUES (%s,%s,%s)",
        (repo, issue, identity),
    )
    connection.execute(
        "INSERT INTO flow_rounds(flow_id,round_no,repo,pr_number,original_reason,archive_reason,"
        "archive_summary,archived_at,delivery,execution,merged_at,merge_commit_sha) "
        "SELECT f.id,1,f.repo,d.pr_number,f.archive_reason,f.archive_reason,f.archive_summary,"
        "f.archived_at,to_jsonb(d),to_jsonb(x),d.merged_at,d.merge_commit_sha FROM flows f "
        "LEFT JOIN flow_deliveries d ON d.flow_id=f.id "
        "LEFT JOIN flow_executions x ON x.flow_id=f.id WHERE f.id=%s",
        (identity,),
    )
    return identity


def observe(connection, identity, *, correct=False):
    connection.execute(
        "UPDATE flow_rounds SET merged_at=%s,merge_commit_sha=%s WHERE flow_id=%s",
        (NOW, "b" * 40, identity),
    )
    if not correct:
        return
    row = connection.execute("SELECT * FROM flow_rounds WHERE flow_id=%s", (identity,)).fetchone()
    evidence = {
        "repo": row["repo"],
        "pr_number": row["pr_number"],
        "merged_at": NOW.isoformat(),
        "merge_commit_sha": "b" * 40,
        "merge_time_precision_seconds": 1,
        "archived_at": row["archived_at"].isoformat(),
        "original_reason": "abandoned",
        "before": {"archive_reason": "abandoned", "version": 0},
        "after": {"archive_reason": "succeeded", "version": 1},
    }
    connection.execute(
        "INSERT INTO flow_round_corrections VALUES (%s,1,1,'operator','verified late webhook',%s,%s)",
        (identity, Jsonb(evidence), NOW + timedelta(minutes=3)),
    )
    connection.execute(
        "UPDATE flow_rounds SET archive_reason='succeeded',version=1 WHERE flow_id=%s",
        (identity,),
    )
    connection.execute("UPDATE flows SET archive_reason='succeeded' WHERE id=%s", (identity,))


def convert(url):
    report = migrate(url, convert_round_history=True)
    assert report.status == "ready", report.as_dict()
    result = migrate(
        url, convert_round_history=True, apply=True, expected_fingerprint=report.fingerprint
    )
    assert result.status == "applied", result.as_dict()
    return result


def test_converts_original_archives_evidence_corrections_and_all_saved_fields(
    six_state, postgresql_url
):
    six_state.execute(
        "ALTER TABLE flow_rounds ADD COLUMN operator_snapshot JSONB "
        'DEFAULT \'{"opaque":{"original":[1,2,3]}}\''
    )
    corrected = archived(six_state)
    observe(six_state, corrected, correct=True)
    unchanged = archived(six_state, issue=2, number=18)
    observe(
        six_state, unchanged
    )  # A pre-archive merge is evidence, NOT migration correction consent.
    investigation = archived(six_state, issue=3, number=None, reason="succeeded")
    merged = archived(six_state, issue=4, number=19, reason="succeeded")
    # Include a previous migration's backup; conversion must append, never replace it.
    six_state.execute("""
        CREATE TABLE flow_state_migration_backup (
          table_name TEXT NOT NULL,row_data JSONB NOT NULL,report_fingerprint TEXT NOT NULL,
          captured_at TIMESTAMPTZ NOT NULL DEFAULT now());
        INSERT INTO flow_state_migration_backup VALUES ('older','{"opaque":"original"}','prior',now());
    """)
    before = snapshot(six_state)
    dry = migrate(postgresql_url, convert_round_history=True)
    assert dry.status == "ready", dry.as_dict()
    assert snapshot(six_state) == before
    convert(postgresql_url)
    after = snapshot(six_state)
    assert "flow_rounds" not in after["rows"] and "flow_round_corrections" not in after["rows"]
    assert after["rows"]["flows"] == before["rows"]["flows"]
    for table, rows in before["rows"].items():
        if table == "flow_state_migration_backup":
            assert all(r in after["rows"][table] for r in rows)
            continue
        saved = six_state.execute(
            "SELECT row_data AS data FROM flow_state_migration_backup WHERE table_name=%s "
            "ORDER BY row_data::text",
            (table,),
        ).fetchall()
        assert saved == rows, table
        if table == "console_activity":
            assert all(
                {"data": {**r["data"], "round_no": None}} in after["rows"][table] for r in rows
            )
        elif table not in {
            "flow_rounds",
            "flow_round_corrections",
            "console_flows",
            "console_sync",
        }:
            assert after["rows"][table] == rows, table
    database = Database(postgresql_url)
    try:
        activities = ActivityStore(database)
        assert activities.archived_pr("OWNER/Repo", 17) == (corrected, 1)
        assert activities.archived_pr("OWNER/Repo", 18) == (unchanged, 1)
        original = activities.archived(corrected, 1)
        assert original.metadata["archive_reason"] == "abandoned"
        assert original.metadata["merged_at"] is None
        assert original.occurred_at == original.updated_at == NOW + timedelta(minutes=2)
        assert original.author == "FlowTool"
        correction = activities.flow_event(corrected, 1, "archive_correction")
        assert correction.metadata["before"] == "abandoned"
        assert correction.metadata["after"] == "succeeded"
        assert correction.metadata["actor"] == "operator"
        assert correction.metadata["archived_at"] == original.occurred_at.isoformat()
        assert correction.occurred_at == NOW + timedelta(minutes=3)
        assert activities.flow_event(unchanged, 1, "merge").metadata["merged_at"] == NOW.isoformat()
        assert activities.flow_event(unchanged, 1, "archive_correction") is None
        assert activities.archived(investigation, 1).metadata["pr_number"] is None
        assert activities.archived(merged, 1).metadata["merged_at"] == NOW.isoformat()
        database.init_db()
        database.init_db()
    finally:
        database.close()
    assert snapshot(six_state) == after
    assert migrate(postgresql_url, convert_round_history=True).status == "refused"
    assert snapshot(six_state) == after


def test_old_pr_attribution_survives_new_round(six_state, postgresql_url):
    identity = archived(six_state)
    six_state.execute("DELETE FROM flow_deliveries WHERE flow_id=%s", (identity,))
    six_state.execute("DELETE FROM flow_executions WHERE flow_id=%s", (identity,))
    six_state.execute(
        "UPDATE flows SET state='queued',current_round_no=2,current_attempt_id=NULL,"
        "archived_at=NULL,archive_reason=NULL,archive_summary=NULL WHERE id=%s",
        (identity,),
    )
    job_id = job(six_state, identity)
    six_state.execute("UPDATE jobs SET round_no=2 WHERE id=%s", (job_id,))
    convert(postgresql_url)
    assert six_state.execute(
        "SELECT flow_id,round_no,state FROM flow_label_desired WHERE kind='pr' AND number=17"
    ).fetchall() == [{"flow_id": identity, "round_no": 1, "state": "archived"}]
    assert six_state.execute(
        "SELECT state,current_round_no FROM flows WHERE id=%s", (identity,)
    ).fetchone() == {
        "state": "queued",
        "current_round_no": 2,
    }


@pytest.mark.parametrize(
    "hazard",
    [
        "missing-correction",
        "ambiguous-corrections",
        "correction-evidence",
        "original-snapshot",
        "missing-snapshot",
        "partial-merge",
        "missing-round",
        "wrong-repo",
        "unapplied-plan",
        "missing-owner",
        "audit-digest",
        "audit-chain",
        "pending-result",
    ],
)
def test_unprovable_conversion_refuses_with_identity_and_no_writes(
    six_state, postgresql_url, hazard
):
    identity = archived(six_state)
    observe(six_state, identity, correct=True)
    if hazard == "missing-correction":
        six_state.execute("DELETE FROM flow_round_corrections")
    elif hazard == "ambiguous-corrections":
        six_state.execute(
            "INSERT INTO flow_round_corrections SELECT flow_id,round_no,2,actor,reason,evidence,corrected_at "
            "FROM flow_round_corrections"
        )
    elif hazard == "correction-evidence":
        six_state.execute(
            "UPDATE flow_round_corrections SET evidence=jsonb_set(evidence,'{pr_number}','999')"
        )
    elif hazard == "original-snapshot":
        six_state.execute("UPDATE flow_rounds SET archive_summary='rewritten history'")
        six_state.execute("UPDATE flows SET archive_summary='rewritten history'")
    elif hazard == "missing-snapshot":
        six_state.execute("UPDATE flow_rounds SET delivery=NULL")
    elif hazard == "partial-merge":
        six_state.execute("UPDATE flow_rounds SET merge_commit_sha=NULL")
    elif hazard == "missing-round":
        six_state.execute("DELETE FROM flow_round_corrections; DELETE FROM flow_rounds")
    elif hazard == "wrong-repo":
        six_state.execute("UPDATE flow_rounds SET repo='foreign/repo'")
    elif hazard == "missing-owner":
        six_state.execute("DELETE FROM issue_flow_owners")
    elif hazard == "audit-digest":
        six_state.execute("UPDATE console_activity SET digest='corrupted' WHERE kind='round'")
    elif hazard == "audit-chain":
        six_state.execute(
            "DELETE FROM console_activity WHERE kind='round' AND body::jsonb->'before' IS NOT NULL "
            "AND body::jsonb->'correction'='null'::jsonb"
        )
    elif hazard == "pending-result":
        job_id = job(six_state, identity, state="failed")
        six_state.execute("UPDATE jobs SET result_received_at=%s WHERE id=%s", (NOW, job_id))
    elif hazard == "unapplied-plan":
        job_id = job(six_state, identity, state="failed")
        six_state.execute(
            "INSERT INTO result_applications(job_id,digest,plan,created_at) VALUES (%s,'digest','{}',%s)",
            (job_id, NOW),
        )
    before = snapshot(six_state)
    report = migrate(postgresql_url, convert_round_history=True)
    assert report.status == "refused", report.as_dict()
    assert any(identity in p["flow_ids"] for p in report.problems)
    assert (
        migrate(
            postgresql_url,
            convert_round_history=True,
            apply=True,
            expected_fingerprint=report.fingerprint,
        ).status
        == "refused"
    )
    assert snapshot(six_state) == before


def test_case_insensitive_pr_collision_refuses_all_owners(six_state, postgresql_url):
    first = archived(six_state)
    second = archived(six_state, issue=2, repo="OWNER/Repo")
    before = snapshot(six_state)
    report = migrate(postgresql_url, convert_round_history=True)
    assert report.status == "refused", report.as_dict()
    assert any(
        p["flow_ids"] == [first, second] and "Duplicate archived PR" in p["message"]
        for p in report.problems
    )
    assert snapshot(six_state) == before


def test_explicit_mode_fingerprint_and_cli(six_state, postgresql_url, capsys):
    identity = archived(six_state)
    before = snapshot(six_state)
    assert migrate(postgresql_url).status == "refused"
    assert migrate(postgresql_url, convert_round_history=True, apply=True).status == "refused"
    assert snapshot(six_state) == before
    report = migrate(postgresql_url, convert_round_history=True)
    six_state.execute("UPDATE flows SET installation_id=43 WHERE id=%s", (identity,))
    before = snapshot(six_state)
    assert (
        migrate(
            postgresql_url,
            convert_round_history=True,
            apply=True,
            expected_fingerprint=report.fingerprint,
        ).status
        == "refused"
    )
    assert snapshot(six_state) == before
    schema = six_state.execute("SELECT current_schema() AS name").fetchone()["name"]
    args = ["--database-url", postgresql_url, "--schema", schema, "--convert-round-history"]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert main([*args, "--apply", "--expect-report", report["fingerprint"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"


@pytest.mark.parametrize("failure", ["publication", "late-drop"])
def test_conversion_failure_restores_every_row_and_old_authority(
    six_state, postgresql_url, failure
):
    archived(six_state)
    if failure == "publication":
        six_state.execute(
            "ALTER TABLE console_activity ADD CONSTRAINT reject_archive CHECK(kind<>'archive') NOT VALID"
        )
        error = CheckViolation
    else:
        # No CASCADE: an unknown consumer blocks deletion after successful publication.
        six_state.execute("CREATE VIEW unknown_round_consumer AS SELECT * FROM flow_rounds")
        error = psycopg.errors.DependentObjectsStillExist
    before = snapshot(six_state)
    report = migrate(postgresql_url, convert_round_history=True)
    assert report.status == "ready", report.as_dict()
    with pytest.raises(error):
        migrate(
            postgresql_url,
            convert_round_history=True,
            apply=True,
            expected_fingerprint=report.fingerprint,
        )
    assert snapshot(six_state) == before
    # A rolled-back conversion leaves the original trigger usable too.
    observe(six_state, 1)
    assert (
        six_state.execute(
            "SELECT count(*) AS n FROM console_activity WHERE kind='round'"
        ).fetchone()["n"]
        == 2
    )


def test_live_writers_are_not_waited_out(six_state, postgresql_url):
    identity = archived(six_state)
    before = snapshot(six_state)
    with psycopg.connect(postgresql_url) as other:
        other.execute("UPDATE flows SET installation_id=43 WHERE id=%s", (identity,))
        with pytest.raises(LockNotAvailable):
            migrate(postgresql_url, convert_round_history=True)
        other.rollback()
    assert snapshot(six_state) == before


@pytest.mark.parametrize("partial", [False, True])
def test_startup_refuses_round_tables_before_any_ddl(six_state, postgresql_url, partial):
    if partial:
        six_state.execute("DROP TABLE flow_round_corrections")
    before = snapshot(six_state)
    database = Database(postgresql_url)
    try:
        with pytest.raises(ValueError, match="legacy round history"):
            database.init_db()
    finally:
        database.close()
    assert snapshot(six_state) == before


def test_old_migration_snapshot_without_round_trigger_publication(six_state, postgresql_url):
    # The previous four-state migration seeded snapshots before installing round
    # triggers. Their immutable delivery snapshot still proves the original archive.
    identity = archived(six_state, reason="succeeded")
    six_state.execute("DELETE FROM console_activity WHERE kind='round'")
    convert(postgresql_url)
    database = Database(postgresql_url)
    try:
        assert (
            ActivityStore(database).archived(identity, 1).metadata["archive_reason"] == "succeeded"
        )
    finally:
        database.close()


def test_missing_publication_verification_rolls_back(six_state, postgresql_url, monkeypatch):
    identity = archived(six_state)
    before = snapshot(six_state)
    report = migrate(postgresql_url, convert_round_history=True)
    monkeypatch.setattr(ActivityStore, "append_flow", lambda *args, **kwargs: False)
    with pytest.raises(ValueError, match=f"Flow {identity} round 1: archive publication"):
        migrate(
            postgresql_url,
            convert_round_history=True,
            apply=True,
            expected_fingerprint=report.fingerprint,
        )
    assert snapshot(six_state) == before
