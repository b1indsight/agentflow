from datetime import timedelta

from app.control.service import ControlService
from app.models import JobKind
from app.protocol import PROTOCOL_VERSION
from app.stop_store import StopStore


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
    from app.protocol import ExecutionStopped
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
    from app.models import DeliveryState, FlowState
    from app.protocol import ExecutionStopped
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
    from app.protocol import ExecutionStopped
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
    from app.models import DeliveryState
    from app.protocol import ExecutionStopped
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
