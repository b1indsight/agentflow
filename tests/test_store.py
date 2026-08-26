import sqlite3
from collections.abc import Iterator
from datetime import timedelta

import pytest

import app.store as store_module
from app.models import FlowState, JobKind, JobState, ReviewItem, ReviewItemKind, utcnow
from app.store import Store


@pytest.fixture
def store(tmp_path) -> Iterator[Store]:
    instance = Store(tmp_path / "flowtool.sqlite3")
    instance.init_db()
    try:
        yield instance
    finally:
        instance.close()


def test_only_one_active_flow_exists_per_issue(store: Store) -> None:
    flow = store.create_flow("owner/repo", 7, installation_id=11)

    with pytest.raises(sqlite3.IntegrityError):
        store.create_flow("owner/repo", 7, installation_id=11)

    flow.state = FlowState.CANCELLED
    store.update_flow(flow)
    replacement = store.create_flow("owner/repo", 7, installation_id=11)

    assert replacement.id != flow.id
    assert store.get_active_flow("owner/repo", 7) == replacement


def test_attempt_and_job_lifecycle_round_trip(store: Store) -> None:
    flow = store.create_flow("owner/repo", 8, installation_id=12)
    attempt = store.create_attempt(flow.id)
    attempt.branch = "flow/issue-8"
    flow.current_attempt_id = attempt.id
    store.update_attempt(attempt)
    store.update_flow(flow)

    job = store.enqueue_job(flow.id, JobKind.INITIAL, payload={"issue_number": 8})
    claimed = store.claim_job()

    assert store.get_current_attempt(flow) == attempt
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.state is JobState.RUNNING
    assert claimed.attempt == 1
    assert claimed.payload == {"issue_number": 8}

    store.finish_job(claimed.id, JobState.DONE)
    finished = store.get_job(claimed.id)
    assert finished is not None
    assert finished.state is JobState.DONE


def test_stale_running_job_is_requeued(store: Store, monkeypatch) -> None:
    current_time = utcnow()
    monkeypatch.setattr(store_module, "utcnow", lambda: current_time)
    flow = store.create_flow("owner/repo", 9, installation_id=None)
    job = store.enqueue_job(flow.id, JobKind.INITIAL)
    claimed = store.claim_job()
    assert claimed is not None

    # A worker can die after claiming a job; an expired heartbeat must make that work retryable.
    current_time += timedelta(seconds=301)
    assert store.recover_stale_jobs(stale_after_seconds=300) == 1

    recovered = store.get_job(job.id)
    assert recovered is not None
    assert recovered.state is JobState.QUEUED
    assert recovered.pid is None


def test_review_item_upsert_preserves_one_feedback_record(store: Store) -> None:
    flow = store.create_flow("owner/repo", 10, installation_id=None)
    attempt = store.create_attempt(flow.id)
    item = ReviewItem(
        id=None,
        attempt_id=attempt.id,
        kind=ReviewItemKind.INLINE,
        github_id=101,
        author="reviewer",
        body="Please add a bound.",
    )
    store.upsert_review_item(item)
    item.body = "Please add a strict bound."
    store.upsert_review_item(item)
    store.mark_resolved(attempt.id, item.github_id)

    pending = store.unrelayed_items(attempt.id)
    assert len(pending) == 1
    assert pending[0].body == "Please add a strict bound."
    assert pending[0].resolved is True

    assert pending[0].id is not None
    store.mark_relayed([pending[0].id])
    assert store.unrelayed_items(attempt.id) == []
