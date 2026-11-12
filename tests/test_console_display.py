"""Conversation display reads hide transport wrappers without changing archived evidence."""

import json
from datetime import timedelta

import pytest

from app.contracts.console import ConsoleFailure
from app.controler.workflow.service import ControlService
from app.domain.models import ArchiveReason, TaskContent
from app.github.console import Observation
from app.persistence.activity import ActivityStore
from app.persistence.console import ConsoleStore

MARKER = "<!-- flowtool:effect:" + "a" * 64 + " -->"


def test_direct_task_title_overrides_remote_cache_and_body_is_only_loaded_in_detail(store):
    # Cached remote titles must not overwrite the direct task or make its original input unsearchable.
    task = TaskContent(title="直接任务标题", body="Private original requirements 😀")
    flow = store.flows.create_flow("octo/repo", None, 1, task_content=task)
    console = ConsoleStore(store.database)
    with store.connection() as connection:
        connection.execute("UPDATE console_flows SET title='Stale remote title' WHERE flow_id=%s", (flow.id,))
        facts = connection.execute("SELECT facts FROM console_flows WHERE flow_id=%s", (flow.id,)).fetchone()["facts"]
        history = connection.execute("SELECT body FROM console_activity WHERE flow_id=%s", (flow.id,)).fetchall()
    assert facts["task_title"] == task.title
    assert "task_content" not in facts and task.body not in json.dumps(facts, ensure_ascii=False)
    assert all(task.body not in row["body"] for row in history)
    page = console.flows(q=task.title)
    assert [item.id for item in page.items] == [flow.id]
    assert page.items[0].title == task.title
    assert page.items[0].issue_number is page.items[0].issue_url is None
    assert not console.flows(q="Stale remote title").items
    assert "task_content" not in page.model_dump_json()
    detail = console.detail(flow.id)
    assert detail.task_content == task and detail.issue_url is None
    issue = store.flows.create_flow("octo/repo", 7, 1)
    assert console.detail(issue.id).task_content is None
    assert console.detail(issue.id).issue_url == "https://github.com/octo/repo/issues/7"


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

    # Opening another round must not rewrite prior reading versions or expose receipt markers.
    service = ControlService(database=store.database)
    service.finish(flow, reason=ArchiveReason.SUCCEEDED, summary="调查完成", expected_round_no=1)
    service.resume(store.flows.get_flow(flow.id), expected_round_no=1)
    assert store.flows.get_flow(flow.id).current_round_no == 2
    assert archive.content(flow.id, changed.key, changed.version, 0, compare=True) == diff
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


@pytest.mark.parametrize("revision", [False, True])
def test_coding_summary_display_preserves_archives_paging_and_version_comparisons(store, revision):
    # Existing archives contain the coding-result envelope, often longer than the preview.
    flow = store.flows.create_flow("octo/repo", 7, 1)
    archive = ActivityStore(store._db)
    summary = "## 完成\n\n- **修复** JSON 展示\n\n" + "中文😀带引号\"和反斜线\\\n" * 210
    payload: dict[str, object] = {"summary": summary, "vcs_backend_used": "jj" if revision else "git"}
    if revision:
        payload.update(addressed=[], deferred=[], needs_clarification=[])
    raw = json.dumps(payload)
    original = Observation(
        key="worker:summary",
        source="worker",
        kind="message",
        author="assistant",
        body=raw,
        occurred_at=flow.created_at,
        updated_at=flow.created_at,
    )
    assert archive.append(flow.id, original)
    page = archive.page(flow.id)
    item = page.items[0]
    assert item.body == summary[:2000]
    assert item.body_length == len(summary)
    text, offset = "", 0
    while True:
        part = archive.content(flow.id, item.key, item.version, offset, 997)
        assert part.total == len(summary)
        text += part.text
        if part.next_offset is None:
            break
        offset = part.next_offset
    assert text == summary
    assert archive.versions(flow.id, item.key).items == page.items
    with store._db.connection() as connection:
        row = connection.execute(
            "SELECT body FROM console_activity WHERE flow_id=%s AND key=%s AND version=%s",
            (flow.id, item.key, item.version),
        ).fetchone()
    assert row["body"] == raw

    # Envelope formatting/backend changes do not make an unchanged summary look edited.
    payload["vcs_backend_used"] = "git" if revision else "jj"
    assert archive.append(
        flow.id,
        original.model_copy(update={
            "body": json.dumps(payload, ensure_ascii=False, indent=2),
            "updated_at": flow.created_at + timedelta(seconds=1),
        }),
    )
    unchanged = archive.page(flow.id, since=page.snapshot)
    assert unchanged.changes == {}
    assert unchanged.items[0].previous_version is None

    payload["summary"] = summary.replace("JSON 展示", "摘要展示")
    assert archive.append(
        flow.id,
        original.model_copy(update={
            "body": json.dumps(payload, ensure_ascii=False),
            "updated_at": flow.created_at + timedelta(seconds=2),
        }),
    )
    changed = archive.page(flow.id, since=unchanged.snapshot)
    assert changed.changes == {"edited": 1}
    diff = archive.content(flow.id, item.key, changed.items[0].version, 0, compare=True)
    assert diff.previous_version == unchanged.items[0].version
    assert "-- **修复** JSON 展示" in diff.text and "+- **修复** 摘要展示" in diff.text
    assert "vcs_backend_used" not in diff.text
    assert archive.page(flow.id, snapshot=page.snapshot).items == page.items


def test_summary_display_leaves_user_tool_and_non_result_json_unchanged(store):
    flow = store.flows.create_flow("octo/repo", 7, 1)
    archive = ActivityStore(store._db)
    result = json.dumps({"summary": "完成", "vcs_backend_used": "git"})
    cases = [
        ("worker", "user", "message", result),
        ("worker", "system", "message", result),
        ("worker", "assistant", "tool_call", result),
        ("worker", "tool", "tool_result", result),
        ("issue.comments", "assistant", "message", result),
    ]
    # Invalid or incomplete native output must remain readable, never fail a whole page.
    cases.extend(("worker", "assistant", "message", body) for body in (
        result[:-1],
        "## 普通 Markdown",
        '{"summary":"普通数据"}',
        '{"summary":42,"vcs_backend_used":"git"}',
        '{"summary":null,"vcs_backend_used":"git"}',
        '{"summary":"data","vcs_backend_used":"unknown"}',
        r'{"summary":"\u0000","vcs_backend_used":"git"}',
        r'{"summary":"\ud800","vcs_backend_used":"git"}',
        '[]',
        'null',
    ))
    expected = {}
    for i, (source, author, kind, body) in enumerate(cases):
        key = f"record:{i}"
        expected[key] = body
        assert archive.append(flow.id, Observation(
            key=key,
            source=source,
            kind=kind,
            author=author,
            body=body,
            occurred_at=flow.created_at,
            updated_at=flow.created_at,
        ))
    page = archive.page(flow.id)
    assert {item.key: item.body for item in page.items} == expected
    for item in page.items:
        content = archive.content(flow.id, item.key, item.version, 0)
        assert content.text == expected[item.key]
        assert item.body_length == content.total == len(content.text)
