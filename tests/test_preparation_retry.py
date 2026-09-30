"""Only proven un-dispatched read preparation may retry on the original binding."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.control.gateway import WorkerGateway
from app.control.lifecycle import pause_delivery
from app.control.preparation import PreparationStale
from app.github_models import GitHubAdapterError, GitHubErrorKind
from app.models import JobKind, JobState, ReviewItemKind
from app.protocol import PROTOCOL_VERSION, RevisionTask, TriageTask
from app.stop_store import StopStore
from app.store import Store
from tests.test_coordinator import coordinator
from tests.test_review_batches import feedback, ingest
from tests.test_revision_delivery import verified_result
from tests.test_revision_recovery import accept
from tests.test_revision_recovery import (
    pending_revision as pending_revision,  # noqa: PLC0414 - fixture re-export
)


def gateway(store, subject):
    return WorkerGateway(store, subject, execution_control=StopStore(store), worker_tokens={})


async def test_transient_read_requeues_and_recovers_without_a_revision_cycle(store, pending_revision, monkeypatch):
    flow, job, task, github, subject = pending_revision
    original = github.get_ref

    async def unavailable(*args):
        raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "get_ref", status_code=503)

    monkeypatch.setattr(github, "get_ref", unavailable)
    prepared, secret = await gateway(store, subject)._prepare_assignment(job, "worker-a", "token", "epoch")
    assert prepared is JobState.QUEUED and secret is None
    pending = store.get_job(job.id)
    assert pending.prepare_failures == 1 and pending.prepare_next_at > store.now()
    assert pending.state is JobState.QUEUED and pending.result_received_at is None
    assert store.get_attempt(task.attempt_id).revision_cycles == 0
    assert store.get_flow_execution(flow.id).released_at is None
    assert store.assign_job("worker-a", "too-early", "epoch") is None
    future = pending.prepare_next_at + timedelta(seconds=1)
    monkeypatch.setattr(store, "_clock", lambda: future)
    monkeypatch.setattr(github, "get_ref", original)
    retry = store.assign_job("worker-a", "retry", "epoch")
    prepared, secret = await gateway(store, subject)._prepare_assignment(retry, "worker-a", "retry", "epoch")
    assert isinstance(prepared, RevisionTask) and secret is not None
    assert store.get_job(job.id).prepare_failures == 0
    assert store.get_job(job.id).prepare_first_failed_at is None


async def test_retry_after_and_count_survive_restart(store, pending_revision, monkeypatch):
    _, job, _, github, subject = pending_revision

    async def limited(*args):
        raise GitHubAdapterError(GitHubErrorKind.RATE_LIMITED, "get_ref", retry_after="120")

    monkeypatch.setattr(github, "get_ref", limited)
    await gateway(store, subject)._prepare_assignment(job, "worker-a", "token", "epoch")
    pending = store.get_job(job.id)
    assert pending.prepare_next_at >= pending.prepare_first_failed_at + timedelta(seconds=120)
    restarted = Store(store.database_url, clock=lambda: pending.prepare_next_at + timedelta(seconds=1))
    try:
        retry = restarted.assign_job("worker-a", "retry", "epoch")
        await gateway(restarted, coordinator(restarted, github))._prepare_assignment(retry, "worker-a", "retry", "epoch")
        again = restarted.get_job(job.id)
        assert again.prepare_failures == 2
        assert again.prepare_first_failed_at == pending.prepare_first_failed_at
    finally:
        restarted.close()


async def test_old_permanent_read_failure_cannot_fail_reassigned_started_triage(
    store, pending_revision, monkeypatch,
):
    flow, old, _, github, subject = pending_revision
    store.cancel_jobs(flow.id)
    assert store.finish_assignment(old.id, "worker-a", "token", JobState.DONE, "epoch")
    ingest(store, flow, feedback(SimpleNamespace(now=store.now()), 102), "new-feedback")
    store.reviews.reopened(flow.id, 0)
    store.reviews.claim_due()
    job = store.assign_job("worker-a", "old", "epoch")
    assert job.kind is JobKind.TRIAGE

    async def late_failure(*args):
        # A delayed response belongs to the old preparation, even after the new owner starts.
        assert store.requeue_assignment(job.id, "worker-a", "old", "epoch")
        replacement = store.assign_job("worker-a", "new", "epoch")
        assert store.start_assignment(replacement.id, flow.current_attempt_id, "worker-a", "new", "epoch")
        raise GitHubAdapterError(GitHubErrorKind.PERMISSION, "get_issue", status_code=403)

    monkeypatch.setattr(github, "get_issue", late_failure)
    with pytest.raises(PreparationStale):
        await subject.prepare_for_assignment(job, "worker-a", "old", "epoch")
    current = store.get_job(job.id)
    assert current.state is JobState.RUNNING and current.assignment_token == "new"
    assert current.started_at is not None and current.result_received_at is None
    assert store.get_flow_execution(flow.id).released_at is None


@pytest.mark.parametrize("maximum,header", [(1, None), (5, "901")])
async def test_failure_or_time_budget_stops_without_early_retry(store, pending_revision, monkeypatch, maximum, header):
    _, job, _, github, subject = pending_revision
    subject.settings.prepare_max_failures = maximum

    async def unavailable(*args):
        raise GitHubAdapterError(GitHubErrorKind.RATE_LIMITED, "get_ref", retry_after=header)

    monkeypatch.setattr(github, "get_ref", unavailable)
    prepared, _ = await gateway(store, subject)._prepare_assignment(job, "worker-a", "token", "epoch")
    assert prepared is JobState.FAILED
    failed = store.get_job(job.id)
    assert failed.result_applied_at is not None and failed.prepare_next_at is None
    assert "budget" in failed.last_error or "window" in failed.last_error
    assert "get_ref" in failed.prepare_last_error and "rate_limited" in failed.prepare_last_error
    assert failed.prepare_last_error in failed.last_error


@pytest.mark.parametrize("change", ["started", "epoch", "token", "result"])
def test_defer_requires_the_exact_unstarted_assignment(store, pending_revision, change):
    flow, job, task, _, _ = pending_revision
    if change == "started":
        store.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    elif change == "epoch":
        store.worker_connected("worker-a", PROTOCOL_VERSION, "new-epoch")
    elif change == "result":
        accept(store, job, verified_result(task))
    else:
        assert store.requeue_assignment(job.id, "worker-a", "token", "epoch")
        assert store.assign_job("worker-a", "new-token", "epoch") is not None
    result = store.defer_preparation(job.id, "worker-a", "token", "epoch",
        GitHubAdapterError(GitHubErrorKind.TRANSIENT, "get_ref"), max_failures=5, window_seconds=900)
    assert result is None
    assert store.get_job(job.id).prepare_failures == 0
    assert store.get_flow_execution(flow.id).released_at is None


async def test_close_wins_over_a_read_failure(store, pending_revision, monkeypatch):
    flow, job, _, github, subject = pending_revision

    async def close(*args):
        with store.transaction() as tx:
            tx.lock_workflow(flow.id)
            pause_delivery(tx, tx.get_delivery(flow.id))
        raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "get_ref")

    monkeypatch.setattr(github, "get_ref", close)
    prepared, _ = await gateway(store, subject)._prepare_assignment(job, "worker-a", "token", "epoch")
    assert prepared is JobState.DONE
    assert store.get_job(job.id).prepare_next_at is None
    assert store.get_flow_execution(flow.id).released_at is None


async def test_capability_signing_failure_never_enters_the_read_retry_budget(store, pending_revision, monkeypatch):
    _, job, _, github, subject = pending_revision

    async def fail(*args):
        raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "create_push_capability")

    monkeypatch.setattr(github, "create_push_capability", fail)
    prepared, _ = await gateway(store, subject)._prepare_assignment(job, "worker-a", "token", "epoch")
    assert prepared is JobState.FAILED
    assert store.get_job(job.id).prepare_failures == 0
    assert store.get_job(job.id).state is JobState.FAILED


async def test_expired_window_stops_before_another_github_request(store, pending_revision, monkeypatch):
    _, job, _, github, subject = pending_revision
    calls = []

    async def unavailable(*args):
        calls.append(args)
        raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "get_ref")

    monkeypatch.setattr(github, "get_ref", unavailable)
    await gateway(store, subject)._prepare_assignment(job, "worker-a", "token", "epoch")
    first = store.get_job(job.id).prepare_first_failed_at
    monkeypatch.setattr(store, "_clock", lambda: first + timedelta(seconds=901))
    retry = store.assign_job("worker-a", "expired", "epoch")
    prepared, _ = await gateway(store, subject)._prepare_assignment(retry, "worker-a", "expired", "epoch")
    assert prepared is JobState.FAILED and len(calls) == 1
    assert store.get_job(job.id).prepare_failures == 1
    assert "get_ref" in store.get_job(job.id).prepare_last_error


async def test_partial_read_round_is_not_frozen_or_combined_with_retry(store, pending_revision, monkeypatch):
    flow, old_job, _, github, subject = pending_revision
    store.cancel_jobs(flow.id)
    assert store.finish_assignment(old_job.id, "worker-a", "token", JobState.DONE, "epoch")
    from types import SimpleNamespace

    clock = SimpleNamespace(now=store.now())
    ingest(store, flow, feedback(clock, 102, kind=ReviewItemKind.INLINE), "inline")
    store.reviews.reopened(flow.id, 0)
    queued = store.reviews.claim_due()[0]
    job = store.assign_job("worker-a", "triage", "epoch")
    original_issue, original_threads = github.get_issue, github.list_review_threads
    rounds = []

    async def threads(*args):
        value = await original_threads(*args)
        rounds.append(value)
        return value

    async def unavailable(*args):
        raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "get_issue")

    monkeypatch.setattr(github, "list_review_threads", threads)
    monkeypatch.setattr(github, "get_issue", unavailable)
    prepared, _ = await gateway(store, subject)._prepare_assignment(job, "worker-a", "triage", "epoch")
    assert prepared is JobState.QUEUED and queued.kind is JobKind.TRIAGE
    batch = store.reviews.for_job(job)
    assert batch.context is None and batch.threads is None
    future = store.get_job(job.id).prepare_next_at + timedelta(seconds=1)
    monkeypatch.setattr(store, "_clock", lambda: future)
    monkeypatch.setattr(github, "get_issue", original_issue)
    github.sha = "fresh-head"
    retry = store.assign_job("worker-a", "fresh", "epoch")
    prepared, _ = await gateway(store, subject)._prepare_assignment(retry, "worker-a", "fresh", "epoch")
    assert isinstance(prepared, TriageTask) and prepared.context.pull_request.head_sha == "fresh-head"
    assert len(rounds) == 2 and store.reviews.for_job(job).threads == rounds[-1]


def test_another_worker_can_progress_while_original_binding_waits(store, pending_revision):
    flow, job, _, _, _ = pending_revision
    assert store.defer_preparation(job.id, "worker-a", "token", "epoch",
        GitHubAdapterError(GitHubErrorKind.TRANSIENT, "get_ref"), max_failures=5, window_seconds=900) is JobState.QUEUED
    other = store.create_flow("owner/other", 8, 42)
    store.enqueue_job(other.id, JobKind.INITIAL)
    store.worker_connected("worker-b", PROTOCOL_VERSION, "other-epoch")
    assigned = store.assign_job("worker-b", "other-token", "other-epoch")
    assert assigned.flow_id == other.id
    assert store.get_flow_execution(flow.id).worker_id == "worker-a"


def test_retry_budget_and_requeue_are_one_transaction(store, pending_revision, monkeypatch):
    _, job, _, _, _ = pending_revision
    original = Store.requeue_assignment

    def crash(self, *args):
        original(self, *args)
        raise RuntimeError("before commit")

    monkeypatch.setattr(Store, "requeue_assignment", crash)
    with pytest.raises(RuntimeError):
        store.defer_preparation(job.id, "worker-a", "token", "epoch",
            GitHubAdapterError(GitHubErrorKind.TRANSIENT, "get_ref"), max_failures=5, window_seconds=900)
    assert store.get_job(job.id).state is JobState.RUNNING
    assert store.get_job(job.id).prepare_failures == 0


async def test_a_slow_retry_read_cannot_run_beyond_the_window(store, pending_revision, monkeypatch):
    _, job, _, github, subject = pending_revision
    store.defer_preparation(job.id, "worker-a", "token", "epoch",
        GitHubAdapterError(GitHubErrorKind.TRANSIENT, "get_ref"), max_failures=5, window_seconds=900)
    first = store.get_job(job.id).prepare_first_failed_at
    monkeypatch.setattr(store, "_clock", lambda: first + timedelta(seconds=10))
    subject.settings.prepare_retry_window_seconds = 10.01
    retry = store.assign_job("worker-a", "slow", "epoch")

    async def slow(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(github, "get_ref", slow)
    prepared, _ = await gateway(store, subject)._prepare_assignment(retry, "worker-a", "slow", "epoch")
    assert prepared is JobState.FAILED
    assert "window" in store.get_job(job.id).last_error


@pytest.mark.parametrize("field,value", [("prepare_max_failures", 0), ("prepare_retry_window_seconds", 0),
                                         ("prepare_retry_window_seconds", float("inf"))])
def test_preparation_retry_limits_are_validated(tmp_path, field, value):
    from pydantic import ValidationError

    from app.config import ServeSettings
    from tests.test_github_client import settings

    with pytest.raises(ValidationError):
        ServeSettings.model_validate(settings(tmp_path).model_dump() | {field: value})
