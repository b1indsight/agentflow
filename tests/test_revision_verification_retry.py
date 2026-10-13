"""Result verification keeps server deadlines durable without consuming preparation budget."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from githubkit import GitHub as SDK

from app.contracts.result_models import ResultPending
from app.domain.models import JobState
from app.github import GitHubApp, Installation, PullRequest, Repository
from app.persistence.store import Store
from tests.github import NATIVE_PULL_REQUEST_REFETCH, NATIVE_REPOSITORY_FETCH_REF
from tests.test_coordinator import coordinator
from tests.test_github_client import settings
from tests.test_revision_delivery import verified_result
from tests.test_revision_recovery import accept
from tests.test_revision_recovery import (
    pending_revision as pending_revision,  # noqa: PLC0414 - fixture re-export
)
from tests.test_revision_recovery import (
    revision_resources as revision_resources,  # noqa: PLC0414 - fixture re-export
)


async def test_result_verification_honors_retry_after_across_restart(store, pending_revision, tmp_path, monkeypatch):
    _, job, task, github, subject = pending_revision
    accept(store, job, verified_result(task, commit_sha="revision", remote_sha="revision"))
    calls = []

    def limited(request):
        calls.append(request)
        return httpx.Response(429, json={"message": "rate limited"}, headers={"Retry-After": "600"})

    app = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(Installation, "_client", lambda _, **options: SDK(
        "test-token", async_transport=httpx.MockTransport(limited), **options,
    ))
    subject.results.effects.github = app
    before = store.now()
    with monkeypatch.context() as native:
        native.setattr(PullRequest, "refetch", NATIVE_PULL_REQUEST_REFETCH)
        native.setattr(Repository, "fetch_ref", NATIVE_REPOSITORY_FETCH_REF)
        with pytest.raises(ResultPending):
            await asyncio.wait_for(subject.results.apply(job.id), 1)
    pending = store.jobs.get_job(job.id)
    assert pending.verify_next_at >= before + timedelta(seconds=600)
    assert pending.verify_failures == 1 and pending.prepare_failures == 0
    assert len(calls) == 1
    restarted = Store(store.database_url, clock=lambda: before + timedelta(seconds=599))
    try:
        with pytest.raises(ResultPending):
            await coordinator(restarted, app).results.apply(job.id)
        assert len(calls) == 1
        # At the deadline the original accepted result can finish without running a worker again.
        monkeypatch.setattr(restarted, "_clock", lambda: pending.verify_next_at)
        github.sha = "revision"
        assert await coordinator(restarted, github).results.apply(job.id) is JobState.DONE
        assert restarted.jobs.get_job(job.id).verify_next_at is None
    finally:
        restarted.close()
