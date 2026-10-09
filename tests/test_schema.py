"""Fresh installations use the current schema without historical upgrade state."""

from app.domain.models import FlowState
from app.persistence.store import Store


def test_current_schema_initialization_is_idempotent(store):
    flow = store.create_flow("owner/repo", 8, 42)
    store.init_db()
    current = store.get_flow(flow.id)
    assert current is not None and current.state is FlowState.ACTIVE
    with store.connection() as connection:
        assert connection.execute("SELECT to_regclass('review_threads') AS name").fetchone()["name"] is None
        assert connection.execute("SELECT to_regclass('review_migrations') AS name").fetchone()["name"] is None
        assert connection.execute("SELECT to_regclass('codex_runs') AS name").fetchone()["name"] is None
        assert connection.execute("SELECT to_regclass('ux_codex_runs_source') AS name").fetchone()["name"] is None
        columns = connection.execute(
            "SELECT table_name,column_name FROM information_schema.columns"
            " WHERE table_schema=current_schema()"
        ).fetchall()
    names = {(row["table_name"], row["column_name"]) for row in columns}
    assert not names & {("jobs", "result_recovery_version"), ("jobs", "pid"),
                        ("attempts", "branch"), ("attempts", "pr_number"), ("attempts", "worker_id"),
                        ("flow_tool_calls", "error")}
    assert {("jobs", "dispatch_authorized_at"), ("workers", "draining"),
            ("webhook_inbox", "inbox_position"), ("review_versions", "observation_seq")} <= names


def test_initialization_preserves_legacy_coding_audit(postgresql_url):
    # Upgrading an existing installation must retain audit rows for external SQL consumers.
    subject = Store(postgresql_url)
    try:
        subject.init_db()
        with subject.connection() as connection:
            connection.execute("CREATE TABLE codex_runs (id BIGINT PRIMARY KEY, prompt TEXT)")
            connection.execute("INSERT INTO codex_runs VALUES (1, 'legacy prompt')")
        subject.init_db()
        with subject.connection() as connection:
            assert connection.execute("SELECT * FROM codex_runs").fetchall() == [
                {"id": 1, "prompt": "legacy prompt"},
            ]
    finally:
        subject.close()
