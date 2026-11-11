from datetime import timedelta

import pytest
from psycopg.errors import UniqueViolation

from app.contracts.protocol import ExecutionStopped, Failed
from app.contracts.result_models import EffectState, PullRequestEffect, ResultPlan
from app.controler.workflow.effects import ResultEffects
from app.controler.workflow.service import ControlService
from app.domain.models import DeliveryState, FlowState, JobKind, JobState
from app.github import PullRequest
from tests.github import RecordingGitHub
from tests.test_coordinator import coordinator
from tests.test_merge import route_merge
from tests.workspace_fixtures import register_worker


def running(store):
    flow = store.flows.create_flow("owner/repo", 1, 1)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    register_worker(store, "w", repo=flow.repo, epoch="epoch")
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("w", "token", "epoch")
    assert store.workers.start_assignment(job.id, attempt.id, "w", "token", "epoch")
    return flow, job


def test_cancel_retains_slot_and_fence_past_recovery_timeout(store):
    flow, job = running(store)
    ControlService(
        database=store.database,
    ).cancel(flow)
    assert store.workers.get_flow_execution(flow.id).released_at is None
    stop = store.stops.pending("w")
    assert stop.job_id == job.id and stop.assignment_token == "token"
    store._db._clock = lambda: job.created_at + timedelta(days=1)
    store.workers.recover_abandoned_assignments()
    store._db._clock = lambda: job.created_at + timedelta(days=2)
    store.workers.sweep_expired_assignments()
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert store.jobs.get_job(job.id).assignment_token == "token"


def test_confirmation_requires_current_epoch_and_exact_request(store):
    flow, job = running(store)
    ControlService(
        database=store.database,
    ).cancel(flow)
    stops = store.stops
    stop = stops.pending("w")
    receipt = ExecutionStopped(
        job_id=job.id,
        flow_id=flow.id,
        request_id=stop.request_id,
        assignment_token="token",
        execution_nonce="owned",
    )
    assert not stops.confirm("w", receipt, "old")
    assert stops.confirm("w", receipt, "epoch")
    assert stops.confirm("w", receipt, "epoch")
    assert store.workers.get_flow_execution(flow.id).released_at is not None


def test_cancel_during_followup_preparation_stops_last_dispatched_job(store):
    # A review-waiting Flow still owns its checkout; an unsent followup has no worker journal.
    flow, job = running(store)
    assert store.workers.finish_assignment(job.id, "w", "token", JobState.DONE, "epoch")
    with store.connection() as conn:
        conn.execute("UPDATE jobs SET result_received_at=now() WHERE id=%s", (job.id,))
    followup = store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    assert store.workers.assign_job("w", "preparing", "epoch").id == followup.id
    ControlService(database=store.database).cancel(flow)
    stop = store.stops.pending("w")
    assert stop is not None and stop.job_id == job.id
    assert store.workers.get_flow_execution(flow.id).released_at is None


def test_drain_survives_reconnect_and_allows_bound_followup(store):
    flow, job = running(store)
    store.workers.drain("w", True)
    register_worker(store, "w", repo=flow.repo, epoch="next")
    assert store.workers.list_workers()[0].draining
    # The existing flow still owns the worker; only new Flow allocation is disabled.
    assert store.workers.finish_assignment(job.id, "w", "token", JobState.DONE, "next")
    next_job = store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    assert store.workers.assign_job("w", "revision", "next").id == next_job.id
    assert store.workers.finish_assignment(next_job.id, "w", "revision", JobState.DONE, "next")
    ControlService(database=store.database).cancel(flow)
    stop = store.stops.pending("w")
    assert store.stops.confirm(
        "w", ExecutionStopped(**stop.fence().model_dump(), execution_nonce="owned"), "next"
    )
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    another = store.flows.create_flow("owner/another", 2, 1)
    register_worker(store, "w", repo=another.repo, epoch="next")
    store.jobs.enqueue_job(another.id, JobKind.INITIAL)
    assert store.workers.assign_job("w", "new", "next") is None
    store.workers.drain("w", False)
    assert store.workers.assign_job("w", "new", "next") is not None


def test_never_dispatched_cancel_needs_no_stop(store):
    flow = store.flows.create_flow("owner/repo", 1, 1)
    store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    ControlService(
        database=store.database,
    ).cancel(flow)
    assert store.stops.pending("w") is None


@pytest.mark.parametrize("reconnect", [False, True])
async def test_merge_during_cancel_retains_binding_for_original_archive(store, reconnect):
    flow, job = running(store)
    delivery = store.deliveries.get_or_create_delivery(flow.id, "flow/1")
    delivery.state, delivery.pr_number, delivery.head_sha = DeliveryState.OPEN, 17, "b" * 40
    store.deliveries.update_delivery(delivery)
    ControlService(
        database=store.database,
    ).cancel(flow)
    await route_merge(store, flow)
    assert store.flows.get_flow(flow.id).state is FlowState.FINALIZING
    stop = store.stops.pending("w")
    assert store.stops.confirm(
        "w", ExecutionStopped(**stop.fence().model_dump(), execution_nonce="owned"), "epoch"
    )
    assert store.workers.get_flow_execution(flow.id).released_at is None
    epoch = "reconnected" if reconnect else "epoch"
    if reconnect:
        register_worker(store, "w", repo=flow.repo, epoch=epoch)
    archive = store.workers.assign_job("w", "archive", epoch)
    assert archive is not None and archive.kind is JobKind.ARCHIVE
    job = store.jobs.get_job(job.id)
    assert archive.workspace == job.workspace and archive.generation == job.generation
    assert store.workers.get_flow_execution(flow.id).bound_at == stop.binding_at
    # A retained merged Flow can stop coding then archive in the same generation.
    # Stop identity must follow the assignment, not the Flow generation.
    assert store.stops.authorize_dispatch(archive.id, "w", "archive", epoch)
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.stops.request(flow.id)
    archive_stop = store.stops.pending("w")
    assert archive_stop.job_id == archive.id and archive_stop.generation == archive.generation


@pytest.mark.parametrize("kind", ["comment", "label", None])
def test_unresolved_dispatched_effect_blocks_stop_except_skipped_status_labels(store, kind):
    flow, job = running(store)
    # Only skipped display updates can settle without a remote receipt, not other writes.
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO result_applications(job_id,digest,plan,created_at) VALUES(%s,'digest','{}',now())",
            (job.id,),
        )
        conn.execute(
            "INSERT INTO result_effects(job_id,effect_key,position,payload,state,dispatched_at,next_attempt_at)"
            " VALUES(%s,'effect',0,jsonb_build_object('kind',%s::text),'skipped',now(),now())",
            (job.id, kind),
        )
    ControlService(
        database=store.database,
    ).cancel(flow)
    stop = store.stops.pending("w")
    receipt = ExecutionStopped(**stop.fence().model_dump(), execution_nonce="owned")
    if kind != "label":
        assert not store.stops.confirm("w", receipt, "epoch")
        assert store.workers.get_flow_execution(flow.id).released_at is None
        assert store.stops.pending("w").state == "stopped"
        assert all(w.current_job_id is None for w in store.workers.list_workspaces("w"))
        with store.connection() as conn:
            conn.execute("UPDATE result_effects SET receipt='{}' WHERE job_id=%s", (job.id,))
    assert store.stops.confirm("w", receipt, "epoch")
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.workers.get_flow_execution(flow.id).released_at is not None


def test_stopped_writer_does_not_block_its_pending_terminal_application(store):
    # Stop may arrive while a worker terminal is durable but still awaiting effects.
    # Its exact claim can clear without stranding the result application behind it.
    flow, job = running(store)
    terminal = Failed(job_id=job.id, assignment_token="token", error="worker stopped")
    assert store.workers.record_assignment_result(job.id, "w", "token", "epoch", terminal) == "pending"
    ControlService(database=store.database).cancel(flow)
    stop = store.stops.pending("w")
    receipt = ExecutionStopped(**stop.fence().model_dump(), execution_nonce="owned")
    assert not store.stops.confirm("w", receipt, "epoch")
    assert store.stops.pending("w").state == "stopped"
    assert all(w.current_job_id is None for w in store.workers.list_workspaces("w"))
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert store.jobs.complete_persisted_result(job.id, JobState.FAILED)
    assert store.stops.confirm("w", receipt, "epoch")
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED


async def test_finalizing_flow_reserves_issue_until_late_merge_cleanup_settles(store):
    flow, _job = running(store)
    delivery = store.deliveries.get_or_create_delivery(flow.id, "flow/1")
    delivery.state, delivery.pr_number = DeliveryState.OPEN, 17
    store.deliveries.update_delivery(delivery)
    ControlService(
        database=store.database,
    ).cancel(flow)
    with pytest.raises(UniqueViolation):
        store.flows.create_flow(flow.repo, flow.issue_number, flow.installation_id)
    await route_merge(store, flow)
    stop = store.stops.pending("w")
    assert store.stops.confirm(
        "w", ExecutionStopped(**stop.fence().model_dump(), execution_nonce="owned"), "epoch"
    )
    store._db._clock = lambda: flow.created_at + timedelta(days=1)
    store.workers.recover_abandoned_assignments()
    store._db._clock = lambda: flow.created_at + timedelta(days=2)
    store.workers.sweep_expired_assignments()
    assert store.workers.get_flow_execution(flow.id).released_at is None


def test_lost_started_ack_cannot_requeue_then_cancel_as_unsent(store):
    flow = store.flows.create_flow("owner/repo", 1, 1)
    register_worker(store, "w", repo=flow.repo, epoch="epoch")
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("w", "token", "epoch")
    assert store.stops.authorize_dispatch(job.id, "w", "token", "epoch")
    assert not store.workers.requeue_assignment(job.id, "w", "token", "epoch")
    store.workers.worker_disconnected("w", "lost ACK", "epoch")
    store._db._clock = lambda: job.created_at + timedelta(days=1)
    assert store.workers.sweep_expired_assignments() == (0, 0)
    assert store.workers.get_flow_execution(flow.id).released_at is None
    ControlService(
        database=store.database,
    ).cancel(flow)
    assert store.stops.pending("w") is not None
    assert store.workers.get_flow_execution(flow.id).released_at is None


def test_cancel_during_preparation_releases_slot_without_nonexistent_journal(store):
    flow = store.flows.create_flow("owner/repo", 1, 1)
    register_worker(store, "w", repo=flow.repo, epoch="epoch")
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("w", "preparing", "epoch")
    ControlService(
        database=store.database,
    ).cancel(flow)
    assert store.stops.pending("w") is None
    assert store.workers.get_flow_execution(flow.id).released_at is not None
    assert store.jobs.get_job(job.id).state is JobState.FAILED
    assert store.workers.list_workers()[0].current_job_id is None
    assert not store.stops.authorize_dispatch(job.id, "w", "preparing", "epoch")
    replacement = store.flows.create_flow("owner/repo", 2, 1)
    next_job = store.jobs.enqueue_job(replacement.id, JobKind.INITIAL)
    assert store.workers.assign_job("w", "replacement", "epoch").id == next_job.id
    assert not store.workers.clear_preparation(job.id, "w", "preparing", "epoch")
    assert any(w.current_job_id == next_job.id for w in store.workers.list_workspaces("w"))


def test_confirmed_stop_can_be_reacknowledged_after_flow_rebind(store):
    flow, _ = running(store)
    ControlService(
        database=store.database,
    ).cancel(flow)
    stop = store.stops.pending("w")
    receipt = ExecutionStopped(**stop.fence().model_dump(), execution_nonce="old-owned")
    assert store.stops.confirm("w", receipt, "epoch")
    ControlService(
        database=store.database,
    ).resume(store.flows.get_flow(flow.id), expected_round_no=flow.current_round_no)
    register_worker(store, "new-worker", repo=flow.repo, epoch="new-epoch")
    replacement = store.workers.assign_job("new-worker", "new-token", "new-epoch")
    assert replacement is not None
    before = store.workers.get_flow_execution(flow.id)
    register_worker(store, "w", epoch="reconnect")
    assert store.stops.confirm("w", receipt, "reconnect")
    assert store.workers.get_flow_execution(flow.id) == before
    assert store.jobs.get_job(replacement.id).assignment_token == "new-token"


@pytest.mark.parametrize("remote_state", ["open", "closed", "merged"])
async def test_cancelled_creation_persists_pr_identity_and_merge_before_stop(
    store,
    remote_state,
    recording_resources,
):
    flow, job = running(store)
    delivery = store.deliveries.get_or_create_delivery(flow.id, "flow/1")
    delivery.state, delivery.base_branch, delivery.head_sha = (
        DeliveryState.OPENING,
        "main",
        "a" * 40,
    )
    store.deliveries.update_delivery(delivery)
    effect = PullRequestEffect(
        key="pr",
        installation_id=1,
        repo=flow.repo,
        branch=delivery.branch,
        base_branch="main",
        head_sha="a" * 40,
        title="fixture",
        body="fixture",
    )
    plan = ResultPlan(
        job_id=job.id,
        flow_id=flow.id,
        attempt_id=flow.current_attempt_id,
        digest="fixture",
        state=JobState.DONE,
        delivery=delivery,
        effects=[effect],
    )
    store.results.prepare(plan)
    record = store.results.claim(job.id, "sender")
    store.results.mark_dispatched(record)
    store.results.finish(record, EffectState.UNCERTAIN, error="lost response", delay=0)
    ControlService(
        database=store.database,
    ).cancel(flow)
    remote = PullRequest(
        title="PR",
        body="",
        number=17,
        repo=flow.repo,
        state="closed" if remote_state == "merged" else remote_state,
        head_repository=flow.repo,
        head_branch=delivery.branch,
        base_branch="main",
        head_sha="a" * 40,
        base_sha="b" * 40,
        merged=remote_state == "merged",
        merged_at=store.now() if remote_state == "merged" else None,
        merge_commit_sha="c" * 40 if remote_state == "merged" else None,
    )
    github = RecordingGitHub()
    github.pull_request = remote
    assert (
        await ResultEffects(
            github,
            database=store.database,
        ).run(plan)
        is None
    )
    recovered = store.deliveries.get_delivery(flow.id)
    assert recovered.pr_number == 17 and recovered.state is DeliveryState(remote_state)
    assert recovered.merge_commit_sha == remote.merge_commit_sha
    assert store.flows.get_flow_by_pr(flow.repo, 17).id == flow.id
    assert store.flows.get_flow(flow.id).state is FlowState.FINALIZING
    stop = store.stops.pending("w")
    assert store.stops.confirm(
        "w", ExecutionStopped(**stop.fence().model_dump(), execution_nonce="owned"), "epoch"
    )
    if remote_state == "merged":
        assert store.workers.get_flow_execution(flow.id).released_at is None
        assert store.workers.assign_job("w", "archive", "epoch").kind is JobKind.ARCHIVE
    else:
        assert store.workers.get_flow_execution(flow.id).released_at is not None


async def test_preparation_result_releases_cancelled_binding_after_application(
    store, recording_resources
):
    flow = store.flows.create_flow("owner/repo", 1, 1)
    register_worker(store, "w", repo=flow.repo, epoch="epoch")
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.workers.assign_job("w", "preparing", "epoch")
    message = Failed(job_id=job.id, assignment_token="preparing", error="prepare failed")
    store.jobs.record_control_result(job.id, message, "prepare failed")
    ControlService(
        database=store.database,
    ).cancel(flow)
    assert store.stops.pending("w") is None
    assert store.workers.get_flow_execution(flow.id).released_at is None
    await coordinator(store, RecordingGitHub()).results.apply(job.id)
    assert store.jobs.get_job(job.id).result_applied_at is not None
    assert store.workers.get_flow_execution(flow.id).released_at is not None
