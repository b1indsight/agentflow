"""Upgrade the split audit/receipt schema without changing the meaning of either record."""

import psycopg
import pytest

from tests.test_flow_state_migration import LEGACY_WEBHOOK_SCHEMA


@pytest.fixture
def split_events(store):
    flow = store.flows.create_flow("owner/repo", 1, None)
    with store.connection() as conn:
        conn.execute("DROP TABLE events; DROP TABLE IF EXISTS webhook_inbox")
        conn.execute("""CREATE TABLE events (
            delivery_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, flow_id BIGINT REFERENCES flows(id),
            payload JSONB, processed_at TIMESTAMPTZ NOT NULL)""")
        conn.execute(LEGACY_WEBHOOK_SCHEMA)
        conn.execute("""INSERT INTO events VALUES
            ('direct','issues',NULL,'{"direct":true}','2026-01-02'),
            ('both','issues',%s,'{"legacy":true}','2026-01-01')""", (flow.id,))
        conn.execute("""INSERT INTO webhook_inbox
            (inbox_position,delivery_id,event_type,payload,body_digest,received_at,state,attempts,
             next_at,claim_token,claim_expires_at,processed_at,error_code) OVERRIDING SYSTEM VALUE VALUES
            (52,'both','issues',NULL,'abc','2026-01-01','done',1,'2026-01-01',NULL,NULL,'2026-01-03',NULL),
            (96,'receipt','issues','{}','def','2026-01-04','processing',3,'2026-01-05',
             'token','2026-01-06',NULL,'TransientError')""")
    return store


def test_upgrade_and_restart_preserve_each_event_and_receipt(split_events):
    store = split_events
    with store.connection() as conn:
        receipts = conn.execute("SELECT * FROM webhook_inbox ORDER BY delivery_id").fetchall()
        registrations = conn.execute("SELECT delivery_id,flow_id,processed_at AS recorded_at FROM events").fetchall()
    store.init_db()
    store.init_db()
    with store.connection() as conn:
        assert conn.execute("SELECT to_regclass('webhook_inbox') AS old").fetchone()["old"] is None
        rows = {r['delivery_id']: r for r in conn.execute("SELECT * FROM events")}
    assert set(rows) == {'receipt', 'both', 'direct'}
    for receipt in receipts:
        assert receipt.items() <= rows[receipt['delivery_id']].items()
    for registration in registrations:
        assert registration.items() <= rows[registration['delivery_id']].items()
    assert rows['direct']['state'] is None and rows['direct']['payload'] == {'direct': True}
    assert rows['receipt']['recorded_at'] is None
    store.webhooks.receive('new', 'issues', {}, 'new-digest')
    with store.connection() as conn:
        assert conn.execute("SELECT inbox_position FROM events WHERE delivery_id='new'").fetchone()['inbox_position'] > 96


@pytest.mark.parametrize("conflict", ["type", "payload"])
def test_inconsistent_shared_event_rolls_back_upgrade(split_events, conflict):
    # Conflict between two non-erased originals cannot be reconciled into one row.
    store = split_events
    with store.connection() as conn:
        if conflict == "type":
            conn.execute("UPDATE events SET event_type='other' WHERE delivery_id='both'")
        else:
            conn.execute("""UPDATE webhook_inbox SET payload='{"other":true}' WHERE delivery_id='both'""")
    with pytest.raises(psycopg.errors.RaiseException, match="conflicting webhook"):
        store.init_db()
    with store.connection() as conn:
        assert conn.execute("SELECT processed_at FROM events WHERE delivery_id='both'").fetchone()['processed_at']
        assert conn.execute("SELECT count(*) n FROM webhook_inbox").fetchone()['n'] == 2
