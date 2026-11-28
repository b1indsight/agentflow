"""Transactional console projection, immutable archive and durable source work.

Triggers cover canonical writers (including recovery), rather than relying on every
service remembering an outbox call. Only explicit display facts enter this archive.
"""

CONSOLE_FACTS_VIEW = """
CREATE OR REPLACE VIEW console_facts AS
SELECT f.id, f.repo, f.issue_number, f.state AS lifecycle, f.created_at,
 GREATEST(f.updated_at,COALESCE(a.delivery_updated_at,f.updated_at),
          COALESCE(j.updated_at,f.updated_at),COALESCE(a.updated_at,f.updated_at)) AS updated_at,
 CASE WHEN f.state='finalizing' THEN 'running' ELSE f.state END AS phase,
 CASE WHEN f.state='archived' THEN 'archived'
 WHEN f.state='blocked' AND f.archive_reason IS NOT NULL THEN 'archive_blocked'
 WHEN f.state='blocked' THEN 'execution_failed'
 WHEN f.state='finalizing' THEN 'finalizing'
 WHEN j.result_received_at IS NOT NULL AND j.result_applied_at IS NULL THEN 'applying_result'
 WHEN j.state='running' AND j.recovery_deadline IS NOT NULL THEN 'recovering'
 WHEN j.state IN ('queued','running') THEN j.kind || ':' || j.state
 WHEN j.state='failed' THEN 'execution_failed'
 WHEN a.delivery_state='merged' THEN 'awaiting_archive'
 WHEN a.delivery_state='waiting' THEN 'awaiting_input'
 WHEN a.delivery_state='open' THEN 'awaiting_review'
 ELSE 'unknown' END AS action,
 COALESCE(x.worker_id,j.assigned_worker_id) AS worker_id,
 (f.state='archived' OR x.released_at IS NOT NULL)
 AS worker_historical,
 COALESCE(a.revision_cycles,0) AS revision_cycles,
 a.delivery_pr_number AS pr_number,a.delivery_branch AS branch,a.delivery_state,a.delivery_head_sha AS head_sha,
 a.delivery_title,a.delivery_body,j.id AS job_id,
 j.state AS job_state,j.kind AS job_kind,j.attempt AS job_attempt,
 j.started_at,j.result_applied_at,
 CASE WHEN j.last_error IS NOT NULL OR j.prepare_last_error IS NOT NULL
 OR j.verify_last_error IS NOT NULL THEN 'execution_error' ELSE NULL END AS error_code,
 a.id AS attempt_id,a.codex_session_id,a.session_generation,a.last_revision_summary,
 f.current_attempt_id,a.created_at AS attempt_started_at,f.archive_reason,
 f.archive_summary,f.archived_at,f.title AS task_title,f.history_only
FROM flows f
LEFT JOIN flow_executions x ON x.flow_id=f.id
LEFT JOIN attempts a ON a.id=f.current_attempt_id
LEFT JOIN LATERAL (SELECT candidate.*
 FROM jobs candidate WHERE flow_id=f.id AND attempt_id=f.current_attempt_id
 AND kind<>'inference' ORDER BY id DESC LIMIT 1) j ON TRUE;
"""

CONSOLE_SCHEMA = CONSOLE_FACTS_VIEW + """
CREATE TABLE IF NOT EXISTS console_flows (
 flow_id BIGINT PRIMARY KEY REFERENCES flows(id),
 facts JSONB NOT NULL, title TEXT, title_updated_at TIMESTAMPTZ,
 state_version BIGINT NOT NULL DEFAULT 0,
 activity_version BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_console_phase ON console_flows ((facts->>'phase'),flow_id DESC);
CREATE INDEX IF NOT EXISTS ix_console_repo ON console_flows ((lower(facts->>'repo')),flow_id DESC);
CREATE INDEX IF NOT EXISTS ix_console_flow_issue ON flows (lower(repo),issue_number);
CREATE TABLE IF NOT EXISTS console_activity (
 flow_id BIGINT NOT NULL REFERENCES console_flows(flow_id), version BIGINT NOT NULL,
 key TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
 occurred_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 author TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
 url TEXT, parent_key TEXT, deleted BOOLEAN NOT NULL DEFAULT FALSE,
 metadata JSONB NOT NULL DEFAULT '{}', digest TEXT NOT NULL,
 attempt_id BIGINT,
 awaiting_agent BOOLEAN NOT NULL DEFAULT FALSE,
 CONSTRAINT console_activity_owner FOREIGN KEY(flow_id,attempt_id) REFERENCES attempts(flow_id,id),
 PRIMARY KEY(flow_id,version)
);
CREATE INDEX IF NOT EXISTS ix_console_pending_agent ON console_activity(flow_id,attempt_id,version)
 WHERE awaiting_agent;
CREATE INDEX IF NOT EXISTS ix_console_activity_attempt ON console_activity(flow_id,attempt_id,version);
CREATE UNIQUE INDEX IF NOT EXISTS ux_console_flow_evidence ON console_activity(flow_id,attempt_id,kind)
 WHERE source='flow' AND kind IN ('archive','merge','archive_correction');
CREATE UNIQUE INDEX IF NOT EXISTS ux_console_archived_pr ON console_activity
 (lower(metadata->>'repo'),((metadata->>'pr_number')::integer))
 WHERE source='flow' AND kind='archive' AND metadata->>'pr_number' IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_console_flow_origin ON console_activity(flow_id)
 WHERE source='flow' AND kind IN ('created','snapshot');
CREATE UNIQUE INDEX IF NOT EXISTS ux_console_flow_attempt_start ON console_activity(flow_id,attempt_id)
 WHERE source='flow' AND kind='attempt_started';
CREATE OR REPLACE FUNCTION console_preserve_evidence() RETURNS TRIGGER AS $$
BEGIN
 -- Every Flow row except display snapshots and notices is ledger evidence (activity.py).
 IF OLD.source='flow' AND OLD.kind NOT IN ('state','baseline','inference_exhausted') THEN
   RAISE EXCEPTION 'Flow activity evidence is immutable' USING ERRCODE='23514';
 END IF;
 IF TG_OP='UPDATE' THEN RETURN NEW; END IF;
 RETURN OLD;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER console_preserve_evidence BEFORE UPDATE OR DELETE ON console_activity
 FOR EACH ROW EXECUTE FUNCTION console_preserve_evidence();
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
 worker_scan_clean BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(flow_id,source)
);
CREATE INDEX IF NOT EXISTS ix_console_sync_due ON console_sync(next_at,flow_id)
 WHERE pending;
CREATE TABLE IF NOT EXISTS console_installation_backoff (
 installation_id BIGINT PRIMARY KEY, until_at TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE FUNCTION console_enqueue(identity BIGINT) RETURNS VOID AS $$
BEGIN
 -- Reconnects also refresh execution facts: use the same publication -> source order.
 PERFORM flow_id FROM console_flows WHERE flow_id=identity FOR UPDATE;
 IF EXISTS (SELECT 1 FROM flows WHERE id=identity AND history_only) THEN RETURN; END IF;
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
 SELECT facts INTO previous_facts
 FROM console_flows WHERE flow_id=identity FOR UPDATE;
 SELECT to_jsonb(f) INTO next_facts FROM console_facts f WHERE id=identity;
 IF (next_facts->>'history_only')::boolean THEN
   -- Keep the last published card and its activity history. Classification is not a new event.
   IF previous_facts->>'history_only'='true' THEN RETURN; END IF;
   -- Older cards may lack an Attempt or its start time, filled by the offline migration.
   next_facts := next_facts || previous_facts || jsonb_build_object('history_only',TRUE,
     'current_attempt_id',next_facts->'current_attempt_id','attempt_started_at',next_facts->'attempt_started_at');
   UPDATE console_flows SET facts=next_facts,state_version=state_version+1
    WHERE flow_id=identity AND facts IS DISTINCT FROM next_facts;
   RETURN;
 END IF;
 IF next_facts->>'current_attempt_id' IS NULL THEN RETURN; END IF;
 IF previous_facts-'updated_at'=next_facts-'updated_at' THEN RETURN; END IF;
 UPDATE console_flows SET facts=next_facts,state_version=state_version+1,
 activity_version=activity_version+1 WHERE flow_id=identity
 RETURNING activity_version,state_version INTO next_version,state_revision;
 INSERT INTO console_activity(flow_id,version,attempt_id,key,source,kind,occurred_at,updated_at,
 author,title,body,metadata,digest) VALUES(identity,next_version,
 (next_facts->>'current_attempt_id')::bigint,
 'flow:'||identity||':'||state_revision,'flow',CASE WHEN baseline THEN 'baseline' ELSE 'state' END,
 clock_timestamp(),clock_timestamp(),'AgentFlow',
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
 IF identity IS NOT NULL THEN PERFORM console_refresh(identity); END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DO $$ DECLARE target TEXT; BEGIN
 FOREACH target IN ARRAY ARRAY['flows','jobs','attempts','flow_executions','result_effects'] LOOP
 EXECUTE format('CREATE OR REPLACE TRIGGER console_fact_changed AFTER INSERT OR UPDATE ON %I'
                ' FOR EACH ROW EXECUTE FUNCTION console_fact_changed()',target);
 END LOOP;
END $$;
CREATE OR REPLACE FUNCTION console_worker_changed() RETURNS TRIGGER AS $$
BEGIN
 IF TG_OP='INSERT' OR NEW.connection_epoch IS DISTINCT FROM OLD.connection_epoch THEN
 PERFORM console_enqueue(flow_id) FROM (
   SELECT flow_id FROM flow_executions WHERE worker_id=NEW.worker_id
   UNION
   -- Include outstanding historical gaps and in-flight scans racing this reconnect.
   SELECT j.flow_id FROM jobs j JOIN console_sync s ON s.flow_id=j.flow_id
   WHERE j.assigned_worker_id=NEW.worker_id AND j.kind IN ('initial','revision')
   AND ((s.pending AND s.source='worker') OR
        (s.reason='worker_unavailable' AND s.source LIKE 'worker:'||j.attempt_id||':%'))
 ) affected ORDER BY flow_id;
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER console_worker_changed AFTER INSERT OR UPDATE ON workers
 FOR EACH ROW EXECUTE FUNCTION console_worker_changed();
"""
