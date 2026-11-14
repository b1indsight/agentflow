"""Flow tools use the real assignment fences, delivery intent and PostgreSQL ledger."""

from uuid import UUID, uuid4

import pytest

from app.contracts.protocol import (
    FlowToolArguments,
    FlowToolErrorKind,
    FlowToolRequest,
    call_header,
)
from app.controler.workflow.service import ControlService
from app.controler.workflow.tools import FlowToolService
from app.domain.delivery_models import PreparedDelivery
from app.domain.models import ArchiveReason, DeliveryState, FlowState, JobKind, VcsKind
from app.github import GitHubError, GitHubErrorKind, RemoteRef, Repository
from app.persistence.deliveries import DeliveryStore
from tests.github import RecordingGitHub
from tests.test_merge import route_merge
from tests.workspace_fixtures import register_worker


@pytest.fixture
def fresh_tool_request(prepared_initial):
    # Reuse the prepared assignment, but leave this call without a receipt so it
    # must pass the remote-read and transactional preparation boundaries again.
    _, _, _, request, _ = prepared_initial
    return request.model_copy(update={
        "message_id": uuid4().hex,
        "arguments": request.arguments.model_copy(update={"call_id": uuid4().hex}),
    })


async def test_direct_open_pr_preserves_body_without_closing_a_fake_issue(store, recording_resources):
    # Direct requirements have no Issue; preserve the worker's exact body across persisted replay.
    flow = ControlService(database=store.database).create_flow(
        "owner/repo", None, 42, task_content="Direct",
    )
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    register_worker(store, "worker-a", repo=flow.repo, epoch="epoch")
    job = store.workers.assign_job("worker-a", "token", "epoch")
    assert store.workers.start_assignment(job.id, attempt.id, "worker-a", "token", "epoch")
    delivery = store.deliveries.get_or_create_delivery(flow.id, flow.initial_branch)
    delivery.base_branch = "trunk"
    store.deliveries.update_delivery(delivery)
    body = "\n## Direct result\n\nPreserve trailing whitespace.  \n"
    request = FlowToolRequest(**call_header(), method="flow.open_pr", arguments=FlowToolArguments(
        job_id=job.id, flow_id=flow.id, attempt_id=attempt.id, assignment_token="token",
        call_id="direct-open", arguments={"title": "Direct PR", "body": body},
        vcs_backend=VcsKind.GIT, branch=delivery.branch, commit_sha="abc123", remote_sha="abc123",
    ))
    tools = FlowToolService(RecordingGitHub(), database=store.database)
    first = await tools.execute(request, worker_id="worker-a", connection_epoch="epoch")
    assert first.error is None
    assert store.deliveries.get_delivery(flow.id).body == body
    replay = await tools.execute(request, worker_id="worker-a", connection_epoch="epoch")
    assert replay.result == first.result


async def test_open_pr_prepares_and_replays_without_remote_delivery(
    store, prepared_initial, fresh_tool_request,
):
    flow, subject, github, _, result = prepared_initial
    invocation = fresh_tool_request
    retry = invocation.model_copy(update={"message_id": uuid4().hex})
    before = len(github.resource_calls)
    first = await subject.tools.execute(invocation, worker_id="worker-a", connection_epoch="epoch")
    replay = await subject.tools.execute(retry, worker_id="worker-a", connection_epoch="epoch")

    assert first.error is replay.error is None
    assert first.reply_to == invocation.message_id and replay.reply_to == retry.message_id
    assert first.method == replay.method == invocation.method
    assert first.call_id == replay.call_id == invocation.arguments.call_id
    assert first.message_id != replay.message_id
    assert UUID(first.message_id).version == UUID(replay.message_id).version == 4
    assert replay.result == first.result == result.delivery_receipt
    assert first.result == PreparedDelivery(status="prepared", head_sha="abc123", delivery_version=0)
    delivery = store.deliveries.get_delivery(flow.id)
    assert delivery.state is DeliveryState.OPENING
    assert delivery.head_sha == "abc123"
    assert delivery.base_branch == "trunk"
    assert delivery.body == f"Closes #{flow.issue_number}\n\n{invocation.arguments.arguments['body']}"
    assert store.deliveries.get_tool_call(invocation.arguments.call_id).response == first.result
    assert [call["operation"] for call in github.resource_calls[before:]] == ["fetch_ref"]
    assert github.opened == github.labels == github.review_requests == []


async def test_open_pr_after_merge_cannot_restore_opening_delivery(store, completed_open_initial):
    # A still-running execution can emit a tool call after the merge webhook retires coding.
    flow, subject, github, request, _ = completed_open_initial
    job = store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    replacement = store.workers.assign_job("worker-a", "later-token", "epoch")
    assert replacement is not None and replacement.id == job.id
    assert store.workers.start_assignment(
        job.id, request.arguments.attempt_id, "worker-a", "later-token", "epoch",
    )
    invocation = request.model_copy(update={
        "arguments": request.arguments.model_copy(update={
            "job_id": job.id, "assignment_token": "later-token", "call_id": uuid4().hex,
        }),
    })
    await route_merge(store, flow)
    delivery = store.deliveries.get_delivery(flow.id)
    assert delivery.state is DeliveryState.MERGED

    response = await subject.tools.execute(invocation, worker_id="worker-a", connection_epoch="epoch")

    assert response.error_kind is FlowToolErrorKind.CONFLICT
    assert store.deliveries.get_delivery(flow.id) == delivery
    assert store.deliveries.get_tool_call(invocation.arguments.call_id) is None
    assert github.opened == [request.arguments.branch]


async def test_prepared_delivery_and_tool_receipt_roll_back_together(
    store, prepared_initial, fresh_tool_request, monkeypatch,
):
    # A crash after both writes must preserve the previous intent and leave the
    # new call retryable; otherwise its unsaved receipt could acknowledge a new head.
    flow, subject, _, original, result = prepared_initial
    before = store.deliveries.get_delivery(flow.id)
    next_sha = "b" * 40
    invocation = fresh_tool_request.model_copy(update={
        "arguments": fresh_tool_request.arguments.model_copy(update={
            "commit_sha": next_sha, "remote_sha": next_sha,
        }),
    })

    async def next_ref(repo, branch, *, retry_transient=False):
        return RemoteRef(branch=branch, sha=next_sha)

    complete = DeliveryStore.complete_tool_call

    def crash(self, call_id, response):
        assert response.head_sha == next_sha
        complete(self, call_id, response)
        raise RuntimeError("receipt commit interrupted")

    monkeypatch.setattr(Repository, "fetch_ref", next_ref)
    monkeypatch.setattr(DeliveryStore, "complete_tool_call", crash)
    with pytest.raises(RuntimeError, match="receipt commit interrupted"):
        await subject.tools.execute(invocation, worker_id="worker-a", connection_epoch="epoch")
    assert store.deliveries.get_delivery(flow.id) == before
    assert store.deliveries.get_tool_call(invocation.arguments.call_id).response is None
    assert store.deliveries.get_tool_call(original.arguments.call_id).response == result.delivery_receipt


async def test_cancel_during_remote_verification_cannot_prepare_delivery(
    store, prepared_initial, fresh_tool_request, monkeypatch,
):
    # The GitHub read yields control; cancellation wins before local intent is committed.
    flow, subject, _, _, _ = prepared_initial
    before = store.deliveries.get_delivery(flow.id)

    async def cancel_and_read(repo, branch, *, retry_transient=False):
        ControlService(database=store.database).cancel(flow)
        return RemoteRef(branch=branch, sha="abc123")

    monkeypatch.setattr(Repository, "fetch_ref", cancel_and_read)
    response = await subject.tools.execute(
        fresh_tool_request, worker_id="worker-a", connection_epoch="epoch",
    )
    assert response.error_kind is FlowToolErrorKind.STALE_ASSIGNMENT
    current = store.flows.get_flow(flow.id)
    assert current.state is FlowState.FINALIZING and current.archive_reason is ArchiveReason.ABANDONED
    assert store.deliveries.get_delivery(flow.id) == before
    assert store.deliveries.get_tool_call(fresh_tool_request.arguments.call_id).response is None


@pytest.mark.parametrize("token,epoch", [("stale-token", "epoch"), ("token", "stale-epoch")])
async def test_stale_assignment_is_not_retryable(
    store, prepared_initial, fresh_tool_request, token, epoch,
):
    _, subject, github, _, _ = prepared_initial
    invocation = fresh_tool_request.model_copy(update={
        "arguments": fresh_tool_request.arguments.model_copy(update={"assignment_token": token}),
    })
    before = list(github.resource_calls)
    response = await subject.tools.execute(invocation, worker_id="worker-a", connection_epoch=epoch)
    assert response.error_kind is FlowToolErrorKind.STALE_ASSIGNMENT
    assert response.retryable is False
    assert store.deliveries.get_tool_call(invocation.arguments.call_id) is None
    assert github.resource_calls == before


@pytest.mark.parametrize("arguments", [{"title": "", "body": "Done"}, {"title": "Title", "body": None}])
async def test_invalid_arguments_are_distinct_from_stale_assignment(
    store, prepared_initial, fresh_tool_request, arguments,
):
    _, subject, _, _, _ = prepared_initial
    invocation = fresh_tool_request.model_copy(update={
        "arguments": fresh_tool_request.arguments.model_copy(update={"arguments": arguments}),
    })
    response = await subject.tools.execute(invocation, worker_id="worker-a", connection_epoch="epoch")
    assert response.error_kind is FlowToolErrorKind.INVALID_REQUEST
    assert response.retryable is False
    assert store.deliveries.get_tool_call(invocation.arguments.call_id) is None


async def test_github_rate_limit_is_returned_as_retryable(
    store, prepared_initial, fresh_tool_request, monkeypatch,
):
    async def limited(repo, branch, *, retry_transient=False):
        raise GitHubError(GitHubErrorKind.RATE_LIMITED, "get ref", status_code=429,
                          retry_after="30")

    monkeypatch.setattr(Repository, "fetch_ref", limited)
    flow, subject, _, _, _ = prepared_initial
    before = store.deliveries.get_delivery(flow.id)
    response = await subject.tools.execute(
        fresh_tool_request, worker_id="worker-a", connection_epoch="epoch",
    )
    assert response.error_kind is FlowToolErrorKind.RATE_LIMITED
    assert response.retryable is True and response.retry_after == "30"
    assert store.deliveries.get_delivery(flow.id) == before
    assert store.deliveries.get_tool_call(fresh_tool_request.arguments.call_id).response is None


async def test_new_call_cannot_change_existing_pr_arguments(store, prepared_initial, fresh_tool_request):
    flow, subject, github, _, _ = prepared_initial
    before = store.deliveries.get_delivery(flow.id)
    conflicting = fresh_tool_request.model_copy(update={
        "arguments": fresh_tool_request.arguments.model_copy(update={
            "arguments": {"title": "Different", "body": "Body"},
        }),
    })
    response = await subject.tools.execute(conflicting, worker_id="worker-a", connection_epoch="epoch")
    assert response.result is None and response.error_kind is FlowToolErrorKind.CONFLICT
    assert response.error == "flow.open_pr arguments conflict with the existing delivery"
    assert store.deliveries.get_delivery(flow.id) == before
    assert store.deliveries.get_tool_call(conflicting.arguments.call_id) is None
    assert github.opened == []


async def test_remote_sha_mismatch_fails_before_opening_pr(store, prepared_initial, fresh_tool_request):
    flow, subject, github, _, _ = prepared_initial
    before = store.deliveries.get_delivery(flow.id)
    mismatched = fresh_tool_request.model_copy(update={
        "arguments": fresh_tool_request.arguments.model_copy(update={
            "commit_sha": "different", "remote_sha": "different",
        }),
    })
    response = await subject.tools.execute(mismatched, worker_id="worker-a", connection_epoch="epoch")
    assert response.result is None and response.error_kind is FlowToolErrorKind.CONFLICT
    assert response.error == "remote branch SHA does not match the worker-verified commit"
    assert store.deliveries.get_delivery(flow.id) == before
    assert store.deliveries.get_tool_call(mismatched.arguments.call_id).response is None
    assert github.opened == []


async def test_reconnect_during_tool_read_keeps_original_epoch(
    store, prepared_initial, fresh_tool_request, monkeypatch,
):
    # A reconnect authorizes the new connection, never the in-flight old call.
    flow, subject, _, _, _ = prepared_initial
    before = store.deliveries.get_delivery(flow.id)

    async def reconnect(repo, branch, *, retry_transient=False):
        register_worker(store, "worker-a", repo=flow.repo, epoch="epoch-b")
        assert store.workers.resume_assignment(
            fresh_tool_request.arguments.job_id, "worker-a", "token", "epoch-b",
        )
        return RemoteRef(branch=branch, sha="abc123")

    monkeypatch.setattr(Repository, "fetch_ref", reconnect)
    response = await subject.tools.execute(
        fresh_tool_request, worker_id="worker-a", connection_epoch="epoch",
    )
    assert response.error_kind is FlowToolErrorKind.STALE_ASSIGNMENT
    assert response.reply_to == fresh_tool_request.message_id
    assert response.method == fresh_tool_request.method
    assert response.call_id == fresh_tool_request.arguments.call_id
    assert store.deliveries.get_delivery(flow.id) == before
    assert store.deliveries.get_tool_call(fresh_tool_request.arguments.call_id).response is None
    assert store.workers.assignment_active(fresh_tool_request.arguments.job_id, "worker-a", "token", "epoch-b")
    assert not store.workers.assignment_active(fresh_tool_request.arguments.job_id, "worker-a", "token", "epoch")


async def test_unknown_tool_returns_correlated_domain_error(store, prepared_initial, fresh_tool_request):
    _, subject, github, _, _ = prepared_initial
    invocation = fresh_tool_request.model_copy(update={"method": "unregistered.method"})
    before = list(github.resource_calls)
    response = await subject.tools.execute(invocation, worker_id="worker-a", connection_epoch="epoch")
    assert response.error_kind is FlowToolErrorKind.INVALID_REQUEST
    assert response.reply_to == invocation.message_id and response.method == invocation.method
    assert response.call_id == invocation.arguments.call_id
    assert store.deliveries.get_tool_call(invocation.arguments.call_id) is None
    assert github.resource_calls == before


async def test_tool_ledger_keeps_business_arguments_and_rejects_call_id_reuse_by_another_job(
    store, completed_open_initial,
):
    flow, subject, _, invocation, result = completed_open_initial
    call = store.deliveries.get_tool_call(invocation.arguments.call_id)
    assert (call.flow_id, call.attempt_id, call.job_id, call.name) == (
        flow.id, invocation.arguments.attempt_id, invocation.arguments.job_id, invocation.method,
    )
    assert call.arguments == invocation.arguments.arguments
    assert call.response == result.delivery_receipt
    job = store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    replacement = store.workers.assign_job("worker-a", "token-b", "epoch")
    assert replacement is not None and replacement.id == job.id
    # A later execution cannot claim an old call_id, even with identical business arguments.
    conflicting = invocation.model_copy(update={
        "message_id": uuid4().hex,
        "arguments": invocation.arguments.model_copy(update={
            "job_id": replacement.id, "assignment_token": "token-b",
        }),
    })
    rejected = await subject.tools.execute(conflicting, worker_id="worker-a", connection_epoch="epoch")
    assert rejected.error_kind is FlowToolErrorKind.CONFLICT
    assert rejected.error == f"tool call {invocation.arguments.call_id} conflicts with its persisted request"
    assert rejected.reply_to == conflicting.message_id
    assert store.deliveries.get_tool_call(invocation.arguments.call_id) == call
