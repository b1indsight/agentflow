"""Append-only round activity using the existing console publication fence."""

ROUND_AUDIT_SCHEMA = """
CREATE OR REPLACE FUNCTION console_round_changed() RETURNS TRIGGER AS $$
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
 -- Share activity allocation with handlers and console_refresh, never a separate sequence.
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
 -- console_facts joins only current_round_no: historical edits cannot alter a new round.
 PERFORM console_refresh(NEW.flow_id);
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER console_round_changed AFTER INSERT OR UPDATE ON flow_rounds
 FOR EACH ROW EXECUTE FUNCTION console_round_changed();
"""
