"""Transactional console projection, immutable archive and durable source work.

Triggers cover canonical writers (including recovery), rather than relying on every
service remembering an outbox call. Only explicit display facts enter this archive.
"""

CONSOLE_SCHEMA = """
CREATE OR REPLACE VIEW console_facts AS
SELECT f.id, f.repo, f.issue_number, f.state AS lifecycle, f.created_at,
 GREATEST(f.updated_at,COALESCE(d.updated_at,f.updated_at),
          COALESCE(j.updated_at,f.updated_at),COALESCE(a.updated_at,f.updated_at)) AS updated_at,
 CASE
 WHEN f.state='succeeded' THEN 'done'
 WHEN f.state IN ('failed','cancelled') THEN 'blocked'
 WHEN d.state='closed' OR j.state='failed' OR (j.kind='archive'
 AND j.recovery_deadline IS NOT NULL AND j.result_received_at IS NULL) OR EXISTS (
 SELECT 1 FROM result_effects e JOIN jobs ej ON ej.id=e.job_id
 WHERE ej.flow_id=f.id AND ej.result_applied_at IS NULL AND e.state='failed') THEN 'blocked'
 WHEN d.state='merged' THEN 'running'
 WHEN d.pr_number IS NULL AND (x.worker_id IS NULL OR x.released_at IS NOT NULL) THEN 'queued'
 WHEN EXISTS (SELECT 1 FROM jobs pending WHERE pending.flow_id=f.id
 AND (pending.state IN ('queued','running') OR (pending.result_received_at IS NOT NULL
 AND pending.result_applied_at IS NULL))) THEN 'running'
 WHEN d.state='open' THEN 'review' ELSE 'running' END AS phase,
 CASE WHEN f.state='succeeded' THEN 'archived'
 WHEN f.state IN ('failed','cancelled') THEN f.state
 WHEN j.result_received_at IS NOT NULL AND j.result_applied_at IS NULL THEN 'applying_result'
 WHEN j.state='running' AND j.recovery_deadline IS NOT NULL THEN 'recovering'
 WHEN j.state IN ('queued','running') THEN j.kind || ':' || j.state
 WHEN j.state='failed' THEN 'execution_failed'
 WHEN d.state='merged' THEN 'awaiting_archive'
 WHEN d.state='closed' THEN 'delivery_closed'
 WHEN d.state='open' THEN 'awaiting_review'
 ELSE 'unknown' END AS action,
 COALESCE(x.worker_id,j.assigned_worker_id) AS worker_id,
 (f.state IN ('failed','cancelled','succeeded') OR x.released_at IS NOT NULL)
 AS worker_historical,
 COALESCE(a.revision_cycles,0) AS revision_cycles,
 d.pr_number,d.branch,d.state AS delivery_state,d.head_sha,
 d.title AS delivery_title,d.body AS delivery_body,j.id AS job_id,
 j.state AS job_state,j.kind AS job_kind,j.attempt AS job_attempt,
 j.started_at,j.result_applied_at,
 CASE WHEN j.last_error IS NOT NULL OR j.prepare_last_error IS NOT NULL
 OR j.verify_last_error IS NOT NULL THEN 'execution_error' ELSE NULL END AS error_code,
 a.id AS attempt_id,a.codex_session_id,a.session_generation,a.last_revision_summary
FROM flows f LEFT JOIN flow_deliveries d ON d.flow_id=f.id
LEFT JOIN flow_executions x ON x.flow_id=f.id
LEFT JOIN attempts a ON a.id=f.current_attempt_id
LEFT JOIN LATERAL (SELECT * FROM jobs WHERE flow_id=f.id ORDER BY id DESC LIMIT 1) j ON TRUE;

CREATE TABLE IF NOT EXISTS console_flows (
 flow_id BIGINT PRIMARY KEY REFERENCES flows(id),
 facts JSONB NOT NULL, title TEXT, title_updated_at TIMESTAMPTZ,
 state_version BIGINT NOT NULL DEFAULT 0,
 activity_version BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_console_phase ON console_flows ((facts->>'phase'),flow_id DESC);
CREATE INDEX IF NOT EXISTS ix_console_repo ON console_flows ((lower(facts->>'repo')),flow_id DESC);
CREATE INDEX IF NOT EXISTS ix_console_flow_issue ON flows (lower(repo),issue_number);
CREATE INDEX IF NOT EXISTS ix_console_delivery_head ON flow_deliveries(head_sha) WHERE head_sha IS NOT NULL;
CREATE TABLE IF NOT EXISTS console_activity (
 flow_id BIGINT NOT NULL REFERENCES console_flows(flow_id), version BIGINT NOT NULL,
 key TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
 occurred_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 author TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
 url TEXT, parent_key TEXT, deleted BOOLEAN NOT NULL DEFAULT FALSE,
 metadata JSONB NOT NULL DEFAULT '{}', digest TEXT NOT NULL,
 PRIMARY KEY(flow_id,version)
);
CREATE INDEX IF NOT EXISTS ix_console_object ON console_activity
 (flow_id,key,deleted DESC,updated_at DESC,version DESC);
CREATE INDEX IF NOT EXISTS ix_console_digest ON console_activity(flow_id,key,digest);
CREATE INDEX IF NOT EXISTS ix_console_parent ON console_activity
 (flow_id,parent_key,occurred_at,key);
CREATE TABLE IF NOT EXISTS console_sync (
 flow_id BIGINT NOT NULL REFERENCES console_flows(flow_id), source TEXT NOT NULL,
 cursor TEXT, pending BOOLEAN NOT NULL DEFAULT TRUE, dirty BOOLEAN NOT NULL DEFAULT FALSE,
 claim_token TEXT, claim_until TIMESTAMPTZ, next_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 failures INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending', reason TEXT,
 last_success_at TIMESTAMPTZ, limitations JSONB NOT NULL DEFAULT '[]',
 PRIMARY KEY(flow_id,source)
);
ALTER TABLE console_sync ADD COLUMN IF NOT EXISTS worker_scan_clean BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS ix_console_sync_due ON console_sync(next_at,flow_id)
 WHERE pending;
CREATE TABLE IF NOT EXISTS console_installation_backoff (
 installation_id BIGINT PRIMARY KEY, until_at TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE FUNCTION console_enqueue(identity BIGINT) RETURNS VOID AS $$
BEGIN
 -- Reconnects also refresh execution facts: use the same publication -> source order.
 PERFORM flow_id FROM console_flows WHERE flow_id=identity FOR UPDATE;
 INSERT INTO console_sync(flow_id,source) VALUES(identity,'github'),(identity,'worker')
 ON CONFLICT(flow_id,source) DO UPDATE SET
 dirty=console_sync.pending,
 cursor=CASE WHEN console_sync.pending OR console_sync.source='worker'
 THEN console_sync.cursor ELSE NULL END,
 pending=TRUE,
 next_at=CASE WHEN console_sync.pending THEN console_sync.next_at ELSE now() END,
 status=CASE WHEN console_sync.pending THEN console_sync.status ELSE 'pending' END;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION console_refresh(identity BIGINT, baseline BOOLEAN DEFAULT FALSE)
RETURNS VOID AS $$
DECLARE next_facts JSONB; previous_facts JSONB; next_version BIGINT; state_revision BIGINT;
BEGIN
 -- This per-Flow row is also the publication fence: allocation and archive commit together.
 INSERT INTO console_flows(flow_id,facts) VALUES(identity,'{}') ON CONFLICT DO NOTHING;
 SELECT facts INTO previous_facts FROM console_flows WHERE flow_id=identity FOR UPDATE;
 SELECT to_jsonb(f) INTO next_facts FROM console_facts f WHERE id=identity;
 IF next_facts IS NULL OR previous_facts-'updated_at'=next_facts-'updated_at' THEN RETURN; END IF;
 UPDATE console_flows SET facts=next_facts,state_version=state_version+1,
 activity_version=activity_version+1 WHERE flow_id=identity
 RETURNING activity_version,state_version INTO next_version,state_revision;
 INSERT INTO console_activity(flow_id,version,key,source,kind,occurred_at,updated_at,
 author,title,body,metadata,digest) VALUES(identity,next_version,
 'flow:'||identity||':'||state_revision,'flow',CASE WHEN baseline THEN 'baseline' ELSE 'state' END,
 clock_timestamp(),clock_timestamp(),'FlowTool',
 CASE WHEN baseline THEN '迁移基线：此前业务历史不可还原' ELSE 'Flow 状态变更' END,
 jsonb_build_object('before',previous_facts,'after',next_facts)::text,
 jsonb_build_object('baseline',baseline),
 encode(sha256(convert_to(next_facts::text,'UTF8')),'hex'));
 PERFORM console_enqueue(identity);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION console_fact_changed() RETURNS TRIGGER AS $$
DECLARE identity BIGINT;
BEGIN
 IF TG_TABLE_NAME='flows' THEN identity=NEW.id;
 ELSIF TG_TABLE_NAME='result_effects' THEN
 SELECT flow_id INTO identity FROM jobs WHERE id=NEW.job_id;
 ELSE identity=NEW.flow_id;
 END IF;
 PERFORM console_refresh(identity);
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DO $$ DECLARE target TEXT; BEGIN
 FOREACH target IN ARRAY ARRAY['flows','jobs','attempts','flow_deliveries',
                               'flow_executions','result_effects'] LOOP
 EXECUTE format('CREATE OR REPLACE TRIGGER console_fact_changed AFTER INSERT OR UPDATE ON %I'
                ' FOR EACH ROW EXECUTE FUNCTION console_fact_changed()',target);
 END LOOP;
END $$;
CREATE OR REPLACE FUNCTION console_worker_changed() RETURNS TRIGGER AS $$
BEGIN
 IF TG_OP='INSERT' OR NEW.connection_epoch IS DISTINCT FROM OLD.connection_epoch THEN
 PERFORM console_enqueue(flow_id) FROM flow_executions WHERE worker_id=NEW.worker_id ORDER BY flow_id;
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER console_worker_changed AFTER INSERT OR UPDATE ON workers
 FOR EACH ROW EXECUTE FUNCTION console_worker_changed();
-- Only missing projections are seeded; ordinary restarts never rescan archived source bodies.
SELECT console_refresh(id,TRUE) FROM flows
 WHERE NOT EXISTS (SELECT 1 FROM console_flows c WHERE c.flow_id=flows.id);
"""
