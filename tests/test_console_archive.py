"""Console contracts exercised through real PostgreSQL publication boundaries."""

from datetime import timedelta

import pytest

from app.contracts.console import ConsoleFailure
from app.domain.models import DeliveryState, FlowState
from app.github.console import Observation
from app.persistence.activity import ActivityStore
from app.persistence.console import ConsoleStore


def test_archive_versions_snapshots_and_prior_interactions(store):
    flow = store.flows.create_flow("octo/repo", 7, 1)
    archive = ActivityStore(store._db)
    now = flow.created_at
    original = Observation(
        key="github:comment:9",
        source="issue.comments",
        kind="comment",
        author="alice",
        body="old" * 5000,
        occurred_at=now - timedelta(days=1),
        updated_at=now,
    )
    assert archive.append(flow.id, original)
    assert not archive.append(flow.id, original)
    before = archive.page(flow.id, limit=1)
    archive.append(
        flow.id,
        original.model_copy(update={"body": "edited", "updated_at": now + timedelta(seconds=1)}),
    )
    archive.append(
        flow.id,
        original.model_copy(
            update={"body": "", "deleted": True, "updated_at": now + timedelta(seconds=2)}
        ),
    )
    frozen = archive.page(flow.id, snapshot=before.snapshot, limit=20)
    old = next(item for item in frozen.items if item.key == original.key)
    assert old.prior and old.body_length == 15000
    assert (
        archive.content(flow.id, original.key, old.version, 2000, 1000).text
        == original.body[2000:3000]
    )
    assert archive.versions(flow.id, original.key, limit=20).total == 3
    current = archive.page(flow.id, since=before.snapshot, limit=20)
    assert next(item for item in current.items if item.key == original.key).deleted
    assert current.changes["deleted"] == 1
    with pytest.raises(ConsoleFailure):
        archive.page(flow.id + 1, cursor=before.next_cursor, limit=20)


def test_current_projection_and_history_rollback_with_facts(store):
    flow = store.flows.create_flow("octo/repo", 7, 1)
    console = ConsoleStore(store._db)
    before = console.detail(flow.id)
    with pytest.raises(RuntimeError), store.transaction() as tx:
        row = tx.flows.get_flow(flow.id)
        row.state = "failed"
        tx.flows.update_flow(row)
        raise RuntimeError("rollback")
    assert console.detail(flow.id) == before
    flow.state = "failed"
    store.flows.update_flow(flow)
    assert console.detail(flow.id).phase == "blocked"
    with store._db.connection() as connection:
        connection.execute("UPDATE console_flows SET facts='{}' WHERE flow_id=%s", (flow.id,))
    console.rebuild(flow.id)
    assert console.detail(flow.id).lifecycle == "failed"


def test_filter_counts_precede_column_pagination_and_payloads_are_private(store):
    for index in range(7):
        flow = store.flows.create_flow("octo/repo" if index < 6 else "other/repo", index + 1, 1)
        if index % 2:
            flow.state = "succeeded"
            store.flows.update_flow(flow)
    console = ConsoleStore(store._db)
    first = console.flows(q="repo:octo/repo", phase="queued", limit=2)
    assert first.counts["queued"] == 3 and first.counts["done"] == 3
    assert first.has_more
    second = console.flows(q="repo:octo/repo", phase="queued", limit=2, cursor=first.next_cursor)
    assert len(second.items) == 1 and not second.has_more
    assert not {f.id for f in first.items} & {f.id for f in second.items}
    with pytest.raises(ConsoleFailure, match="cursor"):
        console.flows(q="repo:other/repo", phase="queued", cursor=first.next_cursor)
    assert console.repos(limit=1).has_more
    assert "installation_id" not in first.model_dump_json()


def test_closed_pr_archive_filters_reopen_and_existing_projection_refresh(store):
    # Closing a PR archives its card, while preserving the lifecycle needed for a verified reopen.
    flow = store.flows.create_flow("octo/repo", 7, 1)
    delivery = store.deliveries.get_or_create_delivery(flow.id, "flow/7")
    delivery.pr_number, delivery.state = 17, DeliveryState.CLOSED
    store.deliveries.update_delivery(delivery)
    console = ConsoleStore(store._db)
    archived = console.flows(q="is:done", phase="done")
    assert [item.id for item in archived.items] == [flow.id]
    assert archived.counts["done"] == 1 and archived.counts["blocked"] == 0
    assert not console.flows(q="is:active").items
    assert [item.id for item in console.flows(q="status:done").items] == [flow.id]
    assert store.flows.get_flow(flow.id).state is FlowState.ACTIVE
    assert "未合并" in console.detail(flow.id).reason
    # An upgrade must also move a previously projected blocked card, once, without a GitHub event.
    with store.connection() as connection:
        connection.execute(
            "UPDATE console_flows SET facts=jsonb_set(facts,'{phase}','\"blocked\"') WHERE flow_id=%s",
            (flow.id,),
        )
    store.init_db()
    refreshed = console.detail(flow.id)
    assert refreshed.phase == "done"
    store.init_db()
    assert console.detail(flow.id) == refreshed
    delivery.state = DeliveryState.OPEN
    store.deliveries.update_delivery(delivery)
    assert console.detail(flow.id).phase == "review"
    assert not console.flows(q="is:done").items
    assert [item.id for item in console.flows(q="is:active").items] == [flow.id]


def test_late_description_cannot_regress_current_title_after_unchanged_newer_observation(store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    archive = ActivityStore(store._db)
    item = Observation(
        key="description:7",
        source="issue.description",
        kind="description",
        occurred_at=flow.created_at,
        updated_at=flow.created_at,
        author="owner",
        title="A",
    )
    archive.append(flow.id, item)
    archive.append(
        flow.id, item.model_copy(update={"updated_at": flow.created_at + timedelta(seconds=3)})
    )
    archive.append(
        flow.id,
        item.model_copy(
            update={"title": "B", "updated_at": flow.created_at + timedelta(seconds=2)}
        ),
    )
    assert ConsoleStore(store._db).detail(flow.id).title == "A"
    assert archive.versions(flow.id, item.key).total == 3
