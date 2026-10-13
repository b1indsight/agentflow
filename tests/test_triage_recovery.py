"""Unfinished batches are retried without local assignment or worker ownership changes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models import JobKind, ReviewItemKind, TriageVerdict, Verdict
from app.domain.review_models import FeedbackUpdate
from app.persistence.reviews import ReviewStore
from tests.github import RecordingGitHub
from tests.review_memory import judge_decision
from tests.test_coordinator import coordinator
from tests.test_merge import route_merge
from tests.test_review_batches import claim, feedback, ingest, review_flow  # noqa: F401
from tests.test_workflow_transactions import bound_review_flow


async def test_revision_and_batch_transition_commit_together(
    store, review_flow, monkeypatch, recording_resources,  # noqa: F811
):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock))
    batch = claim(store, clock)[0]
    judge = SimpleNamespace(triage=AsyncMock(side_effect=lambda context: judge_decision(
        TriageVerdict(verdict=Verdict.REVISE, confidence=1, instruction="Fix it"), context)))
    subject = coordinator(store, RecordingGitHub())
    advance = ReviewStore.advance

    def interrupt(self, *args):
        advance(self, *args)
        raise RuntimeError("interrupted transaction")

    with monkeypatch.context() as patch:
        patch.setattr(ReviewStore, "advance", interrupt)
        with pytest.raises(RuntimeError, match="interrupted transaction"):
            await subject.triage(batch, judge)
    assert store.jobs.list_jobs(flow.id) == []
    assert store.reviews.get_batch(batch.id).job_id is None
    await subject.triage(store.reviews.get_batch(batch.id), judge)
    await subject.triage(store.reviews.get_batch(batch.id), judge)
    assert len(store.jobs.list_jobs(flow.id)) == 1


async def test_judge_failure_then_ignore_keeps_archive_owner(store, recording_resources):
    # A provider failure must not release the worker needed after a later PR merge.
    flow = bound_review_flow(store)
    store.reviews.ingest(flow.id, "review", "pull_request_review_comment", {},
        FeedbackUpdate(kind=ReviewItemKind.INLINE, github_id=101, author="alice", body="Check",
                       updated_at=store.now()), settle_seconds=0, submit_grace_seconds=0)
    batch = store.reviews.claim_due()[0]
    before = store.workers.get_flow_execution(flow.id)
    subject = coordinator(store, RecordingGitHub())
    judge = SimpleNamespace(triage=AsyncMock(side_effect=RuntimeError("provider stopped")))
    with pytest.raises(RuntimeError, match="provider stopped"):
        await subject.triage(batch, judge)
    judge.triage.side_effect = lambda context: judge_decision(TriageVerdict(verdict=Verdict.IGNORE), context)
    await subject.triage(store.reviews.get_batch(batch.id), judge)
    assert store.workers.get_flow_execution(flow.id) == before
    await route_merge(store, flow)
    archive = store.workers.assign_job("worker-a", "archive", "epoch-a")
    assert archive is not None and archive.kind is JobKind.ARCHIVE
