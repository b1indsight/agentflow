"""Merge notifications recover independently of Worker cleanup and business archival."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from githubkit import GitHub
from githubkit.cache.mem_cache import MemCacheStrategy
from psycopg.types.json import Jsonb

from app.contracts.protocol import ExecutionStopped
from app.contracts.receipts import MergeArchiveNotice, ResultInbox, merge_notice_digest
from app.contracts.result_models import CommentEffect, EffectState, LabelEffect, ResultPending
from app.controler.workflow.archive import archive_effects, archive_progress
from app.controler.workflow.service import ControlService
from app.domain.models import Flow, FlowDelivery, FlowState, JobState
from app.github import GitHubApp, GitHubError, GitHubErrorKind, Issue, PullRequest
from app.persistence.results import ResultStore
from app.persistence.store import Store
from app.plugins.hookspecs import hookimpl
from tests.github import (
    NATIVE_ISSUE_COMMENT,
    NATIVE_ISSUE_FIND_COMMENT,
    RecordingGitHub,
    record_call,
)
from tests.test_coordinator import coordinator
from tests.test_github_client import settings
from tests.test_merge import (
    merge_flow as merge_flow,  # noqa: PLC0414 - fixture re-export
)
from tests.test_merge import merge_payload, route_merge
from tests.test_workflow_transactions import accepted_coding


def test_direct_archive_notifies_only_real_pr_with_no_fake_issue_identity():
    # The same completion receipt must remain useful without an Issue target or fake Issue line.
    flow = Flow(id=1, repo="owner/repo", issue_number=None,
                task_content="Direct", installation_id=42,
                state=FlowState.FINALIZING)
    delivery = FlowDelivery(flow_id=flow.id, branch="flow/1", pr_number=17,
                            merge_commit_sha="a" * 40, merged_at=datetime(2026, 9, 8, tzinfo=UTC))
    effects = archive_effects(flow, delivery, done_label="flow:done", in_review_label="flow:in-review")
    assert [effect.number for effect in effects] == [17, 17, 17]
    assert [effect.key for effect in effects] == [
        "archive:17:17:done", "archive:17:17:in-review", "archive:17:17:comment",
    ]
    comment = next(effect for effect in effects if isinstance(effect, CommentEffect))
    assert "关联 Issue" not in comment.body and "None" not in comment.body
    assert "关联 PR：owner/repo#17" in comment.body and "a" * 40 in comment.body
    assert "本轮已归档" in comment.body and "工作区处置已确认" not in comment.body


class ArchiveGitHub(RecordingGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.targets: dict[int, set[str]] = {}
        self.writes: list[tuple[str, int]] = []
        self.notifications: list[tuple[int, str]] = []


@pytest.fixture
def archive_resources(recording_resources, monkeypatch):
    original_comment = Issue.comment

    async def fetch_labels(issue):
        state = record_call(issue.repository, "fetch_labels", number=issue.number)
        return list(state.targets.setdefault(issue.number, {"flow", "Flow:In-Review", "user-label"}))

    async def add_label(issue, label):
        state = record_call(issue.repository, "add_label", number=issue.number)
        state.writes.append(("add", issue.number))
        state.targets[issue.number].update({label, "concurrent-user-label"})

    async def remove_label(issue, label):
        state = record_call(issue.repository, "remove_label", number=issue.number)
        state.writes.append(("remove", issue.number))
        state.targets[issue.number] = {item for item in state.targets[issue.number]
                                     if item.casefold() != label.casefold()}

    async def comment(issue, body):
        state = issue.repository.installation.app
        state.writes.append(("comment", issue.number))
        state.notifications.append((issue.number, body))
        return await original_comment(issue, body)

    monkeypatch.setattr(Issue, "fetch_labels", fetch_labels)
    monkeypatch.setattr(Issue, "add_label", add_label)
    monkeypatch.setattr(Issue, "remove_label", remove_label)
    monkeypatch.setattr(Issue, "comment", comment)


async def pending_archive(store):
    execution, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(execution.flow_id)
    await route_merge(store, flow)
    flow = store.flows.get_flow(flow.id)
    job = store.jobs.list_jobs(flow.id)[-1]
    github = ArchiveGitHub()
    subject = coordinator(store, github)
    result = MergeArchiveNotice.model_validate(job.result_payload["terminal"])
    return flow, job, result, github, subject


def settle_cleanup(store):
    stop = store.stops.pending("worker-a")
    assert store.stops.confirm("worker-a", ExecutionStopped(
        **stop.fence().model_dump(), execution_nonce="original-coding-executor",
    ), "epoch-a")


def due(store, job_id):
    with store.connection() as connection:
        connection.execute("UPDATE result_effects SET next_attempt_at=%s WHERE job_id=%s",
                           (store.now(), job_id))


def archive_adapter(tmp_path, monkeypatch, transport, *, invalid_key=False):
    """Use production client construction and isolated credentials/cache, mocking only transport."""
    config = settings(tmp_path)
    if not invalid_key:
        config.github_private_key_path.write_bytes(
            rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    cache = MemCacheStrategy()

    def client(*args, **kwargs):
        return GitHub(*args, **kwargs, async_transport=httpx.MockTransport(transport),
                      cache_strategy=cache)

    monkeypatch.setattr("githubkit.GitHub", client)
    return GitHubApp(config), cache


async def test_merge_notice_survives_permission_failure_without_worker(
    store, merge_flow, monkeypatch, archive_resources,
):
    # GitHub permission failures must not reverse an already merged business result.
    observed = []

    class BrokenPlugin:
        @hookimpl
        def on_archived(self, flow):
            observed.append(store.flows.get_flow(flow.id).state)
            raise RuntimeError("optional plugin failed")

    service = ControlService(database=store.database)
    service.plugins.register(BrokenPlugin())
    service.on_merged(merge_flow, "merge", merge_payload(merge_flow))
    service.on_merged(merge_flow, "merge", merge_payload(merge_flow))
    assert observed == [FlowState.ARCHIVED]
    job = store.jobs.list_jobs(merge_flow.id)[-1]
    github = ArchiveGitHub()
    subject = coordinator(store, github)
    comment = Issue.comment

    async def forbidden(issue, body):
        if issue.number == 17:
            raise GitHubError(GitHubErrorKind.PERMISSION, "comment", status_code=403,
                              request_rejected=True)
        return await comment(issue, body)

    monkeypatch.setattr(Issue, "comment", forbidden)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    archived = store.flows.get_flow(merge_flow.id)
    assert archived.state is FlowState.ARCHIVED
    assert store.workers.get_flow_execution(archived.id) is None
    assert store.jobs.get_job(job.id).assigned_worker_id is None
    plan = store.results.get_plan(job.id)
    assert plan.archive is None and plan.archive_notice is not None
    assert [number for number, _ in github.notifications] == [archived.issue_number]
    assert "notification failed" in archive_progress(
        archived, store.jobs.list_jobs(archived.id), result_store=store.results, workers=store.workers,
    )
    monkeypatch.setattr(Issue, "comment", comment)
    subject.results.settings.done_label = "changed-after-freeze"
    assert ControlService(database=store.database).retry(archived)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert await coordinator(store, github).results.apply(job.id) is JobState.DONE
    assert store.results.get_plan(job.id) == plan
    assert len(github.notifications) == 2
    assert len(github.writes) == 6
    assert all("本轮已归档" in body and "工作区处置已确认" not in body
               for _, body in github.notifications)
    assert store.flows.get_flow(archived.id) == archived


async def test_merge_notice_restart_after_resume_only_observes_old_write(
    store, merge_flow, monkeypatch, archive_resources,
):
    await route_merge(store, merge_flow)
    job = store.jobs.list_jobs(merge_flow.id)[-1]
    github = ArchiveGitHub()
    subject = coordinator(store, github)
    finish = ResultStore.finish

    def interrupted(self, record, state, **kwargs):
        if isinstance(record.payload, CommentEffect):
            raise RuntimeError("receipt interrupted")  # noqa: TRY004 -- simulate a crash after POST
        return finish(self, record, state, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ResultStore, "finish", interrupted)
        with pytest.raises(RuntimeError, match="receipt interrupted"):
            await subject.results.apply(job.id)
    resumed = ControlService(database=store.database).resume(merge_flow, expected_round_no=1)
    before = list(github.writes)
    github.targets[merge_flow.issue_number] = {"Flow:In-Review", "user-label"}
    store.results.recover_claims()
    assert await coordinator(store, github).results.apply(job.id) is JobState.DONE
    assert github.writes == before
    assert github.targets[merge_flow.issue_number] == {"Flow:In-Review", "user-label"}
    assert len(github.notifications) == 1
    assert store.flows.get_flow(merge_flow.id) == resumed


async def test_old_merge_notice_cannot_change_new_issue_owner_labels(
    store, merge_flow, archive_resources,
):
    await route_merge(store, merge_flow)
    job = store.jobs.list_jobs(merge_flow.id)[-1]
    newer = ControlService(database=store.database).create_flow(merge_flow.repo, merge_flow.issue_number, 42)
    github = ArchiveGitHub()
    github.targets[merge_flow.issue_number] = {"Flow:In-Review", "user-label"}
    assert await coordinator(store, github).results.apply(job.id) is JobState.DONE
    assert github.targets[merge_flow.issue_number] == {"Flow:In-Review", "user-label"}
    assert "flow:done" in github.targets[17]
    assert store.flows.get_flow(newer.id) == newer


@pytest.mark.parametrize("changed", [{"pr_number": 99}, {"round_no": 2}])
async def test_notice_cannot_replace_immutable_archive_facts(
    store, merge_flow, changed, archive_resources,
):
    await route_merge(store, merge_flow)
    job = store.jobs.list_jobs(merge_flow.id)[-1]
    notice = MergeArchiveNotice.model_validate(job.result_payload["terminal"]).model_copy(update=changed)
    forged = ResultInbox(version=2, source="control", terminal=notice)
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET result_payload=%s,result_digest=%s WHERE id=%s",
                           (Jsonb(forged.model_dump(mode="json")), merge_notice_digest(notice), job.id))
    github = ArchiveGitHub()
    with pytest.raises(ValueError, match="archived round"):
        await coordinator(store, github).results.apply(job.id)
    assert not github.writes and not github.notifications
    assert store.jobs.get_job(job.id).result_applied_at is None
    assert store.flows.get_flow(merge_flow.id).state is FlowState.ARCHIVED


async def test_archive_publishes_stable_status_and_replay_does_not_backfill(store, archive_resources):
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
        assert "本轮已归档" in body and "工作区处置已确认" not in body
        assert "worker-a:" not in body and "archive-token" not in body
    plan = store.results.get_plan(job.id)
    assert plan.archive_notice == result and plan.archive is None
    await route_merge(store, flow, "duplicate-merge")
    assert await subject.results.apply(job.id) is JobState.DONE
    assert len(github.notifications) == 2
    settle_cleanup(store)
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.workers.get_flow_execution(flow.id).released_at is not None


async def test_permanent_archive_write_failure_retries_only_frozen_missing_effects(store, monkeypatch, archive_resources):
    flow, job, result, github, subject = await pending_archive(store)
    comment = Issue.comment

    async def forbidden(issue, body):
        if issue.number == 17:
            raise GitHubError(GitHubErrorKind.PERMISSION, "comment", status_code=403,
                              request_rejected=True)
        return await comment(issue, body)

    monkeypatch.setattr(Issue, "comment", forbidden)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    plan = store.results.get_plan(job.id)
    assert plan.archive_notice == result and plan.archive is None
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert store.jobs.get_job(job.id).result_applied_at is None
    assert "comment" in store.jobs.get_job(job.id).last_error
    assert "GitHub" in archive_progress(
        flow, store.jobs.list_jobs(flow.id), result_store=store.results, workers=store.workers
    )
    assert [number for number, _ in github.notifications] == [flow.issue_number]
    jobs = store.jobs.list_jobs(flow.id)
    monkeypatch.setattr(Issue, "comment", comment)
    ControlService(
        database=store.database,
    ).retry(flow)
    assert len(store.jobs.list_jobs(flow.id)) == len(jobs)
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.flows.get_flow(flow.id).current_attempt_id == flow.current_attempt_id
    restarted = coordinator(store, github)
    restarted.results.settings.done_label = "new-config"
    assert await restarted.results.apply(job.id) is JobState.DONE
    assert store.results.get_plan(job.id) == plan
    assert len(github.notifications) == 2
    assert len(github.writes) == 6
    assert [item.id for item in store.jobs.list_jobs(flow.id)] == [item.id for item in jobs]


@pytest.mark.parametrize("operation", ["add_label", "remove_label", "comment"])
async def test_archive_recovers_remote_write_before_receipt_after_restart(
    store, monkeypatch, operation, archive_resources
):
    flow, job, _, github, subject = await pending_archive(store)
    original = getattr(Issue, operation)

    async def lost_response(*args):
        await original(*args)
        raise GitHubError(GitHubErrorKind.TRANSIENT, operation)

    monkeypatch.setattr(Issue, operation, lost_response)
    if operation == "comment":
        with pytest.raises(ResultPending):
            await subject.results.apply(job.id)
        assert store.workers.get_flow_execution(flow.id).released_at is None
    else:
        assert await subject.results.apply(job.id) is JobState.DONE
        skipped = [r for r in store.results.list_effects(job.id) if r.state is EffectState.SKIPPED]
        assert len(skipped) == 2
        assert all(
            isinstance(r.payload, LabelEffect)
            and r.receipt is None
            and r.dispatched_at is not None
            and r.last_error
            for r in skipped
        )
        settle_cleanup(store)
        assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
        assert store.workers.get_flow_execution(flow.id).released_at is not None
    monkeypatch.setattr(Issue, operation, original)
    store.results.recover_claims()
    due(store, job.id)
    assert await coordinator(store, github).results.apply(job.id) is JobState.DONE
    assert len(github.writes) == 6
    assert len(github.notifications) == 2


async def test_unknown_archive_comment_stays_pending_without_resending_or_recleaning(store, monkeypatch, archive_resources):
    flow, job, result, github, subject = await pending_archive(store)

    async def unknown(issue, body):
        raise GitHubError(GitHubErrorKind.TRANSIENT, "comment")

    monkeypatch.setattr(Issue, "comment", unknown)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    due(store, job.id)
    with pytest.raises(ResultPending, match="outcome is unknown"):
        await coordinator(store, github).results.apply(job.id)
    with pytest.raises(ValueError, match="awaiting reconciliation"):
        ControlService(database=store.database).retry(flow)
    assert store.results.get_plan(job.id).archive_notice == result
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert not github.notifications


@pytest.mark.parametrize("failure", ["invalid_key", "token_rejected", "token_unavailable"])
@pytest.mark.parametrize("already_dispatched", [False, True])
async def test_archive_auth_failure_preserves_whether_comment_was_sent(
    store, tmp_path, monkeypatch, failure, already_dispatched, archive_resources):
    flow, job, notice, github, subject = await pending_archive(store)
    attempt_id = flow.current_attempt_id
    comment, find_comment = Issue.comment, Issue.find_comment
    requests = []

    def transport(request):
        requests.append(request)
        assert request.url.path == "/app/installations/42/access_tokens"
        return httpx.Response(503 if failure == "token_unavailable" else 401,
                              json={"message": "installation token unavailable"})

    adapter, _ = archive_adapter(tmp_path, monkeypatch, transport,
                                 invalid_key=failure == "invalid_key")

    if already_dispatched:
        async def lost_response(*args):
            await comment(*args)
            raise GitHubError(GitHubErrorKind.TRANSIENT, "comment")
        monkeypatch.setattr(Issue, "comment", lost_response)
        with pytest.raises(ResultPending):
            await subject.results.apply(job.id)
        due(store, job.id)
        # A failed lookup cannot clear evidence of the preceding uncertain write.
        monkeypatch.setattr(Issue, "find_comment", lambda issue, marker:
            NATIVE_ISSUE_FIND_COMMENT(Issue(adapter.installation(issue.repository.installation.installation_id).repository(issue.repository.full_name), issue.number), marker))
    else:
        # Credentials can expire between the marker lookup and the comment request.
        monkeypatch.setattr(Issue, "comment", lambda issue, body:
            NATIVE_ISSUE_COMMENT(Issue(adapter.installation(issue.repository.installation.installation_id).repository(issue.repository.full_name), issue.number), body))
    with pytest.raises(ResultPending, match=("transient" if failure == "token_unavailable"
                                             else "authentication")):
        await subject.results.apply(job.id)
    assert bool(requests) is (failure != "invalid_key")
    effect = store.results.list_effects(job.id)[2]
    expected = (EffectState.RETRY if failure == "token_unavailable" else EffectState.FAILED)
    assert effect.state is (EffectState.UNCERTAIN if already_dispatched else expected)
    assert (effect.dispatched_at is not None) is already_dispatched
    assert store.results.get_plan(job.id).archive_notice == notice
    assert store.workers.get_flow_execution(flow.id).released_at is None

    monkeypatch.setattr(Issue, "comment", comment)
    monkeypatch.setattr(Issue, "find_comment", find_comment)
    if not already_dispatched and failure != "token_unavailable":
        assert ControlService(database=store.database).retry(flow)
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    due(store, job.id)
    assert await coordinator(store, github).results.apply(job.id) is JobState.DONE
    settle_cleanup(store)
    assert len(github.notifications) == 2
    assert store.flows.get_flow(flow.id).current_attempt_id == attempt_id
    assert store.workers.get_flow_execution(flow.id).released_at is not None


async def test_archive_keeps_dispatch_evidence_when_token_expires_after_post(store, tmp_path, monkeypatch, archive_resources):
    flow, job, _, _github, subject = await pending_archive(store)
    requests = []

    def transport(request):
        requests.append(request.url.path)
        if request.url.path == "/app/installations/42/access_tokens":
            if len(requests) > 1:
                return httpx.Response(503, json={"message": "token service unavailable"})
            return httpx.Response(201, json={"token": "test-token", "expires_at":
                (store.now() + timedelta(hours=1)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
        # The POST may have committed. Expiry must not let a later SDK auth failure erase it.
        assert request.url.path.endswith("/comments")
        cache._cache = None
        return httpx.Response(503, json={"message": "comment response unavailable"})

    async def no_wait(*args):
        pass

    adapter, cache = archive_adapter(tmp_path, monkeypatch, transport)
    monkeypatch.setattr("githubkit.core.anyio.sleep", no_wait)
    monkeypatch.setattr(Issue, "comment", lambda issue, body:
        NATIVE_ISSUE_COMMENT(Issue(adapter.installation(issue.repository.installation.installation_id).repository(issue.repository.full_name), issue.number), body))
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    effect = store.results.list_effects(job.id)[2]
    assert effect.state is EffectState.UNCERTAIN
    assert effect.dispatched_at is not None
    assert requests == ["/app/installations/42/access_tokens",
                        f"/repos/{flow.repo}/issues/{flow.issue_number}/comments"]
    assert store.workers.get_flow_execution(flow.id).released_at is None


async def test_archive_retry_policy_keeps_push_token_acquisition_retryable(tmp_path, monkeypatch):
    requests = []

    def transport(request):
        assert request.url.path == "/app/installations/42/access_tokens"
        requests.append(request)
        return httpx.Response(503 if len(requests) == 1 else 201, json={"token": "test-token"})

    async def no_wait(*args):
        pass

    adapter, _ = archive_adapter(tmp_path, monkeypatch, transport)
    monkeypatch.setattr("githubkit.core.anyio.sleep", no_wait)
    # Credential issuance precedes the worker assignment and has no effect-ledger retry.
    response = await adapter._client().arequest(
        "POST", "/app/installations/42/access_tokens", json={"repositories": ["repo"]},
    )
    assert response.status_code == 201
    assert len(requests) == 2


async def test_preparation_discovery_defers_retries_to_its_durable_owner(tmp_path, monkeypatch):
    queries = []

    def transport(request):
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(201, json={"token": "test-token", "expires_at":
                (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        assert request.url.path == "/graphql"
        queries.append(request)
        if len(queries) == 1:
            return httpx.Response(503, json={"message": "temporarily unavailable"})
        return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}},
        }}}})

    async def no_wait(*args):
        pass

    adapter, _ = archive_adapter(tmp_path, monkeypatch, transport)
    monkeypatch.setattr("githubkit.core.anyio.sleep", no_wait)
    with pytest.raises(GitHubError) as caught:
        await PullRequest(adapter.installation(42).repository("owner/repo"), 17).list_review_threads()
    assert caught.value.kind is GitHubErrorKind.TRANSIENT
    assert len(queries) == 1
    snapshot = await PullRequest(adapter.installation(42).repository("owner/repo"), 17).list_review_threads()
    assert snapshot.complete
    assert len(queries) == 2


async def test_add_label_retries_definition_read_but_not_write(tmp_path, monkeypatch):
    requests = []
    definition_path = "/repos/owner/repo/labels/flow:done"
    write_path = "/repos/owner/repo/issues/17/labels"

    def transport(request):
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(201, json={"token": "test-token", "expires_at":
                (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            assert request.url.path == definition_path
            if len(requests) == 1:
                return httpx.Response(503, json={"message": "temporarily unavailable"})
            return httpx.Response(200, json={
                "id": 1, "node_id": "LA_test", "name": "flow:done", "color": "0e8a16",
                "url": f"https://api.github.com{definition_path}",
                "description": None, "default": False,
            })
        assert (request.method, request.url.path) == ("POST", write_path)
        return httpx.Response(503, json={"message": "write outcome unknown"})

    async def no_wait(*args):
        pass

    adapter, _ = archive_adapter(tmp_path, monkeypatch, transport)
    monkeypatch.setattr("githubkit.core.anyio.sleep", no_wait)
    # A transient definition lookup is safe to repeat; the subsequent write may have committed.
    with pytest.raises(GitHubError) as failure:
        await Issue(adapter.installation(42).repository("owner/repo"), 17).add_label("flow:done")
    assert failure.value.endpoint == "add_label"
    assert failure.value.status_code == 503
    assert requests == [("GET", definition_path), ("GET", definition_path), ("POST", write_path)]


async def test_archive_rate_limit_waits_until_reset_without_retry_after(store, tmp_path, monkeypatch, archive_resources):
    _flow, job, _, _github, subject = await pending_archive(store)
    reset = datetime.fromtimestamp(int(store.now().timestamp()) + 3600, UTC)
    requests = []

    def transport(request):
        requests.append(request.url.path)
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(201, json={"token": "test-token", "expires_at":
                (reset + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        return httpx.Response(403, json={"message": "rate limited"}, headers={
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(reset.timestamp())),
        })

    adapter, _ = archive_adapter(tmp_path, monkeypatch, transport)
    monkeypatch.setattr(Issue, "comment", lambda issue, body:
        NATIVE_ISSUE_COMMENT(Issue(adapter.installation(issue.repository.installation.installation_id).repository(issue.repository.full_name), issue.number), body))
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    effect = store.results.list_effects(job.id)[2]
    assert effect.state is EffectState.RETRY
    assert effect.next_attempt_at >= reset
    assert effect.dispatched_at is None
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    assert len(requests) == 2


@pytest.mark.parametrize(
    "operation,kind,status",
    [
        ("fetch_labels", GitHubErrorKind.PERMISSION, 403),
        ("add_label", GitHubErrorKind.NOT_FOUND, 404),
    ],
)
async def test_archive_label_errors_do_not_block_completion(
    store, monkeypatch, operation, kind, status, archive_resources
):
    # Display failures cannot reverse archival or prevent independent cleanup settlement.
    # The full error-kind matrix is covered by INITIAL in test_result_recovery.
    flow, job, _, github, subject = await pending_archive(store)

    async def unavailable(*args):
        raise GitHubError(kind, operation, status_code=status, request_rejected=True)

    monkeypatch.setattr(Issue, operation, unavailable)
    assert await subject.results.apply(job.id) is JobState.DONE
    skipped = [r for r in store.results.list_effects(job.id) if r.state is EffectState.SKIPPED]
    assert skipped
    assert all(
        isinstance(r.payload, LabelEffect)
        and r.receipt is None
        and r.last_error
        and str(status) in r.last_error
        for r in skipped
    )
    settle_cleanup(store)
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.workers.get_flow_execution(flow.id).released_at is not None
    assert len(github.notifications) == 2


async def test_existing_status_labels_need_no_mutation(store, archive_resources):
    flow, job, _, github, subject = await pending_archive(store)
    github.targets = {number: {"Flow:Done", "flow", "user-label"}
                      for number in (flow.issue_number, 17)}
    assert await subject.results.apply(job.id) is JobState.DONE
    assert github.writes == [("comment", flow.issue_number), ("comment", 17)]


async def test_restart_recovers_comment_before_receipt(store, monkeypatch, archive_resources):
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

    # Use a new connection pool, then replay the same inbox just as serve startup does.
    reopened = Store(store.database_url)
    try:
        reopened.results.recover_claims()
        restarted = coordinator(reopened, github)
        assert await restarted.results.apply(job.id) is JobState.DONE
    finally:
        reopened.close()
    assert len(github.notifications) == 2
    assert store.jobs.get_job(job.id).result_applied_at is not None
    settle_cleanup(store)
    assert store.flows.get_flow(flow.id).state is FlowState.ARCHIVED
    assert store.workers.get_flow_execution(flow.id).released_at is not None
