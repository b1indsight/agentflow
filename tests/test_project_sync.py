"""PostgreSQL projections recover independently from worker/Flow transactions."""

import asyncio
from datetime import timedelta

import pytest

from app.control.projects import ProjectSync
from app.control.service import ControlService
from app.domain.github_models import GitHubAdapterError, GitHubErrorKind
from app.domain.models import DeliveryState, FlowState, JobKind, JobState
from app.domain.project_models import ProjectItem, ProjectItemsPage, ProjectStatus, ProjectTarget
from app.persistence.project_store import ProjectStore
from app.persistence.store import Store
from tests.test_github_projects import config
from tests.test_review_batches import review_flow as review_flow  # noqa: PLC0414


class Remote:
    def __init__(self):
        self.item = None
        self.calls = []
        self.failure = None
        self.after_read = None
        self.after_set = None
        self.pages = None
        self.options = config().options

    async def validate(self, repo):
        self.calls.append("validate")
        if self.failure == "offline":
            raise GitHubAdapterError(GitHubErrorKind.RATE_LIMITED, "projects", retry_after="600")
        return ProjectTarget(title="board", status_name="Status")

    async def issue_id(self, repo, number):
        self.calls.append("issue")
        return f"ISSUE-{number}"

    async def items_page(self, cursor):
        self.calls.append(("page", cursor))
        if self.pages:
            return self.pages[cursor]
        return ProjectItemsPage(items=[self.item] if self.item else [], next_cursor=None, complete=True)

    async def read_item(self, item_id):
        self.calls.append("read")
        value = self.item.model_copy() if self.item else None
        if self.after_read:
            await self.after_read()
        return value

    async def add_issue(self, content_id):
        self.calls.append("add")
        self.item = ProjectItem(id="ITEM", content_id=content_id, archived=False)
        if self.failure == "add":
            raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "add")
        return self.item.model_copy()

    async def set_status(self, item_id, status):
        self.calls.append(("set", status))
        self.item.option_id = self.options[status]
        if self.after_set:
            await self.after_set()
        if self.failure == "set":
            raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "set")


@pytest.fixture
def projection(store, review_flow, monkeypatch):
    flow, clock = review_flow
    monkeypatch.setattr("app.persistence.store.utcnow", lambda: clock.now)
    configs = {flow.repo: config()}
    remote = Remote()
    sync = ProjectSync(store, configs, lambda _: remote, interval=30, batch_size=8, concurrency=2)
    return flow, clock, configs, remote, sync


def state(store):
    return ProjectStore(store).list_projections()[0]


async def converge(sync, times=3):
    for _ in range(times):
        await sync.sweep()


@pytest.mark.parametrize("flow_state,delivery,expected", [
    (FlowState.SUCCEEDED, DeliveryState.MERGED, ProjectStatus.DONE),
    (FlowState.FAILED, DeliveryState.OPEN, ProjectStatus.BLOCKED),
    (FlowState.CANCELLED, DeliveryState.OPEN, ProjectStatus.BLOCKED),
    (FlowState.ACTIVE, DeliveryState.CLOSED, ProjectStatus.BLOCKED),
    (FlowState.ACTIVE, DeliveryState.MERGED, ProjectStatus.IN_PROGRESS),
    (FlowState.ACTIVE, DeliveryState.OPEN, ProjectStatus.IN_REVIEW),
])
async def test_priority_mapping_including_terminals(store, projection, flow_state, delivery, expected):
    flow, clock, _, remote, sync = projection
    await sync.sweep()
    flow.state = flow_state
    store.update_flow(flow)
    current = store.get_delivery(flow.id)
    current.state = delivery
    store.update_delivery(current)
    clock.now += timedelta(seconds=31)
    await converge(sync)
    assert state(store).desired_status is expected
    assert remote.item.option_id == config().options[expected]


async def test_unbound_initial_is_todo_then_bound_work_is_in_progress(store, projection):
    flow, clock, _, _, sync = projection
    delivery = store.get_delivery(flow.id)
    delivery.state, delivery.pr_number = DeliveryState.NONE, None
    store.update_delivery(delivery)
    store.enqueue_job(flow.id, JobKind.INITIAL)
    await converge(sync)
    assert state(store).desired_status is ProjectStatus.TODO
    from app.contracts.protocol import PROTOCOL_VERSION

    store.worker_connected("project-worker", PROTOCOL_VERSION, "epoch")
    assert store.assign_job("project-worker", "token", "epoch") is not None
    clock.now += timedelta(seconds=31)
    await sync.sweep()
    assert state(store).desired_status is ProjectStatus.IN_PROGRESS


@pytest.mark.parametrize("lost", ["add", "set"])
async def test_lost_response_restart_reads_before_another_write(store, projection, lost):
    _, clock, configs, remote, sync = projection
    remote.failure = lost
    await converge(sync)
    before = state(store)
    assert before.failures == 1 and before.synced_version < before.desired_version
    remote.failure = None
    clock.now += timedelta(seconds=61)
    reopened = Store(store.database_url, clock=lambda: clock.now)
    try:
        await converge(ProjectSync(reopened, configs, lambda _: remote, interval=30))
        assert state(reopened).synced_version == state(reopened).desired_version
        assert remote.calls.count("add") == 1
        assert remote.calls.count(("set", ProjectStatus.IN_REVIEW)) == 1
    finally:
        reopened.close()


async def test_same_issue_new_flow_takes_over_existing_card(store, projection):
    flow, clock, _, remote, sync = projection
    await converge(sync)
    flow.state = FlowState.FAILED
    store.update_flow(flow)
    fresh = store.create_flow(flow.repo, flow.issue_number, 42)
    clock.now += timedelta(seconds=31)
    await converge(sync)
    assert len(ProjectStore(store).list_projections()) == 1
    assert state(store).flow_id == fresh.id and state(store).desired_status is ProjectStatus.TODO
    assert remote.calls.count("add") == 1
    # A late update on the old Flow cannot become owner again.
    flow.state = FlowState.SUCCEEDED
    store.update_flow(flow)
    clock.now += timedelta(seconds=31)
    await sync.sweep()
    assert state(store).flow_id == fresh.id


async def test_new_desired_version_during_set_is_not_acknowledged_by_old_response(store, projection):
    flow, _, _, remote, sync = projection

    async def change():
        flow.state = FlowState.SUCCEEDED
        store.update_flow(flow)
        ProjectStore(store).refresh(state(store).id, config())

    remote.after_set = change
    await sync.sweep()
    current = state(store)
    assert current.desired_status is ProjectStatus.DONE
    assert current.synced_version < current.desired_version
    remote.after_set = None
    await converge(sync)
    assert remote.item.option_id == "done"


async def test_change_during_read_prevents_obsolete_write(store, projection):
    flow, _, _, remote, sync = projection
    remote.item = ProjectItem(id="ITEM", content_id=f"ISSUE-{flow.issue_number}", archived=False)

    async def change():
        flow.state = FlowState.SUCCEEDED
        store.update_flow(flow)

    remote.after_read = change
    await sync.sweep()
    assert ("set", ProjectStatus.IN_REVIEW) not in remote.calls
    remote.after_read = None
    await converge(sync)
    assert remote.item.option_id == "done"


@pytest.mark.parametrize("removed", ["deleted", "archived"])
async def test_manual_removal_is_never_reversed_even_after_retry(store, projection, removed):
    flow, clock, _, remote, sync = projection
    await converge(sync)
    if removed == "deleted":
        remote.item = None
    else:
        remote.item.archived = True
    clock.now += timedelta(seconds=31)
    await sync.sweep()
    assert state(store).blocked
    ProjectStore(store).retry(flow.repo)
    await sync.sweep()
    assert state(store).blocked and remote.calls.count("add") == 1


async def test_discovered_archived_card_recovers_after_restore_and_restart(store, projection, tmp_path, monkeypatch):
    import json

    import httpx
    from githubkit import GitHub as SDK

    from app.github.client import GitHubApp
    from tests.test_github_client import settings
    from tests.test_github_projects import item, page

    flow, clock, configs, remote, sync = projection
    remote.item = ProjectItem(id="ITEM", content_id=f"ISSUE-{flow.issue_number}", archived=True)

    def transport(request):
        # Match GitHub's default filter: archived membership needs an explicit query argument.
        query = json.loads(request.content)["query"]
        nodes = [item(remote.item.content_id, archived=True)] if "archivedStates:" in query else []
        return httpx.Response(200, json={"data": {"node": {"id": "P", "items": page(nodes)}}})

    async def unexpected_add(content):
        pytest.fail("an existing archived card must not enter the add path")

    app = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(app, "_installation_client", lambda _, **options: SDK(
        "test-token", async_transport=httpx.MockTransport(transport), **options,
    ))
    monkeypatch.setattr(remote, "items_page", app.project_client(config()).items_page)
    monkeypatch.setattr(remote, "add_issue", unexpected_add)
    await sync.sweep()
    assert state(store).item_id == "ITEM" and state(store).blocked
    assert not state(store).add_started
    # Restoring the card is a human action; retry resumes the saved item without another add.
    remote.item.archived = False
    reopened = Store(store.database_url, clock=lambda: clock.now)
    try:
        ProjectStore(reopened).retry(flow.repo)
        await ProjectSync(reopened, configs, lambda _: remote).sweep()
        row = state(reopened)
        assert row.item_id == "ITEM" and not row.blocked
        assert row.synced_version == row.desired_version
    finally:
        reopened.close()


async def test_pagination_and_invisible_content_never_trigger_duplicate_add(store, projection):
    flow, _, _, remote, sync = projection
    remote.item = ProjectItem(id="ITEM", content_id=f"ISSUE-{flow.issue_number}", archived=False)
    invisible = ProjectItem(id="HIDDEN", content_id=None, archived=False)
    remote.pages = {None: ProjectItemsPage(items=[invisible], next_cursor="second", complete=False),
                   "second": ProjectItemsPage(items=[remote.item], next_cursor=None, complete=True)}
    await sync.sweep()
    assert state(store).cursor == "second" and state(store).scan_incomplete
    await converge(sync)
    assert "add" not in remote.calls and state(store).item_id == "ITEM"


async def test_incomplete_absence_and_ambiguous_add_require_confirmation(store, projection):
    flow, _, _, remote, sync = projection
    remote.pages = {None: ProjectItemsPage(items=[], next_cursor=None, complete=False)}
    await sync.sweep()
    assert state(store).blocked and "add" not in remote.calls
    remote.pages = None
    ProjectStore(store).retry(flow.repo)
    with store.connection() as connection:
        connection.execute("UPDATE project_projections SET add_started=TRUE")
    await sync.sweep()
    assert state(store).blocked and "add" not in remote.calls


async def test_rate_limit_deadline_survives_and_does_not_change_flow(store, projection):
    flow, clock, _, remote, sync = projection
    remote.failure = "offline"
    await sync.sweep()
    before = state(store)
    assert before.next_at >= clock.now + timedelta(seconds=600)
    assert not before.blocked
    # Local completion is independent and is discovered despite the remote backoff.
    flow.state = FlowState.SUCCEEDED
    store.update_flow(flow)
    await sync.sweep()
    assert state(store).desired_status is ProjectStatus.DONE
    assert state(store).next_at == before.next_at
    assert remote.calls == ["validate"] and store.get_flow(flow.id).state is FlowState.SUCCEEDED


@pytest.mark.parametrize("status", [429, 403])
async def test_definite_add_rejection_can_retry_after_restart(store, projection, tmp_path, monkeypatch, status):
    import httpx
    from githubkit import GitHub as SDK

    from app.github.client import GitHubApp
    from app.github.projects import GitHubProjects
    from tests.test_github_client import settings
    from tests.test_github_projects import item

    flow, clock, configs, remote, sync = projection
    calls = []

    def rejected(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, json={"message": "rejected"}, headers={"Retry-After": "600"}
                                  if status == 429 else {})
        return httpx.Response(200, json={"data": {"addProjectV2ItemById": {
            "item": item(f"ISSUE-{flow.issue_number}"),
        }}})

    app = GitHubApp(settings(tmp_path))
    monkeypatch.setattr(app, "_installation_client", lambda _, **options: SDK(
        "test-token", async_transport=httpx.MockTransport(rejected), **options,
    ))

    async def add(content):
        remote.item = await GitHubProjects(app, config()).add_issue(content)
        return remote.item.model_copy()

    monkeypatch.setattr(remote, "add_issue", add)
    await sync.sweep()
    pending = state(store)
    assert not pending.add_started and pending.item_id is None and len(calls) == 1
    assert pending.blocked is (status == 403)
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        resumed = ProjectSync(restarted, configs, lambda _: remote)
        await resumed.sweep()
        assert len(calls) == 1
        if status == 403:
            ProjectStore(restarted).retry(flow.repo)
        else:
            assert pending.next_at >= clock.now + timedelta(seconds=600)
            clock.now = pending.next_at
        await resumed.sweep()
        assert len(calls) == 2 and state(restarted).item_id == "ITEM"
        assert state(restarted).synced_version == state(restarted).desired_version
    finally:
        restarted.close()


async def test_disable_mid_request_stops_following_requests(store, projection):
    flow, _, configs, remote, sync = projection
    remote.item = ProjectItem(id="ITEM", content_id=f"ISSUE-{flow.issue_number}", archived=False)

    async def disable():
        configs.clear()

    remote.after_read = disable
    await sync.sweep()
    before = list(remote.calls)
    await sync.sweep()
    assert remote.calls == before and not any(isinstance(call, tuple) and call[0] == "set" for call in before)


async def test_two_sync_instances_serialize_the_same_item(store, projection):
    flow, _, configs, remote, first = projection
    entered, release = asyncio.Event(), asyncio.Event()
    remote.item = ProjectItem(id="ITEM", content_id=f"ISSUE-{flow.issue_number}", archived=False)

    async def wait():
        entered.set()
        await release.wait()

    remote.after_read = wait
    task = asyncio.create_task(first.sweep())
    await asyncio.wait_for(entered.wait(), 2)
    try:
        await asyncio.wait_for(ProjectSync(store, configs, lambda _: remote).sweep(), 2)
        assert remote.calls.count("read") == 1
    finally:
        release.set()
        await task


async def test_activation_skips_history_but_discovers_completion_before_seed(store, projection):
    flow, clock, _, _, sync = projection
    history = store.create_flow(flow.repo, 99, 42)
    history.state = FlowState.SUCCEEDED
    store.update_flow(history)
    clock.now += timedelta(seconds=1)
    await sync.sweep()
    assert [row.issue_number for row in ProjectStore(store).list_projections()] == [flow.issue_number]
    recent = store.create_flow(flow.repo, 100, 42)
    recent.state = FlowState.SUCCEEDED
    store.update_flow(recent)
    await sync.sweep()
    assert {row.issue_number for row in ProjectStore(store).list_projections()} == {flow.issue_number, 100}


async def test_project_error_does_not_block_control_cancellation_or_worker_release(store, projection):
    flow, _, _, remote, sync = projection
    remote.failure = "offline"
    await sync.sweep()
    ControlService(store).cancel(flow)
    assert store.get_flow(flow.id).state is FlowState.CANCELLED
    assert state(store).failures == 1


async def test_explicit_job_failure_blocks_but_pending_retry_does_not(store, projection):
    flow, clock, _, _, sync = projection
    job = store.enqueue_job(flow.id, JobKind.REVISION)
    await converge(sync)
    assert state(store).desired_status is ProjectStatus.IN_PROGRESS
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET state=%s WHERE id=%s", (JobState.FAILED, job.id))
    clock.now += timedelta(seconds=31)
    await sync.sweep()
    assert state(store).desired_status is ProjectStatus.BLOCKED


async def test_archive_ack_and_worker_release_succeed_while_projects_are_offline(store):
    from app.contracts.protocol import ArchiveResult
    from tests.test_archive_recovery import ready_archive, receive

    flow, job, task, subject = await ready_archive(store)
    remote = Remote()
    remote.failure = "offline"
    sync = ProjectSync(store, {flow.repo: config()}, lambda _: remote)
    await sync.sweep()
    assert state(store).failures == 1
    assert receive(store, job, ArchiveResult(**task.model_dump(), cleanup="removed")) == "pending"
    assert await subject.results.apply(job.id) is JobState.DONE
    assert store.get_flow_execution(flow.id).released_at is not None
    assert store.get_job(job.id).result_applied_at is not None
    await sync.sweep()
    assert state(store).desired_status is ProjectStatus.DONE and remote.calls == ["validate"]


async def test_config_change_revalidates_and_reuses_same_item(store, projection):
    flow, _, configs, remote, sync = projection
    await converge(sync)
    old_version = state(store).desired_version
    options = dict(config().options) | {ProjectStatus.IN_REVIEW: "renamed-option"}
    configs[flow.repo] = config().model_copy(update={"options": options})
    remote.options = options
    await sync.sweep()
    assert state(store).desired_version == old_version + 1
    assert state(store).synced_version == old_version + 1
    assert remote.item.option_id == "renamed-option"
    assert remote.calls.count("add") == 1


async def test_installation_mismatch_blocks_before_any_remote_request(store, projection):
    flow, _, _, remote, sync = projection
    flow.installation_id = 999
    store.update_flow(flow)
    await sync.sweep()
    assert state(store).blocked and remote.calls == []


async def test_failed_projection_transaction_rolls_back_version(store, projection):
    flow, _, _, _, sync = projection
    await sync.sweep()
    before = state(store)
    flow.state = FlowState.SUCCEEDED
    store.update_flow(flow)
    with pytest.raises(RuntimeError), store.transaction() as tx:
        ProjectStore(tx).refresh(before.id, config())
        raise RuntimeError("interrupted")
    assert state(store).desired_version == before.desired_version


async def test_slow_item_does_not_block_another_issue(store, projection):
    flow, _, configs, remote, _ = projection
    other = store.create_flow(flow.repo, 8, 42)
    entered, release, progressed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    slow = Remote()

    async def issue(repo, number):
        if number == flow.issue_number:
            entered.set()
            await release.wait()
        else:
            progressed.set()
        return f"ISSUE-{number}"

    # Separate API instances represent separate items; delay only the first identity lookup.
    remotes = iter([slow, remote])
    slow.issue_id = remote.issue_id = issue
    sync = ProjectSync(store, configs, lambda _: next(remotes), concurrency=2)
    task = asyncio.create_task(sync.sweep())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(progressed.wait(), 2)
        assert store.get_flow(other.id).state is FlowState.ACTIVE
    finally:
        release.set()
        await task


async def test_unknown_archive_executor_is_an_explicit_block(store, projection):
    flow, clock, _, _, sync = projection
    job = store.enqueue_job(flow.id, JobKind.ARCHIVE)
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET recovery_deadline=%s WHERE id=%s", (clock.now, job.id))
    await sync.sweep()
    assert state(store).desired_status is ProjectStatus.BLOCKED


async def test_received_archive_result_is_in_progress_despite_old_recovery_deadline(store, monkeypatch):
    from app.contracts.protocol import ArchiveResult
    from app.contracts.result_models import ResultPending
    from tests.test_archive_recovery import ready_archive, receive

    flow, job, task, subject = await ready_archive(store)
    # The retained deadline describes a past disconnect; a persisted receipt now proves the outcome.
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET recovery_deadline=%s WHERE id=%s", (store.now(), job.id))
    assert receive(store, job, ArchiveResult(**task.model_dump(), cleanup="removed")) == "pending"

    async def delayed(plan):
        raise ResultPending("notification backoff")

    monkeypatch.setattr(subject.results.effects, "run", delayed)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    remote = Remote()
    sync = ProjectSync(store, {flow.repo: config()}, lambda _: remote)
    await sync.sweep()
    assert state(store).desired_status is ProjectStatus.IN_PROGRESS
    assert store.get_job(job.id).result_applied_at is None


async def test_case_normalized_repo_is_enrolled_and_synced(store, projection):
    _, _, configs, remote, sync = projection
    store.create_flow("OWNER/REPO", 100, 42)
    await sync.sweep()
    rows = ProjectStore(store).list_projections()
    assert all(row.repo in configs for row in rows)
    assert {row.issue_number for row in rows} == {7, 100}
    assert remote.calls.count("validate") == 2


async def test_cli_retry_preserves_known_card_and_status_is_read_only(store, projection, monkeypatch):
    from contextlib import contextmanager

    from typer.testing import CliRunner

    from app import cli

    flow, _, _, remote, sync = projection
    await converge(sync)
    before = len(remote.calls)
    row = state(store)
    ProjectStore(store).save(row, blocked=True, last_error="operator inspection")

    @contextmanager
    def connection():
        yield store

    monkeypatch.setattr(cli, "_store", connection)
    result = CliRunner().invoke(cli.app, ["project", "status", flow.repo])
    assert result.exit_code == 0 and "operator inspection" in result.stdout
    result = CliRunner().invoke(cli.app, ["project", "retry", flow.repo])
    assert result.exit_code == 0 and "1 project projections" in result.stdout
    assert not state(store).blocked and state(store).item_id == row.item_id
    assert len(remote.calls) == before
