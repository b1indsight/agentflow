"""Additive personal-progress schema; archive publication keeps its existing Flow lock."""

READING_SCHEMA = r"""
ALTER TABLE console_activity ADD COLUMN IF NOT EXISTS read_revision BIGINT;
CREATE TABLE IF NOT EXISTS console_reading_state (
 owner_key TEXT NOT NULL, flow_id BIGINT NOT NULL REFERENCES console_flows(flow_id) ON DELETE CASCADE,
 receipts_version BIGINT NOT NULL DEFAULT 0, position_revision BIGINT NOT NULL DEFAULT 0,
 writer_epoch BIGINT NOT NULL DEFAULT 0, writer_session UUID, last_sequence BIGINT NOT NULL DEFAULT 0,
 anchor JSONB, saved_at TIMESTAMPTZ, PRIMARY KEY(owner_key,flow_id)
);
CREATE TABLE IF NOT EXISTS console_read_receipts (
 owner_key TEXT NOT NULL, flow_id BIGINT NOT NULL, key TEXT NOT NULL, read_revision BIGINT NOT NULL,
 generation BIGINT NOT NULL DEFAULT 0, header_read BOOLEAN NOT NULL DEFAULT FALSE,
 ranges INT8MULTIRANGE NOT NULL DEFAULT '{}', explicit_read BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(owner_key,flow_id,key,read_revision),
 FOREIGN KEY(owner_key,flow_id) REFERENCES console_reading_state(owner_key,flow_id) ON DELETE CASCADE
);

CREATE OR REPLACE FUNCTION console_read_content(a console_activity) RETURNS JSONB AS $$
 SELECT jsonb_build_array(a.source,a.kind,a.author,a.title,
 CASE WHEN a.source='worker' THEN a.body ELSE
 regexp_replace(a.body,E'\n\n<!-- flowtool:effect:[0-9a-f]{64} -->$','') END,
 a.parent_key,a.deleted,
 CASE a.kind
 WHEN 'thread' THEN jsonb_build_array(a.metadata->'resolved',a.metadata->'outdated',
                                     a.metadata->'path',a.metadata->'line')
 WHEN 'check' THEN jsonb_build_array(a.metadata->'workflow_name',a.metadata->'status',
                                    a.metadata->'conclusion')
 WHEN 'reaction' THEN jsonb_build_array(a.metadata->'content',a.metadata->'count')
 WHEN 'event' THEN jsonb_build_array(a.metadata->'event',a.metadata->'label',
 a.metadata->'assignee',a.metadata->'milestone',a.metadata->'requested_reviewer',
 a.metadata->'requested_team',a.metadata->'state')
 ELSE NULL END)
$$ LANGUAGE SQL IMMUTABLE;

CREATE OR REPLACE FUNCTION console_read_revision(a console_activity) RETURNS BIGINT AS $$
DECLARE previous console_activity;
BEGIN
 SELECT * INTO previous FROM console_activity WHERE flow_id=a.flow_id AND key=a.key
 AND version<a.version ORDER BY deleted DESC,updated_at DESC,version DESC LIMIT 1;
 IF previous.version IS NOT NULL AND previous.read_revision IS NOT NULL
 AND (a.deleted,a.updated_at,a.version)>(previous.deleted,previous.updated_at,previous.version)
 AND console_read_content(a)=console_read_content(previous) THEN
   RETURN previous.read_revision;
 END IF;
 RETURN a.version;
END;
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION console_set_read_revision() RETURNS TRIGGER AS $$
BEGIN
 NEW.read_revision=console_read_revision(NEW);
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER console_read_revision_insert BEFORE INSERT ON console_activity
 FOR EACH ROW EXECUTE FUNCTION console_set_read_revision();
"""
