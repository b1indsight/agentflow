"""Missed PR merges are applied atomically through the same path as real webhooks."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.control.reconcile import Reconciler
from app.control.service import ControlService
from app.github_client import GitHubApp
from app.github_models import GitHubAdapterError, GitHubErrorKind
from app.github_retry import retry_delay
from app.models import DeliveryState, FlowState, JobKind
from app.store import Store
from tests.test_github_client import AsyncEndpoint, response, settings
from tests.test_merge import (
    merge_flow as merge_flow,  # noqa: PLC0414 - fixture re-export
)
from tests.test_merge import merge_payload


def status(flow, **updates):
    return SimpleNamespace(**({
        "repo": flow.repo, "number": 17, "state": "closed", "merged": True,
        "merged_at": datetime(2026, 9, 8, 8, tzinfo=UTC), "merge_commit_sha": "a" * 40,
        "head_branch": f"flow/{flow.id}", "head_sha": "b" * 40,
        "base_branch": "main", "base_sha": "c" * 40, "head_repository": flow.repo,
    } | updates))


class GitHub:
    def __init__(self, remote):
        self.remote = remote
        self.calls = []
        self.before_return = None

    async def get_pull_request_status(self, installation, repo, number):
        self.calls.append((installation, repo, number))
        if self.before_return:
            self.before_return()
        if isinstance(self.remote, Exception):
            raise self.remote
        return self.remote


def subject(store, github, **options):
    return Reconciler(store, github, ControlService(store), **options)


async def test_missing_merge_webhook_enters_the_existing_archive_path(store, merge_flow):
    github = GitHub(status(merge_flow))
    poller = subject(store, github)
    await poller._sweep()
    await poller._sweep()
    assert store.get_delivery(merge_flow.id).state is DeliveryState.MERGED
    assert store.get_delivery(merge_flow.id).merge_commit_sha == "a" * 40
    assert [j.kind for j in store.list_jobs(merge_flow.id)] == [JobKind.ARCHIVE]
    assert github.calls == [(42, "owner/repo", 17)]
    assert store.reconcile.get(merge_flow.id).last_success_at is not None
    # Polling does not manufacture webhook delivery IDs.
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 0


async def test_merge_and_archive_roll_back_before_a_later_poll_can_retry(
    store, merge_flow, monkeypatch,
):
    poller = subject(store, GitHub(status(merge_flow)))
    original = Store.enqueue_job

    def interrupted(self, flow_id, kind, *args, **kwargs):
        if kind is JobKind.ARCHIVE:
            raise RuntimeError("private operational details")
        return original(self, flow_id, kind, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "enqueue_job", interrupted)
        await poller._sweep()
    assert store.get_delivery(merge_flow.id).state is DeliveryState.OPEN
    failed = store.reconcile.get(merge_flow.id)
    assert failed.last_success_at is None and failed.failures == 1
    assert "private operational details" not in failed.last_error
    monkeypatch.setattr(store, "_clock", lambda: failed.next_at + timedelta(seconds=1))
    await poller._sweep()
    assert [j.kind for j in store.list_jobs(merge_flow.id)] == [JobKind.ARCHIVE]


async def test_rate_limit_deadline_survives_restart(store, merge_flow, monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 9, 9, tzinfo=UTC))
    monkeypatch.setattr(store, "_clock", lambda: clock.now)
    github = GitHub(GitHubAdapterError(GitHubErrorKind.RATE_LIMITED, "status", retry_after="120"))
    await subject(store, github)._sweep()
    failed = store.reconcile.get(merge_flow.id)
    assert failed.next_at >= clock.now + timedelta(seconds=120)
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        github.remote = status(merge_flow)
        clock.now += timedelta(seconds=119)
        await subject(restarted, github)._sweep()
        assert len(github.calls) == 1
        clock.now += timedelta(seconds=2)
        await subject(restarted, github)._sweep()
        assert len(github.calls) == 2
        assert restarted.reconcile.get(merge_flow.id).failures == 0
    finally:
        restarted.close()


@pytest.mark.parametrize("kind", [GitHubErrorKind.AUTHENTICATION, GitHubErrorKind.PERMISSION,
                                 GitHubErrorKind.NOT_FOUND])
async def test_read_errors_do_not_invent_pr_lifecycle_facts(store, merge_flow, kind):
    await subject(store, GitHub(GitHubAdapterError(kind, "status")))._sweep()
    assert store.get_delivery(merge_flow.id).state is DeliveryState.OPEN
    assert store.list_jobs(merge_flow.id) == []
    assert kind.value in store.reconcile.get(merge_flow.id).last_error


@pytest.mark.parametrize("change", ["version", "attempt", "merged"])
async def test_late_query_cannot_overwrite_a_changed_workflow(store, merge_flow, change):
    github = GitHub(status(merge_flow, merged=change != "merged"))

    def mutate():
        if change == "merged":
            ControlService(store).on_merged(merge_flow, "real-merge", merge_payload(merge_flow))
        elif change == "version":
            delivery = store.get_delivery(merge_flow.id)
            delivery.version += 1
            store.update_delivery(delivery)
        else:
            flow = store.get_flow(merge_flow.id)
            flow.current_attempt_id = store.create_attempt(flow.id).id
            store.update_flow(flow)

    github.before_return = mutate
    await subject(store, github)._sweep()
    jobs = store.list_jobs(merge_flow.id)
    assert [j.kind for j in jobs] == ([JobKind.ARCHIVE] if change == "merged" else [])
    expected = DeliveryState.MERGED if change == "merged" else DeliveryState.OPEN
    assert store.get_delivery(merge_flow.id).state is expected


@pytest.mark.parametrize("state", [FlowState.FAILED, FlowState.CANCELLED])
async def test_historical_terminal_flow_only_records_merge_facts(store, merge_flow, state):
    merge_flow.state = state
    store.update_flow(merge_flow)
    await subject(store, GitHub(status(merge_flow)))._sweep()
    assert store.get_flow(merge_flow.id).state is state
    assert store.get_delivery(merge_flow.id).state is DeliveryState.MERGED
    assert store.list_jobs(merge_flow.id) == []


@pytest.mark.parametrize("updates", [{"repo": "other/repo"}, {"number": 99},
                                     {"head_repository": "fork/repo"},
                                     {"merged_at": None}, {"merge_commit_sha": None}])
async def test_unverified_remote_identity_or_merge_facts_do_not_archive(store, merge_flow, updates):
    await subject(store, GitHub(status(merge_flow, **updates)))._sweep()
    assert store.get_delivery(merge_flow.id).state is DeliveryState.OPEN
    assert store.list_jobs(merge_flow.id) == []
    assert store.reconcile.get(merge_flow.id).last_error


async def test_polling_is_bounded_and_one_error_does_not_starve_later_flows(store, merge_flow):
    for i in range(3):
        flow = store.create_flow("owner/repo", 100 + i, 42)
        delivery = store.get_or_create_delivery(flow.id, f"flow/{flow.id}")
        delivery.pr_number, delivery.state = 20 + i, DeliveryState.OPEN
        store.update_delivery(delivery)
    github = GitHub(GitHubAdapterError(GitHubErrorKind.TRANSIENT, "status"))
    poller = subject(store, github, batch_size=2)
    await poller._sweep()
    assert len(github.calls) == 2
    await poller._sweep()
    assert len(github.calls) == 4
    assert {number for _, _, number in github.calls} == {17, 20, 21, 22}


async def test_status_adapter_reads_by_number_and_keeps_merge_sha(tmp_path, monkeypatch):
    data = SimpleNamespace(number=17, state="closed", merged=True,
        merged_at=datetime(2026, 9, 8, tzinfo=UTC), merge_commit_sha="a" * 40,
        head=SimpleNamespace(ref="flow/1", sha="b" * 40, repo=SimpleNamespace(full_name="owner/repo")),
        base=SimpleNamespace(ref="main", sha="c" * 40, repo=SimpleNamespace(full_name="owner/repo")))
    endpoint = AsyncEndpoint(response(data))
    client = SimpleNamespace(rest=SimpleNamespace(pulls=SimpleNamespace(async_get=endpoint)))
    app = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(app, "_installation_client", lambda _: client)
    result = await app.get_pull_request_status(42, "owner/repo", 17)
    assert endpoint.calls == [(("owner", "repo", 17), {})]
    assert result.repo == "owner/repo" and result.merge_commit_sha == "a" * 40
    assert result.head_sha == "b" * 40
    data.merged_at = None
    with pytest.raises(GitHubAdapterError):
        await app.get_pull_request_status(42, "owner/repo", 17)


async def test_reconcile_concurrency_and_shutdown_are_bounded(store, merge_flow):
    for i in range(2):
        flow = store.create_flow("owner/repo", 100 + i, 42)
        delivery = store.get_or_create_delivery(flow.id, f"flow/{flow.id}")
        delivery.pr_number, delivery.state = 20 + i, DeliveryState.OPEN
        store.update_delivery(delivery)
    started, release = asyncio.Event(), asyncio.Event()
    active = set()

    class WaitingGitHub(GitHub):
        async def get_pull_request_status(self, installation, repo, number):
            active.add(number)
            if len(active) == 2:
                started.set()
            try:
                await release.wait()
            finally:
                active.remove(number)
            return self.remote

    sweep = asyncio.create_task(subject(store, WaitingGitHub(status(merge_flow)),
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
    ("broken", 2), ("nan", 2), ("inf", 2), ("-20", 2),
])
def test_shared_retry_timing_preserves_server_deadlines(header, expected):
    now = datetime(2026, 9, 9, tzinfo=UTC)
    error = GitHubAdapterError(GitHubErrorKind.RATE_LIMITED, "read", retry_after=header)
    assert retry_delay(1, now, error) == expected
