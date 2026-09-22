"""Read operational facts on a dedicated, read-only connection off the event loop."""

from __future__ import annotations

import psycopg
from pydantic import BaseModel

from app.reconcile_store import PR_RECONCILE_ELIGIBLE

# One statement gives a consistent snapshot and one total SQL execution budget.
SNAPSHOT_SQL = f"""
WITH due_reconcile AS (
 SELECT r.next_at FROM flow_reconcile r JOIN flows f ON f.id=r.flow_id
 JOIN flow_deliveries d ON d.flow_id=f.id WHERE r.next_at<=now() AND {PR_RECONCILE_ELIGIBLE}
), ages AS (
 SELECT 'inbox' AS queue, min(received_at) AS oldest FROM webhook_inbox WHERE state != 'done'
 UNION ALL SELECT 'jobs', min(created_at) FROM jobs WHERE state='queued'
 UNION ALL SELECT 'recovery', min(result_received_at) FROM jobs
   WHERE result_received_at IS NOT NULL AND result_applied_at IS NULL
 UNION ALL SELECT 'archive', min(created_at) FROM jobs WHERE kind='archive' AND state IN ('queued','running','failed')
 UNION ALL SELECT 'reconcile', min(next_at) FROM due_reconcile
 UNION ALL SELECT 'projects', min(next_at) FROM project_projections WHERE desired_version > synced_version
)
SELECT 'inbox' AS family, state AS a, '' AS b, count(*)::float8 AS value FROM webhook_inbox GROUP BY state
UNION ALL SELECT 'jobs', kind, state, count(*) FROM jobs GROUP BY kind,state
UNION ALL SELECT 'workers', status, '', count(*) FROM workers GROUP BY status
UNION ALL SELECT 'bindings_active', '', '', count(*) FROM flow_executions WHERE released_at IS NULL
UNION ALL SELECT 'recovery_pending', '', '', count(*) FROM jobs
 WHERE result_received_at IS NOT NULL AND result_applied_at IS NULL
UNION ALL SELECT 'effects', state, '', count(*) FROM result_effects GROUP BY state
UNION ALL SELECT 'projects', CASE WHEN blocked THEN 'blocked' ELSE 'pending' END, '', count(*)
 FROM project_projections WHERE desired_version > synced_version GROUP BY blocked
UNION ALL SELECT 'reconcile_due', '', '', count(*) FROM due_reconcile
UNION ALL SELECT 'oldest_age_seconds', queue, '', COALESCE(GREATEST(0,extract(epoch FROM now()-oldest)),0) FROM ages
UNION ALL SELECT 'execution_receipts', kind, '', count(*) FROM jobs
 WHERE started_at IS NOT NULL AND result_received_at IS NOT NULL GROUP BY kind
UNION ALL SELECT 'execution_elapsed_seconds', kind, '', sum(GREATEST(0,extract(epoch FROM result_received_at-started_at)))
 FROM jobs WHERE started_at IS NOT NULL AND result_received_at IS NOT NULL GROUP BY kind
"""


class Sample(BaseModel):
    family: str
    labels: list[str]
    value: float


class Snapshot(BaseModel):
    samples: list[Sample]


def read_snapshot(database_url: str) -> Snapshot:
    """Never borrow dispatcher pool slots; connect <=2s, SQL statement <=2s."""
    with psycopg.connect(database_url, connect_timeout=2) as conn:
        conn.read_only = True
        conn.execute("SET LOCAL statement_timeout = '2s'")
        rows = conn.execute(SNAPSHOT_SQL).fetchall()
    return Snapshot(samples=[Sample(family=r[0], labels=[v for v in r[1:3] if v], value=r[3]) for r in rows])
