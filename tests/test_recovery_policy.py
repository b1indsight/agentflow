"""Lease policy distinguishes retryable work from an executor that may still be running."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import DeliveryState, FlowState, JobKind
from app.recovery_policy import (
    BindingRecoveryFacts,
    JobRecoveryFacts,
    RecoveryAction,
    decide_job_recovery,
    should_release_binding,
)

NOW = datetime(2026, 9, 29, tzinfo=UTC)


@pytest.mark.parametrize("changes,expected", [
    ({}, RecoveryAction.REQUEUE),
    ({"lease_expires_at": NOW + timedelta(microseconds=1)}, RecoveryAction.KEEP),
    ({"lease_expires_at": None}, RecoveryAction.KEEP),
    ({"started_at": NOW}, RecoveryAction.FAIL),
    ({"started_at": NOW, "recovery_deadline": None}, RecoveryAction.KEEP),
    ({"started_at": NOW, "recovery_deadline": NOW + timedelta(microseconds=1)}, RecoveryAction.KEEP),
    ({"dispatch_authorized_at": NOW}, RecoveryAction.KEEP),
    ({"kind": JobKind.ARCHIVE, "archive_frozen": True}, RecoveryAction.KEEP),
    ({"kind": JobKind.ARCHIVE}, RecoveryAction.REQUEUE),
    ({"started_at": NOW, "result_received_at": NOW}, RecoveryAction.KEEP),
    ({"result_received_at": NOW}, RecoveryAction.KEEP),
    ({"has_pending_stop": True}, RecoveryAction.KEEP),
    ({"started_at": NOW, "has_pending_stop": True}, RecoveryAction.KEEP),
    ({"started_at": NOW, "delivery_state": DeliveryState.CLOSED}, RecoveryAction.BLOCKED),
    ({"started_at": NOW, "delivery_state": DeliveryState.MERGED}, RecoveryAction.BLOCKED),
    ({"dispatch_authorized_at": NOW, "delivery_state": DeliveryState.CLOSED}, RecoveryAction.BLOCKED),
    ({"started_at": NOW, "delivery_state": DeliveryState.CLOSED, "has_pending_stop": True}, RecoveryAction.BLOCKED),
    ({"delivery_state": DeliveryState.CLOSED}, RecoveryAction.REQUEUE),
    ({"started_at": NOW, "delivery_state": DeliveryState.CLOSED, "flow_state": FlowState.CANCELLED}, RecoveryAction.FAIL),
])
def test_job_recovery_preserves_executor_and_result_evidence(changes, expected):
    # Lost Started/Result frames and PR close must not cause a second executor;
    # the exact deadline is due, while one microsecond before it is not.
    facts = JobRecoveryFacts(id=1, flow_id=1, kind=JobKind.INITIAL,
                             flow_state=FlowState.ACTIVE, delivery_state=None,
                             lease_expires_at=NOW, recovery_deadline=NOW)
    assert decide_job_recovery(facts.model_copy(update=changes), NOW) is expected


@pytest.mark.parametrize("changes,expected", [
    ({}, True),
    ({"recovery_deadline": None}, False),
    ({"recovery_deadline": NOW + timedelta(microseconds=1)}, False),
    ({"delivery_state": DeliveryState.CLOSED}, False),
    ({"delivery_state": DeliveryState.MERGED}, False),
    ({"delivery_state": DeliveryState.MERGED, "flow_state": FlowState.FAILED}, True),
    ({"has_pending_stop": True}, False),
    ({"has_unstarted_dispatch": True}, False),
    ({"has_unapplied_result": True}, False),
])
def test_binding_release_waits_for_durable_fences(changes, expected):
    # An idle worker can still own an unapplied result or an unconfirmed stop;
    # recovery timeout alone must not release that repository to another worker.
    facts = BindingRecoveryFacts(flow_id=1, worker_id="w", flow_state=FlowState.ACTIVE,
                                 delivery_state=None, recovery_deadline=NOW)
    assert should_release_binding(facts.model_copy(update=changes), NOW) is expected
