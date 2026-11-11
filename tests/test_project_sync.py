"""PostgreSQL projections recover independently from worker/Flow transactions."""

import asyncio
import json
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import httpx
import pytest
from githubkit import GitHub as SDK
from pydantic import BaseModel, ConfigDict, PrivateAttr
from typer.testing import CliRunner

from app import cli
from app.contracts.protocol import ExecutionStopped
from app.contracts.result_models import ResultPending
from app.controler.reconciliation.projects import ProjectSync
from app.controler.workflow.service import ControlService
from app.domain.models import ArchiveReason, DeliveryState, FlowState, JobKind, JobState
from app.domain.project_models import ProjectStatus
from app.github import (
    GitHubApp,
    GitHubError,
    GitHubErrorKind,
    Installation,
    Issue,
    Project,
    ProjectItem,
    ProjectItemsPage,
    ProjectTarget,
    Repository,
)
from app.persistence.activity import ActivityStore
from app.persistence.store import Store
from tests.github import RecordingGitHub
from tests.test_coordinator import coordinator
from tests.test_github_client import settings
from tests.test_github_projects import config, item, page
from tests.test_merge import route_merge
from tests.test_review_batches import review_flow as review_flow  # noqa: PLC0414
from tests.test_workflow_transactions import accepted_coding
from tests.workspace_fixtures import register_worker


class RemoteProject(Project):
    # Mutable remote state belongs to this scoped backend, not resource facts.
    model_config = ConfigDict(extra="allow", frozen=False)
    _remote_item: ProjectItem | None = PrivateAttr(default=None)
    _calls: list[Any] = PrivateAttr(default_factory=list)
    _failure: str | None = PrivateAttr(default=None)
    _after_read: Any = PrivateAttr(default=None)
    _after_set: Any = PrivateAttr(default=None)
    _pages: Any = PrivateAttr(default=None)

    def __init__(self):
        target = config()
        super().__init__(Installation.model_construct(installation_id=target.installation_id), target)

    @property
    def item(self):
        return self._remote_item

    @item.setter
    def item(self, value):
        self._remote_item = value

    @property
    def calls(self):
        return self._calls

    @property
    def failure(self):
        return self._failure

    @failure.setter
    def failure(self, value):
        self._failure = value

    @property
    def after_read(self):
        return self._after_read

    @after_read.setter
    def after_read(self, value):
        self._after_read = value

    @property
    def after_set(self):
        return self._after_set

    @after_set.setter
    def after_set(self, value):
        self._after_set = value

    @property
    def pages(self):
        return self._pages

    @pages.setter
    def pages(self, value):
        self._pages = value

    async def validate(self, repo):
        self.calls.append("validate")
        if self.failure == "offline":
            raise GitHubError(GitHubErrorKind.RATE_LIMITED, "projects", retry_after="600")
        return ProjectTarget(title="board", status_name="Status")

    async def fetch_content_id(self, repo, number):
        self.calls.append("issue")
        return f"ISSUE-{number}"

    async def list_items_page(self, cursor):
        self.calls.append(("page", cursor))
        if self.pages:
            return self.pages[cursor]
        return ProjectItemsPage(items=[self.item] if self.item else [], next_cursor=None, complete=True)

    async def fetch_item(self, item_id):
        self.calls.append("read")
        value = self.item.model_copy() if self.item else None
        if self.after_read:
            await self.after_read()
        if value is None:
            return None
        facts = value.model_dump(exclude={"id"})
        facts.setdefault("option_id", None)
        return RemoteProjectItem(self, value.id, **facts)

    async def add_issue(self, content_id):
        self.calls.append("add")
        self.item = ProjectItem(id="ITEM", content_id=content_id, archived=False, option_id=None)
        if self.failure == "add":
            raise GitHubError(GitHubErrorKind.TRANSIENT, "add")
        return RemoteProjectItem(self, self.item.id, content_id=content_id, archived=False)

    async def set_status(self, item_id, status):
        self.calls.append(("set", status))
        self.item = self.item.model_copy(update={"option_id": self.options[status]})
        if self.after_set:
            await self.after_set()
        if self.failure == "set":
            raise GitHubError(GitHubErrorKind.TRANSIENT, "set")


class RemoteProjectItem(ProjectItem):
    async def set_status(self, status):
        await self.project.set_status(self.id, status)
        return self.model_copy(update={"option_id": self.project.options[status]})


class RemoteInstallation(Installation):
    """Only the concrete binders exercised by projection recovery tests."""

    _remote: Any = PrivateAttr()

    def __init__(self, remote, installation_id):
        BaseModel.__init__(self, installation_id=installation_id)
        self._remote = remote

    @property
    def remote(self):
        return self._remote

    def repository(self, full_name):
        return Repository(self, full_name)

    def project(self, config):
        assert config.installation_id == self.installation_id
        return self.remote


class RemoteApp:
    def __init__(self, remote):
        self.remote = remote

    def installation(self, installation_id):
        remote = self.remote.pop(0) if isinstance(self.remote, list) else self.remote
        return RemoteInstallation(remote, installation_id)


@pytest.fixture
def remote_app(monkeypatch):
    async def content_id(issue):
        return await issue.repository.installation.remote.fetch_content_id(
            issue.repository.full_name, issue.number,
        )

    monkeypatch.setattr(Issue, "fetch_content_id", content_id)
    return RemoteApp


@pytest.fixture
def projection(store, review_flow, remote_app):
    flow, clock = review_flow
    # The shared fixture creates delivery facts; this suite explicitly owns its Flow phase.
    flow.state = FlowState.REVIEW
    store.flows.update_flow(flow)
    configs = {flow.repo: config()}
    remote = RemoteProject()
    sync = ProjectSync(
        configs,
        remote_app(remote),
        interval=30,
        batch_size=8,
        concurrency=2,
        database=store.database,
    )
    return flow, clock, configs, remote, sync


def state(store):
    return store.projects.list_projections()[0]


async def converge(sync, times=3):
    for _ in range(times):
        await sync.sweep()


@pytest.mark.parametrize(
    "flow_state,delivery,reason,expected",
    [
        (FlowState.QUEUED, DeliveryState.CLOSED, None, ProjectStatus.TODO),
        (FlowState.RUNNING, DeliveryState.MERGED, None, ProjectStatus.IN_PROGRESS),
        (FlowState.REVIEW, DeliveryState.OPEN, None, ProjectStatus.IN_REVIEW),
        (FlowState.REVIEW, DeliveryState.CLOSED, None, ProjectStatus.IN_REVIEW),
        (FlowState.REVIEW, DeliveryState.MERGED, None, ProjectStatus.IN_REVIEW),
        (FlowState.BLOCKED, DeliveryState.OPEN, None, ProjectStatus.BLOCKED),
        (FlowState.BLOCKED, DeliveryState.MERGED, ArchiveReason.SUCCEEDED, ProjectStatus.BLOCKED),
        (
            FlowState.FINALIZING,
            DeliveryState.MERGED,
            ArchiveReason.SUCCEEDED,
            ProjectStatus.IN_PROGRESS,
        ),
        (
            FlowState.FINALIZING,
            DeliveryState.CLOSED,
            ArchiveReason.ABANDONED,
            ProjectStatus.IN_PROGRESS,
        ),
        (FlowState.ARCHIVED, DeliveryState.NONE, ArchiveReason.SUCCEEDED, ProjectStatus.DONE),
        (FlowState.ARCHIVED, DeliveryState.CLOSED, ArchiveReason.ABANDONED, ProjectStatus.DONE),
    ],
)
async def test_projects_maps_flow_phase_without_inference_from_delivery(
    store,
    projection,
    flow_state,
    delivery,
    reason,
    expected,
):
    flow, clock, _, remote, sync = projection
    await sync.sweep()
    current = store.deliveries.get_delivery(flow.id)
    current.state = delivery
    if delivery is DeliveryState.NONE:
        current.pr_number = None
    store.deliveries.update_delivery(current)
    if flow_state is FlowState.ARCHIVED:
        ControlService(database=store.database).finish(
            flow,
            reason=reason,
            summary="本轮已完成",
            expected_round_no=1,
        )
    else:
        flow.state, flow.archive_reason = flow_state, reason
        store.flows.update_flow(flow)
    clock.now += timedelta(seconds=31)
    await converge(sync)
    assert store.flows.get_flow(flow.id).state is flow_state
    assert state(store).desired_status is expected
    assert remote.item.option_id == config().options[expected]


async def test_unbound_initial_is_todo_then_bound_work_is_in_progress(store, projection):
    flow, clock, _, _, sync = projection
    flow.state = FlowState.QUEUED
    store.flows.update_flow(flow)
    delivery = store.deliveries.get_delivery(flow.id)
    delivery.state, delivery.pr_number = DeliveryState.NONE, None
    store.deliveries.update_delivery(delivery)
    store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    await converge(sync)
    assert state(store).desired_status is ProjectStatus.TODO
    register_worker(store, "project-worker", repo=flow.repo, epoch="epoch")
    assert store.workers.assign_job("project-worker", "token", "epoch") is not None
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
        await converge(
            ProjectSync(
                configs,
                sync.github,
                interval=30,
                database=reopened.database,
            )
        )
        assert state(reopened).synced_version == state(reopened).desired_version
        assert remote.calls.count("add") == 1
        assert remote.calls.count(("set", ProjectStatus.IN_REVIEW)) == 1
    finally:
        reopened.close()


async def test_same_issue_new_flow_takes_over_existing_card(store, projection):
    flow, clock, _, remote, sync = projection
    await converge(sync)
    ControlService(database=store.database).cancel(flow)
    fresh = store.flows.create_flow(flow.repo, flow.issue_number, 42)
    clock.now += timedelta(seconds=31)
    await converge(sync)
    assert len(store.projects.list_projections()) == 1
    assert state(store).flow_id == fresh.id and state(store).desired_status is ProjectStatus.TODO
    assert remote.calls.count("add") == 1


async def test_resuming_older_flow_selects_owner_even_after_both_flows_are_archived(
    store, projection
):
    # Max Flow ID loses ownership when an operator explicitly resumes an older Flow.
    flow, clock, _, remote, sync = projection
    service = ControlService(database=store.database)
    await converge(sync)
    service.cancel(flow)
    newer = service.on_trigger(flow.repo, flow.issue_number, 42)
    assert newer is not None and newer.id > flow.id
    service.cancel(newer)
    clock.now += timedelta(seconds=31)
    await converge(sync)
    assert state(store).flow_id == newer.id
    activity = ActivityStore(store.database)
    first_archive = activity.archived(flow.id, 1)
    assert first_archive is not None
    resumed = service.resume(store.flows.get_flow(flow.id), expected_round_no=1)
    await converge(sync)
    assert state(store).flow_id == flow.id and state(store).desired_status is ProjectStatus.TODO
    service.cancel(resumed)
    await converge(sync)
    assert state(store).flow_id == flow.id and state(store).desired_status is ProjectStatus.DONE
    assert activity.archived(flow.id, 1) == first_archive
    assert activity.archived(flow.id, 2) is not None
    assert remote.calls.count("add") == 1 and remote.item.option_id == "done"


async def test_new_desired_version_during_set_is_not_acknowledged_by_old_response(store, projection):
    flow, _, _, remote, sync = projection

    async def change():
        ControlService(
            database=store.database,
        ).cancel(flow)
        store.projects.refresh(state(store).id, config())

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
        ControlService(
            database=store.database,
        ).cancel(flow)

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
        remote.item = remote.item.model_copy(update={"archived": True})
    clock.now += timedelta(seconds=31)
    await sync.sweep()
    assert state(store).blocked
    store.projects.retry(flow.repo)
    await sync.sweep()
    assert state(store).blocked and remote.calls.count("add") == 1


async def test_discovered_archived_card_recovers_after_restore_and_restart(store, projection, tmp_path, monkeypatch):
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
    monkeypatch.setattr(Installation, "_client", lambda self, **options: SDK(
        "test-token", async_transport=httpx.MockTransport(transport), **options,
    ))
    monkeypatch.setattr(remote, "list_items_page", app.installation(42).project(config()).list_items_page)
    monkeypatch.setattr(remote, "add_issue", unexpected_add)
    await sync.sweep()
    assert state(store).item_id == "ITEM" and state(store).blocked
    assert not state(store).add_started
    # Restoring the card is a human action; retry resumes the saved item without another add.
    remote.item = remote.item.model_copy(update={"archived": False})
    reopened = Store(store.database_url, clock=lambda: clock.now)
    try:
        reopened.projects.retry(flow.repo)
        await ProjectSync(
            configs, sync.github, database=reopened.database,
        ).sweep()
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
    remote.pages = {None: ProjectItemsPage(
        items=[ProjectItem(id="HIDDEN", content_id=None, archived=False)], next_cursor=None, complete=False,
    )}
    await sync.sweep()
    assert state(store).blocked and "add" not in remote.calls
    remote.pages = None
    store.projects.retry(flow.repo)
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
    # User cancellation changes the desired status without bypassing the remote deadline.
    ControlService(
        database=store.database,
    ).cancel(flow)
    await sync.sweep()
    assert state(store).desired_status is ProjectStatus.DONE
    assert state(store).next_at == before.next_at
    assert (
        remote.calls == ["validate"] and store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    )


@pytest.mark.parametrize("status", [429, 422])
async def test_definite_add_rejection_can_retry_after_restart(store, projection, tmp_path, monkeypatch, status):
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
    monkeypatch.setattr(Installation, "_client", lambda self, **options: SDK(
        "test-token", async_transport=httpx.MockTransport(rejected), **options,
    ))

    async def add(content):
        receipt = await app.installation(42).project(config()).add_issue(content)
        remote.item = ProjectItem.model_validate(receipt.model_dump())
        return receipt

    monkeypatch.setattr(remote, "add_issue", add)
    await sync.sweep()
    pending = state(store)
    assert not pending.add_started and pending.item_id is None and len(calls) == 1
    assert pending.blocked is (status != 429)
    restarted = Store(store.database_url, clock=lambda: clock.now)
    try:
        resumed = ProjectSync(
            configs, sync.github, database=restarted.database,
        )
        await resumed.sweep()
        assert len(calls) == 1
        if status != 429:
            restarted.projects.retry(flow.repo)
        else:
            assert pending.next_at >= clock.now + timedelta(seconds=600)
            clock.now = pending.next_at
        await resumed.sweep()
        assert len(calls) == 2 and state(restarted).item_id == "ITEM"
        assert state(restarted).synced_version == state(restarted).desired_version
    finally:
        restarted.close()


async def test_activation_skips_history_but_discovers_completion_before_seed(store, projection):
    flow, clock, _, _, sync = projection
    history = store.flows.create_flow(flow.repo, 99, 42)
    ControlService(
        database=store.database,
    ).cancel(history)
    clock.now += timedelta(seconds=1)
    await sync.sweep()
    assert [row.issue_number for row in store.projects.list_projections()] == [flow.issue_number]
    recent = store.flows.create_flow(flow.repo, 100, 42)
    ControlService(
        database=store.database,
    ).cancel(recent)
    await sync.sweep()
    assert {row.issue_number for row in store.projects.list_projections()} == {flow.issue_number, 100}


async def test_project_error_does_not_block_control_cancellation_or_worker_release(store, projection):
    flow, _, _, remote, sync = projection
    remote.failure = "offline"
    await sync.sweep()
    ControlService(
        database=store.database,
    ).cancel(flow)
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert state(store).failures == 1


async def test_archive_ack_and_worker_release_succeed_while_projects_are_offline(
    store,
    remote_app,
    recording_resources,
):
    execution, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(execution.flow_id)
    store.projects.seed(flow.repo, config(), 100)
    await route_merge(store, flow)
    job = store.jobs.list_jobs(flow.id)[-1]
    subject = coordinator(store, RecordingGitHub())
    remote = RemoteProject()
    remote.failure = "offline"
    sync = ProjectSync(
        {flow.repo: config()},
        remote_app(remote),
        database=store.database,
    )
    await sync.sweep()
    assert state(store).failures == 1
    stop = store.stops.pending("worker-a")
    assert store.stops.confirm(
        "worker-a", ExecutionStopped(**stop.fence().model_dump(), execution_nonce="owned"), "epoch-a",
    )
    assert await subject.results.apply(job.id) is JobState.DONE
    assert store.workers.get_flow_execution(flow.id).released_at is not None
    assert store.jobs.get_job(job.id).result_applied_at is not None
    archived = store.flows.get_flow(flow.id)
    assert (
        archived.state is FlowState.ARCHIVED and archived.archive_reason is ArchiveReason.SUCCEEDED
    )
    archive = ActivityStore(store.database).archived(flow.id, 1)
    assert archive is not None and archive.occurred_at == archived.archived_at is not None
    await sync.sweep()
    assert state(store).desired_status is ProjectStatus.DONE and remote.calls == ["validate"]


async def test_config_change_revalidates_and_reuses_same_item(store, projection):
    flow, _, _, remote, sync = projection
    await converge(sync)
    old_version = state(store).desired_version
    options = dict(config().options) | {ProjectStatus.IN_REVIEW: "renamed-option"}
    remote.options = options
    restarted = ProjectSync(
        {flow.repo: config().model_copy(update={"options": options})},
        sync.github,
        database=store.database,
    )
    await restarted.sweep()
    assert state(store).desired_version == old_version + 1
    assert state(store).synced_version == old_version + 1
    assert remote.item.option_id == "renamed-option"
    assert remote.calls.count("add") == 1


async def test_installation_mismatch_blocks_before_any_remote_request(store, projection):
    flow, _, _, remote, sync = projection
    flow.installation_id = 999
    store.flows.update_flow(flow)
    await sync.sweep()
    assert state(store).blocked and remote.calls == []


async def test_failed_projection_transaction_rolls_back_version(store, projection):
    flow, _, _, _, sync = projection
    await sync.sweep()
    before = state(store)
    ControlService(
        database=store.database,
    ).cancel(flow)
    with pytest.raises(RuntimeError), store.transaction() as tx:
        tx.projects.refresh(before.id, config())
        raise RuntimeError("interrupted")
    assert state(store).desired_version == before.desired_version


async def test_slow_item_does_not_block_another_issue(store, projection, remote_app):
    flow, _, configs, remote, _ = projection
    other = store.flows.create_flow(flow.repo, 8, 42)
    entered, release, progressed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    slow = RemoteProject()

    async def issue(repo, number):
        if number == flow.issue_number:
            entered.set()
            await release.wait()
        else:
            progressed.set()
        return f"ISSUE-{number}"

    # Separate project bindings represent separate items; delay only the first identity lookup.
    slow.fetch_content_id = remote.fetch_content_id = issue
    sync = ProjectSync(
        configs,
        remote_app([slow, remote]),
        concurrency=2,
        database=store.database,
    )
    task = asyncio.create_task(sync.sweep())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(progressed.wait(), 2)
        assert store.flows.get_flow(other.id).state is FlowState.QUEUED
    finally:
        release.set()
        await task


async def test_merged_executor_recovery_preserves_done_projection(
    store,
    monkeypatch,
    remote_app,
    recording_resources,
):
    job, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(job.flow_id)
    store.projects.seed(flow.repo, config(), 100)
    await route_merge(store, flow)
    store.workers.worker_disconnected("worker-a", connection_epoch="epoch-a")
    sync = ProjectSync(
        {flow.repo: config()},
        remote_app(RemoteProject()),
        database=store.database,
    )
    await sync.sweep()
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert state(store).desired_status is ProjectStatus.DONE
    # Executor recovery cannot replace the committed business conclusion.
    later = store.now() + timedelta(hours=1)
    monkeypatch.setattr(store._db, "_clock", lambda: later)
    store.workers.sweep_expired_assignments()
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    await sync.sweep()
    assert state(store).desired_status is ProjectStatus.DONE


async def test_pending_archive_notice_is_done_despite_old_recovery_deadline(
    store, monkeypatch, remote_app, recording_resources,
):
    execution, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(execution.flow_id)
    store.projects.seed(flow.repo, config(), 100)
    await route_merge(store, flow)
    job = store.jobs.list_jobs(flow.id)[-1]
    subject = coordinator(store, RecordingGitHub())
    # The retained deadline cannot change archival while its local notice is pending.
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET recovery_deadline=%s WHERE id=%s", (store.now(), job.id))

    async def delayed(plan):
        raise ResultPending("notification backoff")

    monkeypatch.setattr(subject.results.effects, "run", delayed)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    remote = RemoteProject()
    sync = ProjectSync(
        {flow.repo: config()}, remote_app(remote), database=store.database,
    )
    await sync.sweep()
    assert state(store).desired_status is ProjectStatus.DONE
    assert store.jobs.get_job(job.id).result_applied_at is None


async def test_case_normalized_repo_is_enrolled_and_synced(store, projection):
    _, _, configs, remote, sync = projection
    store.flows.create_flow("OWNER/REPO", 100, 42)
    await sync.sweep()
    rows = store.projects.list_projections()
    assert all(row.repo in configs for row in rows)
    assert {row.issue_number for row in rows} == {7, 100}
    assert remote.calls.count("validate") == 2


async def test_cli_retry_preserves_known_card_and_status_is_read_only(store, projection, monkeypatch):
    flow, _, _, remote, sync = projection
    await converge(sync)
    before = len(remote.calls)
    row = state(store)
    store.projects.save(row, blocked=True, last_error="operator inspection")

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
