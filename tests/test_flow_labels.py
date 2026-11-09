"""Flow-only labels converge against local GitHub doubles and isolated PostgreSQL schemas."""

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.controler.config import ServerSettings
from app.controler.reconciliation.flow_labels import FlowLabelSync
from app.controler.reconciliation.labels import (
    CONFIRMED_INTERVAL,
    FLOW_LABEL_NAMES,
    LabelSynchronizer,
)
from app.controler.workflow.service import ControlService
from app.domain.models import ArchiveReason, DeliveryState, FlowState
from app.github import (
    GitHubApp,
    GitHubError,
    GitHubErrorKind,
    Installation,
    Issue,
    PullRequest,
    PullRequestMerge,
    Repository,
)
from app.persistence.activity import ActivityStore
from app.persistence.flow_labels import FlowLabelStore
from app.persistence.store import Store
from tests.test_github_client import settings
from tests.test_repository_labels import remote_inventory


class RemoteLabels:
    """All mutations are in memory; hooks inject workflow races or lost responses."""

    def __init__(self, monkeypatch):
        self.values = defaultdict(set)
        self.calls = []
        self.provisions = []
        self.failures = {}
        self.lost = set()
        self.after = None

        def unexpected(*args, **kwargs):
            raise AssertionError("unmocked GitHub request; real network is forbidden")

        async def ensure(repository, definitions):
            assert FLOW_LABEL_NAMES <= {label.name for label in definitions}
            self.provisions.append(repository.full_name)
            return ()

        async def fetch(target):
            await self.call("read", target, None)
            return sorted(self.values[target.number])

        async def add(target, name):
            self.values[target.number].add(name)
            await self.call("add", target, name)

        async def remove(target, name):
            self.values[target.number].discard(name)
            await self.call("remove", target, name)

        monkeypatch.setattr(GitHubApp, "_client", unexpected)
        monkeypatch.setattr(Installation, "_client", unexpected)
        monkeypatch.setattr(Repository, "ensure_labels", ensure)
        for resource in (Issue, PullRequest):
            monkeypatch.setattr(resource, "fetch_labels", fetch)
            monkeypatch.setattr(resource, "add_label", add)
            monkeypatch.setattr(resource, "remove_label", remove)

    async def call(self, operation, target, name):
        self.calls.append((operation, target.number, name))
        key = operation, target.number
        if self.after is not None:
            await self.after(operation, target.number, name)
        if key in self.failures:
            raise self.failures[key]
        if key in self.lost:
            self.lost.remove(key)
            raise GitHubError(GitHubErrorKind.TRANSIENT, operation)


@pytest.fixture
def clock():
    return SimpleNamespace(now=datetime(2026, 11, 9, tzinfo=UTC))


@pytest.fixture
def store(postgresql_url, clock):
    instance = Store(postgresql_url, clock=lambda: clock.now)
    try:
        instance.init_db()
        yield instance
    finally:
        instance.close()


@pytest.fixture
def scenario(store, tmp_path, monkeypatch):
    configured = settings(tmp_path).model_copy(
        update={
            "in_review_label": "custom:review",
            "done_label": "custom:done",
        }
    )
    github = GitHubApp(configured)
    remote = RemoteLabels(monkeypatch)
    flow = store.flows.create_flow("owner/repo", 7, 42)
    sync = FlowLabelSync(
        github, configured, database=store.database,
        repository_labels=LabelSynchronizer(github, configured), interval=30, concurrency=1,
    )
    return SimpleNamespace(flow=flow, sync=sync, remote=remote, settings=configured)


def bind_pr(store, flow, number=17):
    delivery = store.deliveries.get_delivery(flow.id)
    if delivery is None:
        delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.pr_number, delivery.state = number, DeliveryState.OPEN
    store.deliveries.update_delivery(delivery)
    return delivery


def target(store, number=7):
    with store.connection() as connection:
        identity = connection.execute(
            "SELECT id FROM flow_label_targets WHERE repo='owner/repo' AND number=%s",
            (number,),
        ).fetchone()["id"]
    return FlowLabelStore(store.database).get(identity)


@pytest.mark.parametrize("field", ["trigger_label", "in_review_label", "done_label"])
@pytest.mark.parametrize("state", list(FlowState))
def test_config_rejects_trigger_and_migration_aliases_of_managed_labels(tmp_path, field, state):
    values = settings(tmp_path).model_dump()
    values[field] = f"  FLOW:{state.value.upper()}  "
    with pytest.raises(ValidationError, match="overlap managed Flow state labels"):
        ServerSettings.model_validate(values)


async def test_custom_trigger_that_was_a_legacy_default_is_preserved(store, scenario):
    configured = ServerSettings.model_validate(
        {
            **scenario.settings.model_dump(),
            "trigger_label": "Flow:Done",
        }
    )
    scenario.remote.values[7] = {"Flow:Done", "flow:in-review", "custom:review", "custom:done"}
    sync = FlowLabelSync(
        scenario.sync.github, configured, database=store.database,
        repository_labels=LabelSynchronizer(scenario.sync.github, configured),
    )
    await sync.sweep()
    assert scenario.remote.values[7] == {"Flow:Done", "flow:queued"}


async def test_bootstrap_and_multiple_targets_share_expiring_repository_confirmation(
    store, scenario, clock, monkeypatch,
):
    # Each due Issue/PR used to scan every repository-label page, in addition to bootstrap.
    sync, remote = scenario.sync, scenario.remote
    sync.concurrency = 4
    bind_pr(store, scenario.flow)
    for number in (8, 9):
        store.flows.create_flow("owner/repo", number, 42)
    remote_inventory(monkeypatch, sync.github, ["owner/repo"])
    now = 0.0
    monkeypatch.setattr("app.controler.reconciliation.labels.monotonic", lambda: now)

    await asyncio.gather(sync.sweep(), sync.repository_labels.sweep())
    assert remote.provisions == ["owner/repo"]
    assert all(remote.values[number] == {"flow:queued"} for number in (7, 8, 9, 17))
    remote.values[7] = {"flow:done", "user"}
    clock.now += timedelta(seconds=31)
    await sync.sweep()
    assert remote.values[7] == {"flow:queued", "user"}
    assert len(remote.provisions) == 1
    now = CONFIRMED_INTERVAL
    clock.now += timedelta(seconds=31)
    await asyncio.gather(sync.sweep(), sync.repository_labels.sweep())
    assert remote.provisions == ["owner/repo", "owner/repo"]


async def test_repository_provisioning_failure_defers_target_and_can_retry(
    store, scenario, clock, monkeypatch,
):
    # Repository provisioning errors must not authorize target writes or become a success hint.
    sync, remote = scenario.sync, scenario.remote
    failed = True
    ensure = Repository.ensure_labels

    async def provision(repository, definitions):
        created = await ensure(repository, definitions)
        if failed:
            raise GitHubError(GitHubErrorKind.PERMISSION, "create_label", status_code=403)
        return created

    monkeypatch.setattr(Repository, "ensure_labels", provision)
    await sync.sweep()
    assert remote.calls == []
    assert target(store).failures == 1
    assert target(store).next_at == clock.now + timedelta(seconds=300)
    failed = False
    await sync.sweep()
    assert len(remote.provisions) == 1
    clock.now += timedelta(seconds=300)
    await sync.sweep()
    assert len(remote.provisions) == 2
    assert remote.values[7] == {"flow:queued"}
    assert target(store).failures == 0


async def test_authority_change_during_repository_provisioning_prevents_target_io(
    store, scenario, monkeypatch,
):
    # Provisioning may wait behind bootstrap; workflow writers must remain free to change state.
    sync, remote, flow = scenario.sync, scenario.remote, scenario.flow
    entered, release = asyncio.Event(), asyncio.Event()
    ensure = Repository.ensure_labels

    async def provision(repository, definitions):
        entered.set()
        await release.wait()
        return await ensure(repository, definitions)

    monkeypatch.setattr(Repository, "ensure_labels", provision)
    task = asyncio.create_task(sync.sweep())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with store.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '1s'")
            connection.execute("UPDATE flows SET state='running' WHERE id=%s", (flow.id,))
    finally:
        release.set()
        await task
    assert remote.calls == []
    assert target(store).next_at <= store.now()
    await sync.sweep()
    assert remote.values[7] == {"flow:running"}
    assert len(remote.provisions) == 1


@pytest.mark.parametrize("state", list(FlowState))
async def test_issue_and_current_pr_use_only_flow_state(store, scenario, state):
    flow, remote, sync = scenario.flow, scenario.remote, scenario.sync
    delivery = bind_pr(store, flow)
    if state is FlowState.ARCHIVED:
        ControlService(database=store.database).cancel(flow)
    else:
        flow.state = state
        if state is FlowState.FINALIZING:
            flow.archive_reason = ArchiveReason.ABANDONED
        store.flows.update_flow(flow)
        # Delivery observations are deliberately inconsistent with Flow.state.
        delivery.state = DeliveryState.CLOSED
        store.deliveries.update_delivery(delivery)
    before = store.flows.get_flow(flow.id)
    initial = {
        "FLOW",
        "user",
        "Flow:In-Review",
        "flow:done",
        "custom:review",
        "custom:done",
        *FLOW_LABEL_NAMES,
    }
    remote.values[7], remote.values[17] = initial.copy(), initial.copy()
    await sync.sweep()
    assert remote.values[7] == remote.values[17] == {"FLOW", "user", f"flow:{state.value}"}
    assert store.flows.get_flow(flow.id) == before
    assert target(store).failures == 0
    assert sync.labels.desired(target(store)).state is state


async def test_owner_selection_is_not_latest_flow_or_late_round_activity(store, scenario, clock):
    flow, remote, sync = scenario.flow, scenario.remote, scenario.sync
    service = ControlService(database=store.database)
    bind_pr(store, flow)
    service.cancel(flow)
    activity = ActivityStore(store.database)
    archived = activity.archived(flow.id, 1)
    assert archived is not None
    newer = store.flows.create_flow(flow.repo, flow.issue_number, 42)
    await sync.sweep()
    assert remote.values[7] == {"flow:queued"}
    assert remote.values[17] == {"flow:archived"}
    # Late evidence and a newer timestamp must not steal the explicitly selected owner.
    with store.connection() as connection:
        connection.execute(
            "UPDATE flows SET updated_at=%s WHERE id=%s", (clock.now + timedelta(days=1), flow.id)
        )
    store.flows.observe_archived_merge(
        flow.id,
        1,
        PullRequestMerge(
            number=17,
            merged=True,
            merged_at=archived.occurred_at - timedelta(seconds=5),
            merge_commit_sha="a" * 40,
        ),
        origin="reconciliation",
    )
    assert activity.archived(flow.id, 1) == archived
    assert store.flows.get_flow(flow.id).archive_reason is ArchiveReason.SUCCEEDED
    clock.now += timedelta(seconds=31)
    await sync.sweep()
    assert remote.values[7] == {"flow:queued"}
    assert sync.labels.desired(target(store)).flow_id == newer.id
    # Explicitly resuming an older Flow must beat the newer numeric ID, including after archive.
    service.cancel(newer)
    resumed = service.resume(store.flows.get_flow(flow.id), expected_round_no=1)
    service.cancel(resumed)
    clock.now += timedelta(seconds=31)
    await sync.sweep()
    assert sync.labels.desired(target(store)).flow_id == flow.id
    assert remote.values[7] == remote.values[17] == {"flow:archived"}


async def test_resumed_round_has_new_pr_while_old_pr_stays_archived(store, scenario, clock):
    flow, remote, sync = scenario.flow, scenario.remote, scenario.sync
    service = ControlService(database=store.database)
    bind_pr(store, flow)
    service.cancel(flow)
    activity = ActivityStore(store.database)
    archived = activity.archived(flow.id, 1)
    assert archived is not None
    resumed = service.resume(store.flows.get_flow(flow.id), expected_round_no=1)
    bind_pr(store, resumed, 18)
    resumed.state = FlowState.REVIEW
    store.flows.update_flow(resumed)
    remote.values[17] = {"flow:running", "custom:review", "user"}
    await sync.sweep()
    assert remote.values[7] == remote.values[18] == {"flow:review"}
    assert remote.values[17] == {"flow:archived", "user"}
    assert sync.labels.desired(target(store, 17)).round_no == 1
    assert sync.labels.desired(target(store, 18)).round_no == 2
    store.flows.observe_archived_merge(
        flow.id,
        1,
        PullRequestMerge(
            number=17,
            merged=True,
            merged_at=archived.occurred_at - timedelta(seconds=5),
            merge_commit_sha="a" * 40,
        ),
        origin="reconciliation",
    )
    assert activity.archived(flow.id, 1) == archived
    assert store.flows.get_flow(flow.id) == resumed
    clock.now += timedelta(seconds=31)
    await sync.sweep()
    assert remote.values[7] == remote.values[18] == {"flow:review"}
    assert remote.values[17] == {"flow:archived", "user"}


async def test_pr_becoming_historical_during_write_is_not_confirmed(store, scenario, clock):
    flow, remote, sync = scenario.flow, scenario.remote, scenario.sync
    service = ControlService(database=store.database)
    bind_pr(store, flow)
    flow.state = FlowState.REVIEW
    store.flows.update_flow(flow)

    async def resume_during_write(operation, number, name):
        if operation != "add" or number != 17:
            return
        remote.after = None
        service.cancel(flow)
        resumed = service.resume(store.flows.get_flow(flow.id), expected_round_no=1)
        bind_pr(store, resumed, 18)
        resumed.state = FlowState.RUNNING
        store.flows.update_flow(resumed)

    remote.after = resume_during_write
    await sync.sweep()
    assert target(store, 17).next_at <= clock.now
    clock.now += timedelta(seconds=31)
    await sync.sweep()
    assert remote.values[7] == remote.values[18] == {"flow:running"}
    assert remote.values[17] == {"flow:archived"}


@pytest.mark.parametrize("operation", ["read", "add", "remove"])
@pytest.mark.parametrize("change", ["state", "owner"])
async def test_inflight_state_and_owner_changes_remain_pending(store, scenario, operation, change):
    flow, remote, sync = scenario.flow, scenario.remote, scenario.sync
    remote.values[7] = {"flow:blocked", "user", "flow"}
    replacement = flow

    async def changed(action, number, name):
        nonlocal replacement
        if action != operation:
            return
        remote.after = None
        if change == "owner":
            ControlService(database=store.database).cancel(flow)
            replacement = store.flows.create_flow(flow.repo, flow.issue_number, 42)
        replacement.state = FlowState.RUNNING
        store.flows.update_flow(replacement)
        remote.values[7].add("concurrently-added-user-label")

    remote.after = changed
    await sync.sweep()
    assert target(store).next_at <= store.now()
    if operation == "read":
        assert not any(call[0] in {"add", "remove"} for call in remote.calls)
    await sync.sweep()
    assert remote.values[7] == {"flow:running", "flow", "user", "concurrently-added-user-label"}
    assert sync.labels.desired(target(store)).flow_id == replacement.id


@pytest.mark.parametrize("operation", ["add", "remove"])
@pytest.mark.parametrize("change_state", [False, True])
async def test_lost_write_restart_rereads_desired_and_remote(
    store,
    scenario,
    clock,
    operation,
    change_state,
):
    flow, remote, sync = scenario.flow, scenario.remote, scenario.sync
    remote.values[7] = {"flow:review", "user"}
    remote.lost.add((operation, 7))
    before = store.flows.get_flow(flow.id)
    await sync.sweep()
    assert target(store).failures == 1
    assert store.flows.get_flow(flow.id) == before
    if change_state:
        flow.state = FlowState.FINALIZING
        flow.archive_reason = ArchiveReason.ABANDONED
        store.flows.update_flow(flow)
    reopened = Store(store.database_url, clock=lambda: clock.now)
    try:
        restarted = FlowLabelSync(
            sync.github, scenario.settings, database=reopened.database,
            repository_labels=LabelSynchronizer(sync.github, scenario.settings),
        )
        calls = remote.calls.copy()
        await restarted.sweep()
        assert remote.calls == calls  # Durable Retry-After/backoff survives restart.
        clock.now += timedelta(seconds=3)
        await restarted.sweep()
        expected = "flow:finalizing" if change_state else "flow:queued"
        assert remote.values[7] == {expected, "user"}
        assert target(reopened).failures == 0
        assert remote.calls.count(("add", 7, "flow:queued")) == 1
        assert remote.calls.count(("remove", 7, "flow:review")) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "kind,delay", [(GitHubErrorKind.PERMISSION, 300), (GitHubErrorKind.RATE_LIMITED, 900)]
)
async def test_failure_is_independent_durable_and_never_permanently_blocked(
    store,
    scenario,
    clock,
    kind,
    delay,
):
    flow, remote, sync = scenario.flow, scenario.remote, scenario.sync
    other = store.flows.create_flow("owner/repo", 8, 42)
    before = [store.flows.get_flow(flow.id), store.flows.get_flow(other.id)]
    remote.failures["read", 7] = GitHubError(kind, "labels", retry_after=str(delay))
    await sync.sweep()
    assert target(store).next_at == clock.now + timedelta(seconds=delay)
    assert target(store).failures == 1
    assert remote.values[8] == {"flow:queued"}
    assert [store.flows.get_flow(flow.id), store.flows.get_flow(other.id)] == before
    remote.failures.clear()
    clock.now += timedelta(seconds=delay)
    await FlowLabelSync(
        sync.github, scenario.settings, database=store.database,
        repository_labels=LabelSynchronizer(sync.github, scenario.settings),
    ).sweep()
    assert remote.values[7] == {"flow:queued"}
    assert target(store).failures == 0


async def test_success_is_not_permanent_confirmation_and_missing_owner_has_no_fallback(
    store,
    scenario,
    clock,
):
    remote, sync = scenario.remote, scenario.sync
    await sync.sweep()
    remote.values[7] = {"flow:done", "user"}
    clock.now += timedelta(seconds=31)
    await FlowLabelSync(
        sync.github, scenario.settings, database=store.database,
        repository_labels=LabelSynchronizer(sync.github, scenario.settings),
    ).sweep()
    assert remote.values[7] == {"flow:queued", "user"}
    with store.connection() as connection:
        connection.execute("DELETE FROM issue_flow_owners")
    calls = remote.calls.copy()
    clock.now += timedelta(seconds=301)
    await sync.sweep()
    assert remote.calls == calls
    assert sync.labels.desired(target(store)) is None


async def test_final_remote_read_rejects_concurrently_added_stale_state(store, scenario, clock):
    remote, sync = scenario.remote, scenario.sync

    async def interfere(operation, number, name):
        if operation == "add":
            remote.values[number].add("flow:archived")
            remote.after = None

    remote.after = interfere
    await sync.sweep()
    assert target(store).failures == 1
    assert "flow_label_verification" in target(store).last_error
    clock.now += timedelta(seconds=300)
    await sync.sweep()
    assert remote.values[7] == {"flow:queued"}
    assert target(store).failures == 0


async def test_two_controllers_serialize_and_cancellation_releases_claim(store, scenario):
    remote, sync = scenario.remote, scenario.sync
    entered = asyncio.Event()

    async def hold(operation, number, name):
        if operation == "add":
            entered.set()
            await asyncio.Event().wait()

    remote.after = hold
    task = asyncio.create_task(sync.sweep())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        competing = FlowLabelSync(
            sync.github, scenario.settings, database=store.database,
            repository_labels=LabelSynchronizer(sync.github, scenario.settings),
        )
        calls = remote.calls.copy()
        await competing.sweep()
        assert remote.calls == calls
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    remote.after = None
    await competing.sweep()
    assert remote.values[7] == {"flow:queued"}
    assert remote.calls.count(("add", 7, "flow:queued")) == 1
    assert target(store).next_at > store.now()


async def test_bounded_batches_eventually_discover_every_target(store, scenario):
    sync = FlowLabelSync(
        scenario.sync.github,
        scenario.settings,
        database=store.database,
        repository_labels=scenario.sync.repository_labels,
        batch_size=2,
        concurrency=2,
    )
    for number in range(8, 14):
        store.flows.create_flow("owner/repo", number, 42)
    for _ in range(4):
        before = len(scenario.remote.calls)
        await sync.sweep()
        assert len(scenario.remote.calls) - before <= 6  # At most two read/add/read sequences.
    assert {number for number, labels in scenario.remote.values.items() if labels} == set(
        range(7, 14)
    )
