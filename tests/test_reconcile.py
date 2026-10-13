"""Missed PR merges are applied atomically through the same path as real webhooks."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from githubkit import GitHub as SDK

from app.controler.reconciliation.pull_requests import Reconciler
from app.controler.workflow.service import ControlService
from app.domain.models import DeliveryState, FlowState, JobKind
from app.github import (
    GitHubApp,
    GitHubError,
    GitHubErrorKind,
    Installation,
    PullRequest,
    retry_delay,
)
from app.persistence.jobs import JobStore
from app.persistence.store import Store
from tests.github import RecordingGitHub, record_call
from tests.test_github_client import AsyncEndpoint, response, settings
from tests.test_merge import (
    merge_flow as merge_flow,  # noqa: PLC0414 - fixture re-export
)
from tests.test_merge import merge_payload


def status(flow, **updates):
    return PullRequest(
        repo=flow.repo, number=17, state="closed", merged=True,
        merged_at=datetime(2026, 9, 8, 8, tzinfo=UTC), merge_commit_sha="a" * 40,
        head_branch=f"flow/{flow.id}", head_sha="b" * 40,
        base_branch="main", base_sha="c" * 40, head_repository=flow.repo,
    ).model_copy(update=updates)


class GitHub(RecordingGitHub):
    def __init__(self, remote):
        super().__init__()
        self.remote = remote
        self.before_return = None


@pytest.fixture
def polling_resources(recording_resources, monkeypatch):
    async def refetch(pr):
        state = record_call(pr.repository, "fetch_pull_request", number=pr.number)
        if state.before_return:
            state.before_return()
        if isinstance(state.remote, Exception):
            raise state.remote
        return PullRequest(pr.repository, **state.remote.model_dump(exclude_unset=True))

    monkeypatch.setattr(PullRequest, "refetch", refetch)


def subject(store, github, **options):
    return Reconciler(
        github,
        ControlService(
            database=store.database,
            deliveries=store.deliveries,
            flows=store.flows,
            jobs=store.jobs,
            result_store=store.results,
            reviews=store.reviews,
            stops=store.stops,
            webhook_store=store.webhooks,
            workers=store.workers,
        ),
        **options,
        database=store.database,
        deliveries=store.deliveries,
        flows=store.flows,
        jobs=store.jobs,
        reconcile_store=store.reconcile,
        reviews=store.reviews,
        stops=store.stops,
        workers=store.workers,
    )


async def test_missing_merge_webhook_enters_the_existing_archive_path(store, merge_flow, polling_resources):
    github = GitHub(status(merge_flow))
    poller = subject(store, github)
    await poller._sweep()
    await poller._sweep()
    assert store.deliveries.get_delivery(merge_flow.id).state is DeliveryState.MERGED
    assert store.deliveries.get_delivery(merge_flow.id).merge_commit_sha == "a" * 40
    assert [j.kind for j in store.jobs.list_jobs(merge_flow.id)] == [JobKind.ARCHIVE]
    assert github.resource_calls == [{
        "operation": "fetch_pull_request", "installation_id": merge_flow.installation_id,
        "repo": merge_flow.repo, "number": 17,
    }]
    assert store.reconcile.get(merge_flow.id).last_success_at is not None
    # Polling does not manufacture webhook delivery IDs.
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 0


async def test_merge_and_archive_roll_back_before_a_later_poll_can_retry(
    store, merge_flow, monkeypatch, polling_resources):
    poller = subject(store, GitHub(status(merge_flow)))
    original = JobStore.enqueue_job

    def interrupted(self, flow_id, kind, *args, **kwargs):
        if kind is JobKind.ARCHIVE:
            raise RuntimeError("private operational details")
        return original(self, flow_id, kind, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(JobStore, "enqueue_job", interrupted)
        await poller._sweep()
    assert store.deliveries.get_delivery(merge_flow.id).state is DeliveryState.OPEN
    failed = store.reconcile.get(merge_flow.id)
    assert failed.last_success_at is None and failed.failures == 1
    assert "private operational details" not in failed.last_error
    monkeypatch.setattr(store._db, "_clock", lambda: failed.next_at + timedelta(seconds=1))
    await poller._sweep()
    assert [j.kind for j in store.jobs.list_jobs(merge_flow.id)] == [JobKind.ARCHIVE]


async def test_open_polling_resumes_its_success_and_retry_deadlines_after_restart(
    store, merge_flow, monkeypatch, polling_resources):
    clock = SimpleNamespace(now=datetime(2026, 9, 18, tzinfo=UTC))
    monkeypatch.setattr(store._db, "_clock", lambda: clock.now)
    remote = status(merge_flow, state="open", merged=False)
    github = GitHub(remote)
    await subject(store, github)._sweep()
    first = store.reconcile.get(merge_flow.id)
    assert first.next_at == clock.now + timedelta(seconds=300)
    # Restart after an ordinary OPEN response, rather than only after a missed merge.
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        clock.now = first.next_at - timedelta(seconds=1)
        await subject(restarted, github)._sweep()
        assert len(github.resource_calls) == 1
        clock.now = first.next_at
        github.remote = GitHubError(GitHubErrorKind.RATE_LIMITED, "status", retry_after="120")
        await subject(restarted, github)._sweep()
        failed = restarted.reconcile.get(merge_flow.id)
        assert failed.failures == 1 and failed.last_success_at == first.last_success_at
        assert failed.next_at == clock.now + timedelta(seconds=120)
    finally:
        restarted.close()
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        github.remote = remote
        clock.now = failed.next_at - timedelta(seconds=1)
        await subject(restarted, github)._sweep()
        assert len(github.resource_calls) == 2
        clock.now = failed.next_at
        await subject(restarted, github)._sweep()
        assert len(github.resource_calls) == 3
        recovered = restarted.reconcile.get(merge_flow.id)
        assert recovered.failures == 0 and recovered.last_error is None
        assert recovered.next_at == clock.now + timedelta(seconds=300)
        assert restarted.deliveries.get_delivery(merge_flow.id).state is DeliveryState.OPEN
        assert restarted.jobs.list_jobs(merge_flow.id) == []
    finally:
        restarted.close()


async def test_read_errors_do_not_invent_pr_lifecycle_facts(store, merge_flow, polling_resources):
    await subject(store, GitHub(GitHubError(GitHubErrorKind.NOT_FOUND, "status")))._sweep()
    assert store.deliveries.get_delivery(merge_flow.id).state is DeliveryState.OPEN
    assert store.jobs.list_jobs(merge_flow.id) == []
    assert GitHubErrorKind.NOT_FOUND.value in store.reconcile.get(merge_flow.id).last_error


@pytest.mark.parametrize("change", ["version", "merged"])
async def test_late_query_cannot_overwrite_a_changed_workflow(store, merge_flow, change, polling_resources):
    github = GitHub(status(merge_flow, merged=change != "merged"))

    def mutate():
        if change == "merged":
            ControlService(
                database=store.database,
                deliveries=store.deliveries,
                flows=store.flows,
                jobs=store.jobs,
                result_store=store.results,
                reviews=store.reviews,
                stops=store.stops,
                webhook_store=store.webhooks,
                workers=store.workers,
            ).on_merged(merge_flow, "real-merge", merge_payload(merge_flow))
        elif change == "version":
            delivery = store.deliveries.get_delivery(merge_flow.id)
            delivery.version += 1
            store.deliveries.update_delivery(delivery)

    github.before_return = mutate
    await subject(store, github)._sweep()
    jobs = store.jobs.list_jobs(merge_flow.id)
    assert [j.kind for j in jobs] == ([JobKind.ARCHIVE] if change == "merged" else [])
    expected = DeliveryState.MERGED if change == "merged" else DeliveryState.OPEN
    assert store.deliveries.get_delivery(merge_flow.id).state is expected


@pytest.mark.parametrize("state", [FlowState.FAILED, FlowState.CANCELLED])
async def test_historical_terminal_flow_only_records_merge_facts(store, merge_flow, state, polling_resources):
    merge_flow.state = state
    store.flows.update_flow(merge_flow)
    await subject(store, GitHub(status(merge_flow)))._sweep()
    assert store.flows.get_flow(merge_flow.id).state is state
    assert store.deliveries.get_delivery(merge_flow.id).state is DeliveryState.MERGED
    assert store.jobs.list_jobs(merge_flow.id) == []


async def test_polling_is_bounded_and_one_error_does_not_starve_later_flows(store, merge_flow, polling_resources):
    for i in range(3):
        flow = store.flows.create_flow(f"owner/repo-{i}", 100 + i, 43 + i)
        delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
        delivery.pr_number, delivery.state = 20 + i, DeliveryState.OPEN
        store.deliveries.update_delivery(delivery)
    github = GitHub(GitHubError(GitHubErrorKind.TRANSIENT, "status"))
    poller = subject(store, github, batch_size=2)
    await poller._sweep()
    assert len(github.resource_calls) == 2
    await poller._sweep()
    assert len(github.resource_calls) == 4
    assert {(call["installation_id"], call["repo"], call["number"])
            for call in github.resource_calls} == {
        (42, "owner/repo", 17), (43, "owner/repo-0", 20),
        (44, "owner/repo-1", 21), (45, "owner/repo-2", 22),
    }


async def test_status_adapter_reads_by_number_and_keeps_merge_sha(tmp_path, monkeypatch):
    data = SimpleNamespace(number=17, state="closed", merged=True, title="PR", body=None,
        merged_at=datetime(2026, 9, 8, tzinfo=UTC), merge_commit_sha="a" * 40,
        head=SimpleNamespace(ref="flow/1", sha="b" * 40, repo=SimpleNamespace(full_name="owner/repo")),
        base=SimpleNamespace(ref="main", sha="c" * 40, repo=SimpleNamespace(full_name="owner/repo")))
    endpoint = AsyncEndpoint(response(data))
    client = SimpleNamespace(rest=SimpleNamespace(pulls=SimpleNamespace(async_get=endpoint)))
    app = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(Installation, "_client", lambda self, **kwargs: client)
    repo = app.installation(42).repository("owner/repo")
    result = await repo.fetch_pull_request(17)
    assert endpoint.calls == [(("owner", "repo", 17), {})]
    assert result.title == "PR" and result.body == ""
    assert result.repo == "owner/repo" and result.merge_commit_sha == "a" * 40
    assert result.head_sha == "b" * 40
    data.merged_at = None
    with pytest.raises(GitHubError):
        await repo.fetch_pull_request(17)


async def test_reconcile_concurrency_and_shutdown_are_bounded(store, merge_flow, polling_resources, monkeypatch):
    for i in range(2):
        flow = store.flows.create_flow("owner/repo", 100 + i, 42)
        delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
        delivery.pr_number, delivery.state = 20 + i, DeliveryState.OPEN
        store.deliveries.update_delivery(delivery)
    started, release = asyncio.Event(), asyncio.Event()
    active = set()

    async def waiting_read(pr):
        record_call(pr.repository, "fetch_pull_request", number=pr.number)
        active.add(pr.number)
        if len(active) == 2:
            started.set()
        try:
            await release.wait()
        finally:
            active.remove(pr.number)
        return PullRequest(pr.repository, **github.remote.model_dump(exclude_unset=True))

    github = GitHub(status(merge_flow))
    monkeypatch.setattr(PullRequest, "refetch", waiting_read)
    sweep = asyncio.create_task(subject(store, github,
                                       concurrency=2)._sweep())
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert active == {17, 20}
    finally:
        sweep.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweep
    assert active == set()


@pytest.mark.parametrize("header,expected", [
    ("120", 120), ("Wed, 09 Sep 2026 00:02:00 GMT", 120),
    ("broken", 2),
])
def test_shared_retry_timing_preserves_server_deadlines(header, expected):
    now = datetime(2026, 9, 9, tzinfo=UTC)
    error = GitHubError(GitHubErrorKind.RATE_LIMITED, "read", retry_after=header)
    assert retry_delay(1, now, error) == expected


async def test_sdk_rate_limit_reaches_durable_poll_owner(store, merge_flow, tmp_path, monkeypatch):
    calls = []

    def limited(request):
        calls.append(request)
        return httpx.Response(429, json={"message": "rate limited"}, headers={"Retry-After": "600"})

    app = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(Installation, "_client", lambda self, **options: SDK(
        "test-token", async_transport=httpx.MockTransport(limited), **options,
    ))
    # SDK sleep must not hold the failure outside PostgreSQL until cancellation.
    before = store.now()
    await asyncio.wait_for(subject(store, app)._sweep(), 1)
    deadline = store.reconcile.get(merge_flow.id).next_at
    assert deadline >= before + timedelta(seconds=600)
    assert len(calls) == 1
    restarted = Store(store.database_url, clock=store.now)
    try:
        await subject(restarted, app)._sweep()
        assert len(calls) == 1 and restarted.reconcile.get(merge_flow.id).next_at == deadline
    finally:
        restarted.close()
