"""Effect receipt markers are hidden from conversation display reads."""

from datetime import timedelta

import pytest

from app.contracts.console import ConsoleFailure
from app.github.console import Observation
from app.persistence.activity import ActivityStore

MARKER = "<!-- flowtool:effect:" + "a" * 64 + " -->"


def test_effect_markers_are_hidden_from_preview_content_and_diffs(store):
    # A generated receipt can begin inside the old preview window but must never leak from it.
    flow = store.flows.create_flow("octo/repo", 7, 1)
    archive = ActivityStore(store._db)
    body = "x" * 1_995 + "\n\n" + MARKER
    original = Observation(
        key="comment:7",
        source="issue.comments",
        kind="comment",
        author="bot",
        body=body,
        occurred_at=flow.created_at,
        updated_at=flow.created_at,
    )
    assert archive.append(flow.id, original)
    item = archive.page(flow.id).items[0]
    assert item.body == "x" * 1_995
    assert item.body_length == 1_995
    assert archive.content(flow.id, item.key, item.version, 1_990, 20).text == "x" * 5
    long = original.model_copy(
        update={
            "key": "comment:long",
            "body": "😀" * 16_380 + "\n\n" + MARKER,
        }
    )
    assert archive.append(flow.id, long)
    long_version = archive.versions(flow.id, long.key).items[0].version
    continuation = archive.content(flow.id, long.key, long_version, 16_375, 20)
    assert continuation.text == "😀" * 5
    assert continuation.total == 16_380 and continuation.next_offset is None
    before_marker = archive.page(flow.id).snapshot

    # An archived marker-only remote revision is not a visible edit or diff predecessor.
    assert archive.append(
        flow.id,
        original.model_copy(
            update={
                "body": "x" * 1_995 + "\n\n" + MARKER.replace("a" * 64, "b" * 64),
                "updated_at": flow.created_at + timedelta(seconds=1),
            }
        ),
    )
    marker_only = archive.page(flow.id, since=before_marker).items[0]
    assert marker_only.previous_version is None
    assert archive.page(flow.id, since=before_marker).changes == {}
    with pytest.raises(ConsoleFailure, match="previous_version_not_found"):
        archive.content(flow.id, marker_only.key, marker_only.version, 0, compare=True)

    assert archive.append(
        flow.id,
        original.model_copy(
            update={
                "body": "changed\n\n" + MARKER,
                "updated_at": flow.created_at + timedelta(seconds=2),
            }
        ),
    )
    changed = archive.page(flow.id).items[0]
    diff = archive.content(flow.id, changed.key, changed.version, 0, compare=True)
    assert MARKER not in diff.text and "+changed" in diff.text
    assert archive.append(
        flow.id,
        original.model_copy(
            update={
                "body": "changed\n\n" + MARKER,
                "metadata": {"state": "resolved"},
                "updated_at": flow.created_at + timedelta(seconds=3),
            }
        ),
    )
    assert archive.page(flow.id, since=archive.page(flow.id).snapshot - 1).changes == {"edited": 1}
    assert archive.append(
        flow.id,
        original.model_copy(
            update={"deleted": True, "updated_at": flow.created_at + timedelta(seconds=4)}
        ),
    )
    deleted_snapshot = archive.page(flow.id).snapshot
    assert archive.page(flow.id, since=deleted_snapshot - 1).changes == {"deleted": 1}
    assert archive.page(flow.id, since=deleted_snapshot).changes == {}


def test_display_preserves_worker_literals_and_unrelated_html(store):
    flow = store.flows.create_flow("octo/repo", 7, 1)
    archive = ActivityStore(store._db)
    literal = "literal " + MARKER
    unrelated = "note\n\n<!-- flowtool:effect:not-a-digest -->"
    observations = (("worker:1", "worker", literal), ("comment:1", "issue.comments", unrelated))
    for key, source, body in observations:
        assert archive.append(
            flow.id,
            Observation(
                key=key,
                source=source,
                kind="message",
                author="author",
                body=body,
                occurred_at=flow.created_at,
                updated_at=flow.created_at,
            ),
        )
    displayed = {item.key: item.body for item in archive.page(flow.id, limit=20).items}
    assert displayed == {"worker:1": literal, "comment:1": unrelated}
