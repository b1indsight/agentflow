"""Fresh installations use the current schema without historical upgrade state."""

from app.domain.models import FlowState


def test_current_schema_initialization_is_idempotent(store):
    flow = store.create_flow("owner/repo", 8, 42)
    store.init_db()
    current = store.get_flow(flow.id)
    assert current is not None and current.state is FlowState.ACTIVE
    with store.connection() as connection:
        assert connection.execute("SELECT to_regclass('review_threads') AS name").fetchone()["name"] is None
        assert connection.execute("SELECT to_regclass('review_migrations') AS name").fetchone()["name"] is None
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
