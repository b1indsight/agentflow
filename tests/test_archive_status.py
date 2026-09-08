"""Archive notifications retain cleanup receipts until every GitHub effect is durable."""

import pytest

from app.control.archive import archive_progress
from app.control.effects import ResultEffects
from app.control.service import ControlService
from app.github_models import GitHubAdapterError, GitHubErrorKind
from app.models import FlowState, JobState
from app.plugins.hookspecs import hookimpl
from app.protocol import ArchiveResult
from app.result_models import CommentEffect, EffectState, ResultPending, ResultPlan
from app.result_store import ResultStore
from app.store import Store
from tests.test_archive_recovery import ready_archive, receive
from tests.test_coordinator import RecordingGitHub, coordinator
from tests.test_merge import route_merge


class ArchiveGitHub(RecordingGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.targets: dict[int, set[str]] = {}
        self.writes: list[tuple[str, int]] = []
        self.notifications: list[tuple[int, str]] = []

    async def get_labels(self, installation_id, repo, number):
        return list(self.targets.setdefault(number, {"flow", "Flow:In-Review", "user-label"}))

    async def add_label(self, installation_id, repo, number, label):
        self.writes.append(("add", number))
        self.targets[number].update({label, "concurrent-user-label"})

    async def remove_label(self, installation_id, repo, number, label):
        self.writes.append(("remove", number))
        self.targets[number] = {item for item in self.targets[number]
                                if item.casefold() != label.casefold()}

    async def set_labels(self, *args):
        raise AssertionError("archive must preserve unrelated labels")

    async def comment(self, installation_id, repo, number, body):
        self.writes.append(("comment", number))
        self.notifications.append((number, body))
        return await super().comment(installation_id, repo, number, body)


async def pending_archive(store):
    flow, job, task, _ = await ready_archive(store)
    github = ArchiveGitHub()
    subject = coordinator(store, github)
    result = ArchiveResult(**task.model_dump(), cleanup="removed")
    assert receive(store, job, result) == "pending"
    return flow, job, result, github, subject


def due(store, job_id):
    with store.connection() as connection:
        connection.execute("UPDATE result_effects SET next_attempt_at=%s WHERE job_id=%s",
                           (store.now(), job_id))


async def test_archive_publishes_stable_status_and_replay_does_not_backfill(store):
    flow, job, result, github, subject = await pending_archive(store)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert set(github.targets) == {flow.issue_number, 17}
    for labels in github.targets.values():
        assert labels == {"flow", "user-label", "concurrent-user-label", "flow:done"}
    assert {number for number, _ in github.notifications} == {flow.issue_number, 17}
    for _, body in github.notifications:
        assert f"Flow {flow.id}" in body
        assert f"#{flow.issue_number}" in body and "#17" in body
        assert "a" * 40 in body and "2026-09-08T08:00:00" in body
        assert "工作区清理已确认" in body
        assert "worker-a:" not in body and "archive-token" not in body
    plan = store.results.get_plan(job.id)
    assert plan.archive == result
    for effect in plan.effects:
        if isinstance(effect, CommentEffect):
            # A cleanup retry must not create a new notification identity.
            assert ResultEffects._comment_marker(plan, effect) == ResultEffects._comment_marker(
                plan.model_copy(update={"job_id": job.id + 100, "digest": "other"}), effect,
            )
    await route_merge(store, flow, "duplicate-merge")
    assert await subject.results.apply(job.id) is JobState.DONE
    assert len(github.notifications) == 2
    assert store.get_flow(flow.id).state is FlowState.SUCCEEDED
    assert store.get_flow_execution(flow.id).released_at is not None


async def test_permanent_archive_write_failure_retries_only_frozen_missing_effects(store, monkeypatch):
    flow, job, result, github, subject = await pending_archive(store)
    comment = github.comment

    async def forbidden(installation, repo, number, body):
        if number == 17:
            raise GitHubAdapterError(GitHubErrorKind.PERMISSION, "comment", status_code=403)
        return await comment(installation, repo, number, body)

    monkeypatch.setattr(github, "comment", forbidden)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    plan = store.results.get_plan(job.id)
    assert plan.archive == result
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    assert store.get_flow_execution(flow.id).released_at is None
    assert store.get_job(job.id).result_applied_at is None
    assert "comment" in store.get_job(job.id).last_error
    assert "GitHub" in archive_progress(store, flow, store.list_jobs(flow.id))
    assert [number for number, _ in github.notifications] == [flow.issue_number]
    jobs = store.list_jobs(flow.id)
    monkeypatch.setattr(github, "comment", comment)
    ControlService(store).retry(flow)
    assert len(store.list_jobs(flow.id)) == len(jobs)
    assert store.get_flow(flow.id).current_attempt_id == flow.current_attempt_id
    restarted = coordinator(store, github)
    restarted.results.settings.done_label = "new-config"
    assert await restarted.results.apply(job.id) is JobState.DONE
    assert store.results.get_plan(job.id) == plan
    assert len(github.notifications) == 2
    assert len(github.writes) == 6
    assert [item.id for item in store.list_jobs(flow.id)] == [item.id for item in jobs]


@pytest.mark.parametrize("operation", ["add_label", "remove_label", "comment"])
async def test_archive_recovers_remote_write_before_receipt_after_restart(store, monkeypatch, operation):
    flow, job, _, github, subject = await pending_archive(store)
    original = getattr(github, operation)

    async def lost_response(*args):
        await original(*args)
        raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, operation)

    monkeypatch.setattr(github, operation, lost_response)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    assert store.get_flow_execution(flow.id).released_at is None
    monkeypatch.setattr(github, operation, original)
    store.results.recover_claims()
    due(store, job.id)
    assert await coordinator(store, github).results.apply(job.id) is JobState.DONE
    assert len(github.writes) == 6
    assert len(github.notifications) == 2


async def test_unknown_archive_comment_stays_pending_without_resending_or_recleaning(store, monkeypatch):
    flow, job, result, github, subject = await pending_archive(store)

    async def unknown(*args):
        raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(github, "comment", unknown)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    due(store, job.id)
    with pytest.raises(ResultPending, match="outcome is unknown"):
        await coordinator(store, github).results.apply(job.id)
    with pytest.raises(ValueError, match="pending result"):
        ControlService(store).retry(flow)
    assert store.results.get_plan(job.id).archive == result
    assert store.get_flow(flow.id).state is FlowState.ACTIVE
    assert not github.notifications


@pytest.mark.parametrize("status,kind", [(429, GitHubErrorKind.RATE_LIMITED),
                                       (503, GitHubErrorKind.TRANSIENT)])
async def test_archive_transient_label_errors_resume_automatically(store, monkeypatch, status, kind):
    flow, job, _, github, subject = await pending_archive(store)
    original = github.add_label

    async def unavailable(*args):
        raise GitHubAdapterError(kind, "add_label", status_code=status)

    monkeypatch.setattr(github, "add_label", unavailable)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    record = store.results.list_effects(job.id)[0]
    assert record.state in {EffectState.RETRY, EffectState.UNCERTAIN}
    assert store.get_flow_execution(flow.id).released_at is None
    monkeypatch.setattr(github, "add_label", original)
    due(store, job.id)
    assert await subject.results.apply(job.id) is JobState.DONE


async def test_archive_fences_changed_delivery_before_github_mutation(store, monkeypatch):
    flow, job, _, github, subject = await pending_archive(store)
    original = github.get_labels

    async def changed(*args):
        labels = await original(*args)
        delivery = store.get_delivery(flow.id)
        delivery.version += 1
        store.update_delivery(delivery)
        return labels

    monkeypatch.setattr(github, "get_labels", changed)
    with pytest.raises(ResultPending, match="archive ownership changed"):
        await subject.results.apply(job.id)
    assert github.writes == []
    assert store.get_flow_execution(flow.id).released_at is None


async def test_existing_status_labels_need_no_mutation(store):
    flow, job, _, github, subject = await pending_archive(store)
    github.targets = {number: {"Flow:Done", "flow", "user-label"}
                      for number in (flow.issue_number, 17)}
    assert await subject.results.apply(job.id) is JobState.DONE
    assert github.writes == [("comment", flow.issue_number), ("comment", 17)]


async def test_restart_recovers_comment_before_receipt_and_isolates_plugin_failure(store, monkeypatch):
    flow, job, _, github, subject = await pending_archive(store)
    finish = ResultStore.finish

    def interrupted(self, record, state, **kwargs):
        # A process can die after GitHub returns but before the receipt commits.
        if isinstance(record.payload, CommentEffect):
            raise RuntimeError("serve interrupted before receipt")  # noqa: TRY004
        return finish(self, record, state, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ResultStore, "finish", interrupted)
        with pytest.raises(RuntimeError, match="serve interrupted"):
            await subject.results.apply(job.id)
    assert len(github.notifications) == 1

    class BrokenPlugin:
        @hookimpl
        def on_archived(self, flow):
            raise RuntimeError("optional plugin failed")

    # Use a new connection pool, then replay the same inbox just as serve startup does.
    reopened = Store(store.database_url)
    try:
        reopened.results.recover_claims()
        restarted = coordinator(reopened, github)
        restarted.results.plugins.register(BrokenPlugin())
        assert await restarted.results.apply(job.id) is JobState.DONE
    finally:
        reopened.close()
    assert len(github.notifications) == 2
    assert store.get_job(job.id).result_applied_at is not None
    assert store.get_flow(flow.id).state is FlowState.SUCCEEDED
    assert store.get_flow_execution(flow.id).released_at is not None


async def test_frozen_pre_upgrade_archive_does_not_backfill_notifications(store):
    flow, job, result, github, subject = await pending_archive(store)
    # F2 froze cleanup-only plans. An upgrade must not add effects to these or completed Flows.
    plan = ResultPlan(job_id=job.id, flow_id=flow.id, digest=store.get_job(job.id).result_digest,
                      attempt_id=flow.current_attempt_id, state=JobState.DONE,
                      flow_state=FlowState.SUCCEEDED, archive=result)
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.results.prepare(plan)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert await coordinator(store, github).results.apply(job.id) is JobState.DONE
    assert not github.writes
