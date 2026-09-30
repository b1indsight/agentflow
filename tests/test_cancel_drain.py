from datetime import timedelta

import pytest

from app.contracts.protocol import PROTOCOL_VERSION
from app.control.service import ControlService
from app.domain.models import JobKind
from app.persistence.stop_store import StopStore


def running(store):
    flow = store.create_flow('owner/repo', 1, 1)
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    store.worker_connected('w', PROTOCOL_VERSION, 'epoch')
    job = store.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.assign_job('w', 'token', 'epoch')
    assert store.start_assignment(job.id, attempt.id, 'w', 'token', 'epoch')
    return flow, job


def test_cancel_retains_slot_and_fence_past_recovery_timeout(store):
    flow, job = running(store)
    ControlService(store).cancel(flow)
    assert store.get_flow_execution(flow.id).released_at is None
    stop = StopStore(store).pending('w')
    assert stop.job_id == job.id and stop.assignment_token == 'token'
    store._clock = lambda: job.created_at + timedelta(days=1)
    store.recover_abandoned_assignments()
    store._clock = lambda: job.created_at + timedelta(days=2)
    store.sweep_expired_assignments()
    assert store.get_flow_execution(flow.id).released_at is None
    assert store.get_job(job.id).assignment_token == 'token'


def test_confirmation_requires_current_epoch_and_exact_request(store):
    from app.contracts.protocol import ExecutionStopped
    flow, job = running(store)
    ControlService(store).cancel(flow)
    stops = StopStore(store)
    stop = stops.pending('w')
    receipt = ExecutionStopped(job_id=job.id, flow_id=flow.id, request_id=stop.request_id,
                               assignment_token='token', connection_epoch='old', execution_nonce='owned')
    assert not stops.confirm('w', receipt)
    receipt = receipt.model_copy(update={'connection_epoch': 'epoch'})
    assert stops.confirm('w', receipt)
    assert stops.confirm('w', receipt)
    assert store.get_flow_execution(flow.id).released_at is not None


def test_drain_survives_reconnect_and_allows_bound_followup(store):
    flow, job = running(store)
    stops = StopStore(store)
    stops.drain('w', True)
    store.worker_connected('w', PROTOCOL_VERSION, 'next')
    assert store.list_workers()[0].draining
    # The existing flow still owns the worker; only new Flow allocation is disabled.
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET state='done' WHERE id=%s", (job.id,))
        connection.execute("UPDATE workers SET current_job_id=NULL,status='online' WHERE worker_id='w'")
    next_job = store.enqueue_job(flow.id, JobKind.REVISION)
    assert store.assign_job('w', 'revision', 'next').id == next_job.id
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET state='done' WHERE id=%s", (next_job.id,))
        connection.execute("UPDATE workers SET current_job_id=NULL,status='online' WHERE worker_id='w'")
    store.release_flow_execution(flow.id)
    another = store.create_flow('owner/another', 2, 1)
    store.enqueue_job(another.id, JobKind.INITIAL)
    assert store.assign_job('w', 'new', 'next') is None
    stops.drain('w', False)
    assert store.assign_job('w', 'new', 'next') is not None


def test_never_dispatched_cancel_needs_no_stop(store):
    flow = store.create_flow('owner/repo', 1, 1)
    store.enqueue_job(flow.id, JobKind.INITIAL)
    ControlService(store).cancel(flow)
    assert StopStore(store).pending('w') is None


async def test_merge_during_cancel_retains_binding_for_original_archive(store):
    from app.contracts.protocol import ExecutionStopped
    from app.domain.models import DeliveryState, FlowState
    from tests.test_merge import route_merge
    flow, _job = running(store)
    delivery = store.get_or_create_delivery(flow.id, 'flow/1')
    delivery.state, delivery.pr_number, delivery.head_sha = DeliveryState.OPEN, 17, 'b' * 40
    store.update_delivery(delivery)
    ControlService(store).cancel(flow)
    await route_merge(store, flow)
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    stop = store.stops.pending('w')
    assert store.stops.confirm('w', ExecutionStopped(**stop.message('epoch').model_dump(exclude={'type'}), execution_nonce='owned'))
    assert store.get_flow_execution(flow.id).released_at is None
    archive = store.assign_job('w', 'archive', 'epoch')
    assert archive is not None and archive.kind is JobKind.ARCHIVE


def test_unresolved_dispatched_effect_blocks_stop_even_if_result_was_marked_applied(store):
    from app.contracts.protocol import ExecutionStopped
    flow, job = running(store)
    # Older code could mark a cancelled effect skipped without reconciling a dispatched write.
    with store.connection() as conn:
        conn.execute("INSERT INTO result_applications(job_id,digest,plan,created_at) VALUES(%s,'digest','{}',now())", (job.id,))
        conn.execute("INSERT INTO result_effects(job_id,effect_key,position,payload,state,dispatched_at,next_attempt_at)"
                     " VALUES(%s,'comment',0,'{}','skipped',now(),now())", (job.id,))
    ControlService(store).cancel(flow)
    stop = store.stops.pending('w')
    receipt = ExecutionStopped(**stop.message('epoch').model_dump(exclude={'type'}), execution_nonce='owned')
    assert not store.stops.confirm('w', receipt)
    assert store.get_flow_execution(flow.id).released_at is None
    with store.connection() as conn:
        conn.execute("UPDATE result_effects SET receipt='{}' WHERE job_id=%s", (job.id,))
    assert store.stops.confirm('w', receipt)


async def test_late_merge_with_replacement_flow_keeps_stop_blocked(store):
    from app.contracts.protocol import ExecutionStopped
    from app.domain.models import DeliveryState
    from tests.test_merge import route_merge
    flow, _job = running(store)
    delivery = store.get_or_create_delivery(flow.id, 'flow/1')
    delivery.state, delivery.pr_number = DeliveryState.OPEN, 17
    store.update_delivery(delivery)
    ControlService(store).cancel(flow)
    store.create_flow(flow.repo, flow.issue_number, flow.installation_id)
    await route_merge(store, flow)
    stop = store.stops.pending('w')
    assert not store.stops.confirm('w', ExecutionStopped(**stop.message('epoch').model_dump(exclude={'type'}), execution_nonce='owned'))
    store._clock = lambda: flow.created_at + timedelta(days=1)
    store.recover_abandoned_assignments()
    store._clock = lambda: flow.created_at + timedelta(days=2)
    store.sweep_expired_assignments()
    assert store.get_flow_execution(flow.id).released_at is None


def test_lost_started_ack_cannot_requeue_then_cancel_as_unsent(store):
    flow = store.create_flow('owner/repo', 1, 1)
    store.worker_connected('w', PROTOCOL_VERSION, 'epoch')
    job = store.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.assign_job('w', 'token', 'epoch')
    assert store.stops.authorize_dispatch(job.id, 'w', 'token', 'epoch')
    assert not store.requeue_assignment(job.id, 'w', 'token', 'epoch')
    store.worker_disconnected('w', 'lost ACK', 'epoch')
    store._clock = lambda: job.created_at + timedelta(days=1)
    assert store.sweep_expired_assignments() == (0, 0)
    assert store.get_flow_execution(flow.id).released_at is None
    ControlService(store).cancel(flow)
    assert store.stops.pending('w') is not None
    assert store.get_flow_execution(flow.id).released_at is None


def test_cancel_during_preparation_releases_slot_without_nonexistent_journal(store):
    from app.domain.models import JobState
    flow = store.create_flow('owner/repo', 1, 1)
    store.worker_connected('w', PROTOCOL_VERSION, 'epoch')
    job = store.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.assign_job('w', 'preparing', 'epoch')
    ControlService(store).cancel(flow)
    assert store.stops.pending('w') is None
    assert store.get_flow_execution(flow.id).released_at is not None
    assert store.get_job(job.id).state is JobState.FAILED
    assert store.list_workers()[0].current_job_id is None
    assert not store.stops.authorize_dispatch(job.id, 'w', 'preparing', 'epoch')
    replacement = store.create_flow('owner/repo', 2, 1)
    next_job = store.enqueue_job(replacement.id, JobKind.INITIAL)
    assert store.assign_job('w', 'replacement', 'epoch').id == next_job.id
    assert not store.clear_preparation(job.id, 'w', 'preparing', 'epoch')
    assert store.list_workers()[0].current_job_id == next_job.id


def test_confirmed_stop_can_be_reacknowledged_after_flow_rebind(store):
    from app.contracts.protocol import ExecutionStopped
    flow, _ = running(store)
    ControlService(store).cancel(flow)
    stop = store.stops.pending('w')
    receipt = ExecutionStopped(**stop.message('epoch').model_dump(exclude={'type'}), execution_nonce='old-owned')
    assert store.stops.confirm('w', receipt)
    ControlService(store).retry(store.get_flow(flow.id))
    store.worker_connected('new-worker', PROTOCOL_VERSION, 'new-epoch')
    replacement = store.assign_job('new-worker', 'new-token', 'new-epoch')
    assert replacement is not None
    before = store.get_flow_execution(flow.id)
    store.worker_connected('w', PROTOCOL_VERSION, 'reconnect')
    replay = receipt.model_copy(update={'connection_epoch': 'reconnect'})
    assert store.stops.confirm('w', replay)
    assert store.get_flow_execution(flow.id) == before
    assert store.get_job(replacement.id).assignment_token == 'new-token'
    assert not store.stops.confirm('w', replay.model_copy(update={'execution_nonce': 'forged'}))


@pytest.mark.parametrize("remote_state", ["open", "closed", "merged", "foreign"])
async def test_cancelled_creation_persists_pr_identity_and_merge_before_stop(store, remote_state):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.contracts.protocol import ExecutionStopped
    from app.contracts.result_models import (
        EffectState,
        PullRequestEffect,
        ResultPending,
        ResultPlan,
    )
    from app.control.effects import ResultEffects
    from app.domain.github_models import PullRequestStatus
    from app.domain.models import DeliveryState, FlowState, JobState
    flow, job = running(store)
    delivery = store.get_or_create_delivery(flow.id, 'flow/1')
    delivery.state, delivery.base_branch, delivery.head_sha = DeliveryState.OPENING, 'main', 'a' * 40
    store.update_delivery(delivery)
    effect = PullRequestEffect(key='pr', installation_id=1, repo=flow.repo, branch=delivery.branch,
                               base_branch='main', head_sha='a' * 40, title='fixture', body='fixture')
    plan = ResultPlan(job_id=job.id, flow_id=flow.id, attempt_id=flow.current_attempt_id,
                      digest='fixture', state=JobState.DONE, delivery=delivery, effects=[effect])
    store.results.prepare(plan)
    record = store.results.claim(job.id, 'sender')
    store.results.mark_dispatched(record)
    store.results.finish(record, EffectState.UNCERTAIN, error='lost response', delay=0)
    ControlService(store).cancel(flow)
    remote = PullRequestStatus(title="PR", body="", number=17, repo=flow.repo, state='closed' if remote_state == 'merged' else 'open' if remote_state == 'foreign' else remote_state, head_repository='fork/repo' if remote_state == 'foreign' else flow.repo,
                               head_branch=delivery.branch, base_branch='main', head_sha='a' * 40,
                               base_sha='b' * 40, merged=remote_state == 'merged', merged_at=store.now() if remote_state == 'merged' else None, merge_commit_sha='c' * 40 if remote_state == 'merged' else None)
    github = SimpleNamespace(find_pull_request_by_head=AsyncMock(return_value=remote),
                             get_pull_request=AsyncMock(return_value=remote))
    if remote_state == 'foreign':
        with pytest.raises(ResultPending):
            await ResultEffects(store, github).run(plan)
        assert store.get_delivery(flow.id).pr_number is None
        assert store.get_flow_execution(flow.id).released_at is None
        return
    assert await ResultEffects(store, github).run(plan) is None
    recovered = store.get_delivery(flow.id)
    assert recovered.pr_number == 17 and recovered.state is DeliveryState(remote_state)
    assert recovered.merge_commit_sha == remote.merge_commit_sha
    assert store.get_flow_by_pr(flow.repo, 17).id == flow.id
    assert store.get_flow(flow.id).state is (FlowState.ACTIVE if remote_state == "merged" else FlowState.CANCELLED)
    stop = store.stops.pending('w')
    assert store.stops.confirm('w', ExecutionStopped(**stop.message('epoch').model_dump(exclude={'type'}), execution_nonce='owned'))
    if remote_state == 'merged':
        assert store.get_flow_execution(flow.id).released_at is None
        assert store.assign_job('w', 'archive', 'epoch').kind is JobKind.ARCHIVE
    else:
        assert store.get_flow_execution(flow.id).released_at is not None


async def test_preparation_result_releases_cancelled_binding_after_application(store):
    from app.contracts.protocol import Failed, terminal_digest
    from tests.test_coordinator import RecordingGitHub, coordinator
    flow = store.create_flow('owner/repo', 1, 1)
    store.worker_connected('w', PROTOCOL_VERSION, 'epoch')
    job = store.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.assign_job('w', 'preparing', 'epoch')
    message = Failed(job_id=job.id, assignment_token='preparing', connection_epoch='control', error='prepare failed')
    store.record_control_result(job.id, message.model_dump(mode='json'), terminal_digest(message), 'prepare failed')
    ControlService(store).cancel(flow)
    assert store.stops.pending('w') is None
    assert store.get_flow_execution(flow.id).released_at is None
    await coordinator(store, RecordingGitHub()).results.apply(job.id)
    assert store.get_job(job.id).result_applied_at is not None
    assert store.get_flow_execution(flow.id).released_at is not None
