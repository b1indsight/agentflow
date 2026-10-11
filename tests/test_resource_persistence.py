"""Frozen control evidence restores the same resource models without runtime clients."""

import json

import pytest
from pydantic import ValidationError

from app.contracts.result_models import EffectReceipt, ResolveEffect
from app.control.resolution import covered_thread
from app.domain.context_models import ReviewContext
from app.github import Issue, PullRequest, ReviewThread, ReviewThreads
from tests.review_memory import review_context
from tests.test_review_resolution import snapshot


def test_legacy_short_pr_receipt_restores_same_partial_resource():
    # Creation responses and old receipts lack full GET facts. Restoring must not
    # turn absent fields into observed defaults or require a live App.
    facts = {"number": 17, "state": "open", "head_branch": "flow/8",
             "head_sha": "abc", "base_branch": "main"}
    receipt = EffectReceipt.model_validate({"pull_request": facts})
    assert type(receipt.pull_request) is PullRequest
    assert json.loads(receipt.model_dump_json(exclude_unset=True)) == {"pull_request": facts}
    assert receipt.pull_request is not None
    with pytest.raises(AttributeError):
        _ = receipt.pull_request.repo
    with pytest.raises(AttributeError):
        _ = receipt.pull_request.repository
    with pytest.raises(ValidationError, match="complete pull-request facts"):
        ReviewContext(batch_id=1, repo="owner/repo", issue_number=8, pr_number=17,
                      pull_request=receipt.pull_request)


def test_full_context_roundtrip_retains_resource_types_and_known_nulls():
    legacy = review_context().model_dump(mode="json")
    legacy["issue"] = {"number": 8, "title": "Issue", "body": "Details", "state": "open"}
    context = ReviewContext.model_validate(legacy)
    restored = ReviewContext.model_validate_json(context.model_dump_json())
    assert type(restored.issue) is Issue
    assert type(restored.pull_request) is PullRequest
    assert restored.model_dump(mode="json") == legacy
    assert restored.digest == context.digest
    assert restored.pull_request is not None
    assert restored.pull_request.merged_at is None
    with pytest.raises(AttributeError):
        _ = restored.pull_request.repository
    with pytest.raises(ValidationError, match="complete issue facts"):
        ReviewContext(batch_id=1, repo="owner/repo", issue_number=8, pr_number=17,
                      issue=Issue(number=8))


def test_frozen_thread_and_resolution_effect_share_the_resource_model():
    observed = snapshot()
    evidence = ReviewThreads(repo="owner/repo", pr_number=17,
                             fetched_at=observed.comments[0].updated_at, threads=(observed,))
    restored = ReviewThreads.model_validate_json(evidence.model_dump_json())
    assert type(restored.threads[0]) is ReviewThread
    assert restored.model_dump(mode="json") == evidence.model_dump(mode="json")
    effect = ResolveEffect(installation_id=42, repo="owner/repo", key="resolve:T1",
                           number=17, thread_id="T1", snapshot=restored.threads[0])
    loaded = ResolveEffect.model_validate_json(effect.model_dump_json(exclude_unset=True))
    assert type(loaded.snapshot) is ReviewThread
    assert loaded.snapshot.comments == observed.comments
    legacy = effect.model_dump(mode="json")
    del legacy["snapshot"]["complete"]
    recovered = ResolveEffect.model_validate(legacy)
    assert not covered_thread(recovered.snapshot, [])
    assert "complete" not in recovered.snapshot.model_fields_set
    unknown = ReviewThread(repo="owner/repo", pr_number=17, thread_id="T1")
    assert not covered_thread(unknown, [])
    with pytest.raises(ValidationError, match="observed thread facts"):
        ResolveEffect(installation_id=42, repo="owner/repo", key="resolve:T1",
                      number=17, thread_id="T1", snapshot=unknown)
