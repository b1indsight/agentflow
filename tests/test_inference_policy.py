"""Inference dispatch, cancellation and settlement rules, decided without a database."""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.inference_models import (
    LOCAL_BUB_WORKER_ID,
    InferenceCapability,
    InferenceFailure,
    InferenceJob,
    InferenceMessage,
    InferenceTask,
)
from app.domain.inference_policy import (
    choose_inference_worker,
    decide_inference_cancellation,
    decide_inference_settlement,
    plan_inference_attempt,
)
from app.domain.models import (
    ArchiveReason,
    Flow,
    FlowState,
    Job,
    JobKind,
    JobState,
    TriageVerdict,
    Verdict,
    WorkerRecord,
    WorkerStatus,
)
from app.domain.review_models import BatchState, ReviewBatch
from tests.review_memory import judge_decision, review_context

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)
DEADLINE = NOW + timedelta(seconds=60)
TASK = InferenceTask(request_id="a" * 32, messages=[InferenceMessage(role="user", content="hi")])


def call(**changes) -> InferenceJob:
    return InferenceJob(task=TASK, deadline_at=DEADLINE, **changes)


def job(*, state=JobState.RUNNING, attempt=1, worker="w1", flow_id=None, **changes) -> Job:
    inference = changes.pop("inference", None) or call()
    return Job(
        id=7, flow_id=flow_id, attempt_id=inference.source_attempt_id, kind=JobKind.INFERENCE, state=state, attempt=attempt,
        assigned_worker_id=worker if state is JobState.RUNNING else None, inference=inference,
        created_at=NOW - timedelta(seconds=2), **changes,
    )


def worker(worker_id: str, **capability) -> WorkerRecord:
    return WorkerRecord(
        worker_id=worker_id, workspace="default", protocol_version=1, status=WorkerStatus.ONLINE,
        inference=InferenceCapability(adapter_version="test/1", **capability),
        first_connected_at=NOW, last_connected_at=NOW, last_seen_at=NOW,
    )


def settle(current: Job, failure: InferenceFailure | None, *, cancellation=None, now=NOW):
    return decide_inference_settlement(current, failure, cancellation=cancellation, now=now)


def test_success_finishes_without_touching_the_call():
    current = job()
    settled = settle(current, None)
    assert (settled.state, settled.failure, settled.call) == (JobState.DONE, None, current.inference)


@pytest.mark.parametrize(("attempt", "state", "reason"), [
    (1, JobState.QUEUED, None),
    (2, JobState.QUEUED, None),
    (3, JobState.FAILED, "max_attempts"),
])
def test_recoverable_failure_retries_until_the_third_attempt(attempt, state, reason):
    settled = settle(job(attempt=attempt), InferenceFailure(code="rate_limited"))

    assert settled.state is state
    assert settled.call.failures == [InferenceFailure(code="rate_limited")]
    assert settled.call.avoid_workers == {"w1"} and settled.call.excluded_workers == frozenset()
    if state is JobState.QUEUED:
        # Backoff grows with the attempt that just failed.
        assert settled.call.next_attempt_at == NOW + timedelta(seconds=0.25 * attempt)
        assert settled.call.summary is None
    else:
        summary = settled.call.summary
        assert settled.call.next_attempt_at is None
        assert (summary.stop_reason, summary.attempts, summary.last_failure_code) == (reason, 3, "rate_limited")
        assert (summary.logical_call_id, summary.last_worker_id) == (TASK.request_id, "w1")
        assert (summary.finished_at, summary.elapsed_ms) == (NOW, 2000)


def test_non_retryable_failure_ends_the_call_on_its_first_attempt():
    settled = settle(job(), InferenceFailure(code="authentication"))

    assert settled.state is JobState.FAILED
    assert settled.call.summary.stop_reason == "non_retryable"
    assert settled.call.avoid_workers == {"w1"}


@pytest.mark.parametrize(("code", "excluded"), [
    ("busy", True), ("connection_closed", True), ("rpc_timeout", True),
    # Confirmed cleanup: the worker may be tried again when nobody else is ready.
    ("execution_timeout", False), ("backend_unavailable", False),
])
def test_only_unknown_outcomes_exclude_the_worker(code, excluded):
    settled = settle(job(), InferenceFailure(code=code))

    assert settled.state is JobState.QUEUED
    assert settled.call.avoid_workers == {"w1"}
    assert settled.call.excluded_workers == ({"w1"} if excluded else frozenset())
    # Only provider pressure waits before the next attempt.
    wait = 0.25 if code == "backend_unavailable" else 0
    assert settled.call.next_attempt_at == NOW + timedelta(seconds=wait)


def test_retry_after_beyond_the_deadline_ends_the_call_instead_of_waiting():
    # Larger than datetime can represent: must be compared before building a timestamp.
    settled = settle(job(), InferenceFailure(code="rate_limited", retry_after_seconds=1e300))

    assert settled.state is JobState.FAILED
    assert settled.call.summary.stop_reason == "deadline"


def test_retry_after_within_the_budget_is_honored():
    settled = settle(job(), InferenceFailure(code="rate_limited", retry_after_seconds=30))

    assert settled.state is JobState.QUEUED
    assert settled.call.next_attempt_at == NOW + timedelta(seconds=30)


def test_cancellation_overrides_a_successful_result():
    cancelled = InferenceFailure(code="cancelled")
    settled = settle(job(), None, cancellation=cancelled)

    assert (settled.state, settled.failure) == (JobState.FAILED, cancelled)
    assert settled.call.cancel_failure == cancelled
    assert settled.call.summary.stop_reason == "non_retryable"


def test_expired_deadline_stops_retries_that_the_budget_would_allow():
    timeout = InferenceFailure(code="execution_timeout")
    settled = settle(job(attempt=1), InferenceFailure(code="rate_limited"), cancellation=timeout)

    assert settled.state is JobState.FAILED
    assert (settled.call.summary.stop_reason, settled.call.summary.last_failure_code) == (
        "deadline", "execution_timeout")


def test_queued_call_settles_without_recording_an_attempt_failure():
    # Nothing was dispatched: zero attempts must not look like a failed execution.
    queued = job(state=JobState.QUEUED, attempt=0)
    settled = settle(queued, None, cancellation=InferenceFailure(code="execution_timeout"))

    assert settled.state is JobState.FAILED
    assert settled.call.failures == [] and settled.call.avoid_workers == frozenset()
    assert (settled.call.summary.attempts, settled.call.summary.last_worker_id) == (0, None)


def flow(**changes) -> Flow:
    values = {"id": 3, "repo": "owner/repo", "issue_number": 8, "state": FlowState.REVIEW,
              "current_attempt_id": 5} | changes
    return Flow.model_validate(values)


def batch(**changes) -> ReviewBatch:
    values = {"id": 9, "flow_id": 3, "attempt_id": 5, "state": BatchState.READY,
              "trigger": "discussion", "job_id": None, "inference_job_id": 7, "feedback": [],
              "open_points": [], "created_at": NOW} | changes
    return ReviewBatch.model_validate(values)


def cancellation(current: Job, *, source=None, review=None, batch_current=True, now=NOW):
    return decide_inference_cancellation(
        current, flow=source, batch=review, batch_current=batch_current, now=now)


def test_generic_call_is_only_bounded_by_its_deadline():
    assert cancellation(job()) is None
    assert cancellation(job(), now=DEADLINE) == InferenceFailure(code="execution_timeout")


def test_recorded_cancellation_is_final():
    recorded = InferenceFailure(code="cancelled")
    current = job(flow_id=3, inference=call(cancel_failure=recorded, source_attempt_id=5))

    assert cancellation(current, source=flow()) == recorded


@pytest.mark.parametrize("source", [
    None,
    flow(current_attempt_id=6),
    flow(state=FlowState.BLOCKED),
    flow(archive_reason=ArchiveReason.ABANDONED),
])
def test_call_is_cancelled_when_its_source_flow_moved_on(source):
    current = job(flow_id=3, inference=call(source_attempt_id=5))

    assert cancellation(current, source=flow()) is None
    assert cancellation(current, source=source) == InferenceFailure(code="cancelled")


def test_call_is_cancelled_when_its_review_batch_no_longer_waits_for_it():
    context = review_context(9)
    current = job(flow_id=3, inference=call(
        source_attempt_id=5, batch_id=9, context_digest=context.digest))
    cancelled = InferenceFailure(code="cancelled")

    assert cancellation(current, source=flow(), review=batch(context=context)) is None
    for review, current_batch in [
        (batch(context=context, inference_job_id=8), True),
        (batch(context=context), False),
        (batch(context=context, decision=judge_decision(
            TriageVerdict(verdict=Verdict.WAIT), context)), True),
        (batch(context=None), True),
        (batch(context=context.model_copy(update={"ci": "passed"})), True),
    ]:
        assert cancellation(current, source=flow(), review=review, batch_current=current_batch) == cancelled


def test_answer_received_on_time_survives_application_after_the_deadline():
    # Restart recovery applies a durable result later than it arrived.
    on_time = job(result_received_at=DEADLINE - timedelta(seconds=1))
    late = job(result_received_at=DEADLINE)

    assert cancellation(on_time, now=DEADLINE + timedelta(hours=1)) is None
    assert cancellation(late, now=DEADLINE + timedelta(hours=1)) == InferenceFailure(code="execution_timeout")


def test_attempt_waits_for_backoff_then_gets_only_the_remaining_budget():
    backing_off = call(next_attempt_at=NOW + timedelta(seconds=1))

    assert plan_inference_attempt(backing_off, request_id="b" * 32, now=NOW) is None
    task = plan_inference_attempt(backing_off, request_id="b" * 32, now=NOW + timedelta(seconds=1))
    assert (task.request_id, task.timeout_seconds, task.messages) == ("b" * 32, 59, TASK.messages)
    assert plan_inference_attempt(call(), request_id="b" * 32, now=DEADLINE) == InferenceFailure(
        code="execution_timeout")


def test_worker_choice_excludes_then_avoids_then_prefers_local():
    remote, other, local = worker("w1"), worker("w2"), worker(LOCAL_BUB_WORKER_ID)

    def chosen(current, available):
        return getattr(choose_inference_worker(current, TASK, available), "worker_id", None)

    # Fair base order decides between equals; the local worker jumps that order.
    assert chosen(call(), [remote, other]) == "w1"
    assert chosen(call(), [remote, other, local]) == LOCAL_BUB_WORKER_ID
    # A worker that failed this call yields to any other ready worker, local or not.
    assert chosen(call(avoid_workers={LOCAL_BUB_WORKER_ID}), [local, other]) == "w2"
    assert chosen(call(avoid_workers={"w1"}), [remote]) == "w1"
    # Unknown outcomes and unsupported requests never qualify.
    assert chosen(call(excluded_workers={"w1"}), [remote]) is None
    assert chosen(call(), [worker("w1", formats={"json"}), other]) == "w2"
