"""Only proven un-dispatched read preparation may retry on the original binding."""

import asyncio
from datetime import timedelta

import pytest

from app.contracts.protocol import PROTOCOL_VERSION, Assignment, RevisionTask
from app.controler.workers.gateway import WorkerGateway
from app.controler.workflow.lifecycle import pause_delivery
from app.controler.workflow.preparation import PreparationStale
from app.domain.models import JobKind, JobState
from app.github import GitHubError, GitHubErrorKind, Repository
from app.persistence.store import Store
from tests.test_coordinator import coordinator
from tests.test_revision_recovery import (
    pending_revision as pending_revision,  # noqa: PLC0414 - fixture re-export
)
from tests.test_revision_recovery import revision_resources as revision_resources  # noqa: PLC0414


def gateway(store, subject):
    return WorkerGateway(store, subject, worker_tokens={})


@pytest.mark.parametrize("change,signing_fails", [
    ("merged", False), ("closed", True),
    ("epoch", False), ("epoch", True),
])
async def test_coordinator_rechecks_after_capability_io(store, pending_revision, monkeypatch, change, signing_fails):
    # Webhooks or a new connection can retire an assignment while signing is in flight.
    flow, job, _task, _github, subject = pending_revision
    original = Repository.create_push_capability

    async def changed(repo):
        capability = await original(repo)
        if change == "closed":
            with store.transaction() as tx:
                tx.lock_workflow(flow.id)
                pause_delivery(tx, tx.get_delivery(flow.id))
        elif change == "merged":
            with store.connection() as connection:
                connection.execute("UPDATE flow_deliveries SET state='merged' WHERE flow_id=%s", (flow.id,))
        else:
            store.worker_connected("worker-a", PROTOCOL_VERSION, "new-epoch")
        if signing_fails:
            raise GitHubError(GitHubErrorKind.TRANSIENT, "create_push_capability")
        return capability

    monkeypatch.setattr(Repository, "create_push_capability", changed)
    if change == "epoch":
        with pytest.raises(PreparationStale):
            await subject.prepare_for_assignment(job, "worker-a", "token", "epoch")
        assert store.get_job(job.id).result_received_at is None
    else:
        prepared = await subject.prepare_for_assignment(job, "worker-a", "token", "epoch")
        assert prepared is JobState.FAILED
    assert store.get_job(job.id).prepare_failures == 0


async def test_cancelling_preparation_joins_capability_io(store, pending_revision, monkeypatch):
    # Disconnect cleanup must wait for the signer to stop before releasing the connection.
    _, job, _, _github, subject = pending_revision
    entered, cleaning, release, ended = (asyncio.Event() for _ in range(4))

    async def blocked(repo):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            ended.set()

    monkeypatch.setattr(Repository, "create_push_capability", blocked)
    preparing = asyncio.create_task(gateway(store, subject)._prepare_assignment(job, "worker-a", "token", "epoch"))
    await entered.wait()
    preparing.cancel()
    try:
        await cleaning.wait()
        assert not preparing.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await preparing
    assert ended.is_set()
    assert store.get_job(job.id).result_received_at is None


async def test_transient_read_requeues_and_recovers_without_a_revision_cycle(store, pending_revision, monkeypatch):
    flow, job, task, _github, subject = pending_revision
    original = Repository.fetch_ref

    async def unavailable(repo, branch, *, retry_transient=False):
        raise GitHubError(GitHubErrorKind.TRANSIENT, "get_ref", status_code=503)

    monkeypatch.setattr(Repository, "fetch_ref", unavailable)
    prepared = await gateway(store, subject)._prepare_assignment(job, "worker-a", "token", "epoch")
    assert prepared is JobState.QUEUED
    pending = store.get_job(job.id)
    assert pending.prepare_failures == 1 and pending.prepare_next_at > store.now()
    assert pending.state is JobState.QUEUED and pending.result_received_at is None
    assert store.get_attempt(task.attempt_id).revision_cycles == 0
    assert store.get_flow_execution(flow.id).released_at is None
    assert store.assign_job("worker-a", "too-early", "epoch") is None
    future = pending.prepare_next_at + timedelta(seconds=1)
    monkeypatch.setattr(store, "_clock", lambda: future)
    monkeypatch.setattr(Repository, "fetch_ref", original)
    retry = store.assign_job("worker-a", "retry", "epoch")
    prepared = await gateway(store, subject)._prepare_assignment(retry, "worker-a", "retry", "epoch")
    assert isinstance(prepared, Assignment) and isinstance(prepared.task, RevisionTask) and prepared.secret is not None
    assert prepared.secret.push.repository == prepared.task.repo
    assert store.get_job(job.id).prepare_failures == 0
    assert store.get_job(job.id).prepare_first_failed_at is None


async def test_retry_after_and_count_survive_restart(store, pending_revision, monkeypatch):
    _, job, _, github, subject = pending_revision

    async def limited(repo, branch, *, retry_transient=False):
        raise GitHubError(GitHubErrorKind.RATE_LIMITED, "get_ref", retry_after="120")

    monkeypatch.setattr(Repository, "fetch_ref", limited)
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




@pytest.mark.parametrize("maximum,header", [(1, None), (5, "901")])
async def test_failure_or_time_budget_stops_without_early_retry(store, pending_revision, monkeypatch, maximum, header):
    _, job, _, _github, subject = pending_revision
    subject.settings.prepare_max_failures = maximum

    async def unavailable(repo, branch, *, retry_transient=False):
        raise GitHubError(GitHubErrorKind.RATE_LIMITED, "get_ref", retry_after=header)

    monkeypatch.setattr(Repository, "fetch_ref", unavailable)
    prepared = await gateway(store, subject)._prepare_assignment(job, "worker-a", "token", "epoch")
    assert prepared is JobState.FAILED
    failed = store.get_job(job.id)
    assert failed.result_applied_at is not None and failed.prepare_next_at is None
    assert "budget" in failed.last_error or "window" in failed.last_error
    assert "get_ref" in failed.prepare_last_error and "rate_limited" in failed.prepare_last_error
    assert failed.prepare_last_error in failed.last_error


@pytest.mark.parametrize("change", ["epoch", "token"])
def test_defer_requires_the_exact_unstarted_assignment(store, pending_revision, change):
    flow, job, _task, _, _ = pending_revision
    if change == "epoch":
        store.worker_connected("worker-a", PROTOCOL_VERSION, "new-epoch")
    else:
        assert store.requeue_assignment(job.id, "worker-a", "token", "epoch")
        assert store.assign_job("worker-a", "new-token", "epoch") is not None
    result = store.defer_preparation(job.id, "worker-a", "token", "epoch",
        GitHubError(GitHubErrorKind.TRANSIENT, "get_ref"), max_failures=5, window_seconds=900)
    assert result is None
    assert store.get_job(job.id).prepare_failures == 0
    assert store.get_flow_execution(flow.id).released_at is None


async def test_close_wins_over_a_read_failure(store, pending_revision, monkeypatch):
    flow, job, _, _github, subject = pending_revision

    async def close(repo, branch, *, retry_transient=False):
        with store.transaction() as tx:
            tx.lock_workflow(flow.id)
            pause_delivery(tx, tx.get_delivery(flow.id))
        raise GitHubError(GitHubErrorKind.TRANSIENT, "get_ref")

    monkeypatch.setattr(Repository, "fetch_ref", close)
    prepared = await gateway(store, subject)._prepare_assignment(job, "worker-a", "token", "epoch")
    assert prepared is JobState.DONE
    assert store.get_job(job.id).prepare_next_at is None
    assert store.get_flow_execution(flow.id).released_at is None


async def test_capability_signing_failure_never_enters_the_read_retry_budget(store, pending_revision, monkeypatch):
    _, job, _, _github, subject = pending_revision

    async def fail(repo):
        raise GitHubError(GitHubErrorKind.TRANSIENT, "create_push_capability")

    monkeypatch.setattr(Repository, "create_push_capability", fail)
    prepared = await gateway(store, subject)._prepare_assignment(job, "worker-a", "token", "epoch")
    assert prepared is JobState.FAILED
    assert store.get_job(job.id).prepare_failures == 0
    assert store.get_job(job.id).state is JobState.FAILED


async def test_expired_window_stops_before_another_github_request(store, pending_revision, monkeypatch):
    _, job, _, _github, subject = pending_revision
    calls = []

    async def unavailable(repo, branch, *, retry_transient=False):
        calls.append(branch)
        raise GitHubError(GitHubErrorKind.TRANSIENT, "get_ref")

    monkeypatch.setattr(Repository, "fetch_ref", unavailable)
    await gateway(store, subject)._prepare_assignment(job, "worker-a", "token", "epoch")
    first = store.get_job(job.id).prepare_first_failed_at
    monkeypatch.setattr(store, "_clock", lambda: first + timedelta(seconds=901))
    retry = store.assign_job("worker-a", "expired", "epoch")
    prepared = await gateway(store, subject)._prepare_assignment(retry, "worker-a", "expired", "epoch")
    assert prepared is JobState.FAILED and len(calls) == 1
    assert store.get_job(job.id).prepare_failures == 1
    assert "get_ref" in store.get_job(job.id).prepare_last_error




def test_another_worker_can_progress_while_original_binding_waits(store, pending_revision):
    flow, job, _, _, _ = pending_revision
    assert store.defer_preparation(job.id, "worker-a", "token", "epoch",
        GitHubError(GitHubErrorKind.TRANSIENT, "get_ref"), max_failures=5, window_seconds=900) is JobState.QUEUED
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
            GitHubError(GitHubErrorKind.TRANSIENT, "get_ref"), max_failures=5, window_seconds=900)
    assert store.get_job(job.id).state is JobState.RUNNING
    assert store.get_job(job.id).prepare_failures == 0


async def test_a_slow_retry_read_cannot_run_beyond_the_window(store, pending_revision, monkeypatch):
    _, job, _, _github, subject = pending_revision
    store.defer_preparation(job.id, "worker-a", "token", "epoch",
        GitHubError(GitHubErrorKind.TRANSIENT, "get_ref"), max_failures=5, window_seconds=900)
    first = store.get_job(job.id).prepare_first_failed_at
    monkeypatch.setattr(store, "_clock", lambda: first + timedelta(seconds=10))
    subject.settings.prepare_retry_window_seconds = 10.01
    retry = store.assign_job("worker-a", "slow", "epoch")

    async def slow(repo, branch, *, retry_transient=False):
        await asyncio.Event().wait()

    monkeypatch.setattr(Repository, "fetch_ref", slow)
    prepared = await gateway(store, subject)._prepare_assignment(retry, "worker-a", "slow", "epoch")
    assert prepared is JobState.FAILED
    assert "window" in store.get_job(job.id).last_error
