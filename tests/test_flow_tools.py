from contextlib import contextmanager
from copy import deepcopy
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from app.contracts.protocol import (
    PROTOCOL_VERSION,
    FlowToolArguments,
    FlowToolErrorKind,
    FlowToolRequest,
)
from app.controler.workflow.tools import FlowToolService
from app.domain.delivery_models import DeliveryReceipt, PreparedDelivery
from app.domain.models import (
    ArchiveReason,
    Attempt,
    DeliveryState,
    Flow,
    FlowDelivery,
    FlowExecution,
    FlowState,
    FlowToolCall,
    JobKind,
    JobState,
    VcsKind,
)
from app.github import (
    GitHubApp,
    GitHubError,
    GitHubErrorKind,
    Issue,
    PullRequest,
    RemoteRef,
    Repository,
)
from app.persistence.store import Store
from tests.github import RecordingGitHub, record_call
from tests.persistence_fakes import RepositoryViews


class ToolStore(RepositoryViews):
    def __init__(self) -> None:
        self.flow = Flow(
            id=3,
            repo="owner/repo",
            issue_number=9,
            installation_id=42,
            state=FlowState.RUNNING,
            current_attempt_id=7,
        )
        self.attempt = Attempt(id=7, flow_id=3)
        self.execution = FlowExecution(
            flow_id=3,
            worker_id="worker-a",
            repo_identity="worker-a:owner/repo",
        )
        self.delivery = FlowDelivery(
            flow_id=3,
            branch="flow/3",
            base_branch="trunk",
        )
        self.calls: dict[str, FlowToolCall] = {}
        self.assignment_is_active = True
        self.connection_epoch = "epoch-a"
        self.checked_epochs: list[str] = []

    @contextmanager
    def transaction(self):
        snapshot = deepcopy(self.__dict__)
        try:
            yield self
        except Exception:
            snapshot["checked_epochs"] = self.checked_epochs
            self.__dict__.update(snapshot)
            raise

    def lock_workflow(self, flow_id: int) -> Flow | None:
        return self.get_flow(flow_id)

    def assignment_active(self, job: int, worker: str, token: str, epoch: str) -> bool:
        self.checked_epochs.append(epoch)
        return self.assignment_is_active and epoch == self.connection_epoch

    def get_flow(self, flow_id: int) -> Flow | None:
        return self.flow if flow_id == self.flow.id else None

    def get_attempt(self, attempt_id: int) -> Attempt | None:
        return self.attempt if attempt_id == self.attempt.id else None

    def get_flow_execution(self, flow_id: int) -> FlowExecution | None:
        return self.execution if flow_id == self.flow.id else None

    def get_delivery(self, flow_id: int) -> FlowDelivery | None:
        return self.delivery if flow_id == self.flow.id else None

    def update_delivery(self, delivery: FlowDelivery) -> None:
        self.delivery = delivery

    def reserve_tool_call(self, **values: Any) -> tuple[FlowToolCall, bool]:
        previous = self.calls.get(values["call_id"])
        if previous is not None:
            return previous, False
        call = FlowToolCall(**values)
        self.calls[call.call_id] = call
        return call, True

    def complete_tool_call(
        self, call_id: str, response: DeliveryReceipt
    ) -> None:
        call = self.calls[call_id]
        call.response = response

    def get_tool_call(self, call_id: str) -> FlowToolCall | None:
        return self.calls.get(call_id)


class ToolGitHub(RecordingGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.opened_count = 0
        self.labels_count = 0
        self.reviews_count = 0


@pytest.fixture
def tool_resources(recording_resources, monkeypatch):
    async def open_pull_request(repo, *, head, base, title, body):
        state = record_call(repo, "open_pull_request", head=head)
        state.opened_count += 1
        assert base == "trunk"
        assert "Closes #9" in body
        return PullRequest(repo, 17, state="open", head_branch=head,
                           head_sha="abc123", base_branch=base)

    async def add_label(issue, label):
        state = record_call(issue.repository, "add_label", number=issue.number)
        state.labels_count += 1

    async def request_reviewers(pr, reviewers):
        state = record_call(pr.repository, "request_reviewers", number=pr.number)
        state.reviews_count += 1

    monkeypatch.setattr(Repository, "open_pull_request", open_pull_request)
    monkeypatch.setattr(Issue, "add_label", add_label)
    monkeypatch.setattr(PullRequest, "request_reviewers", request_reviewers)


def request(name: str = "flow.open_pr") -> FlowToolRequest:
    return FlowToolRequest(
        message_id=uuid4().hex,
        type="tool_call",
        reply_to=None,
        method=name,
        arguments=FlowToolArguments(
            job_id=11,
            flow_id=3,
            attempt_id=7,
            assignment_token="token-a",
            call_id="call-a",
            arguments={"title": "Implement feature", "body": "Complete."},
            vcs_backend=VcsKind.GIT,
            branch="flow/3",
            commit_sha="abc123",
            remote_sha="abc123",
        ),
    )


@pytest.mark.asyncio
async def test_open_pr_prepares_and_replays_without_remote_delivery(tool_resources) -> None:
    store = ToolStore()
    github = ToolGitHub()
    service = FlowToolService(
        cast(GitHubApp, cast(Any, github)),
        database=cast(Store, cast(Any, store)).database,
    )

    invocation, retry = request(), request()
    first = await service.execute(invocation, worker_id="worker-a", connection_epoch="epoch-a")
    replay = await service.execute(retry, worker_id="worker-a", connection_epoch="epoch-a")

    assert first.error is None
    assert first.reply_to == invocation.message_id and replay.reply_to == retry.message_id
    assert first.method == replay.method == invocation.method
    assert first.call_id == replay.call_id == invocation.arguments.call_id
    assert first.message_id != replay.message_id
    assert UUID(first.message_id).version == UUID(replay.message_id).version == 4
    assert store.checked_epochs == ["epoch-a", "epoch-a", "epoch-a"]
    assert replay.result == first.result
    assert first.result == PreparedDelivery(status="prepared", head_sha="abc123", delivery_version=0)
    assert store.delivery.state is DeliveryState.OPENING
    assert store.delivery.head_sha == "abc123"
    assert github.opened_count == 0
    assert github.labels_count == github.reviews_count == 0


@pytest.mark.asyncio
async def test_open_pr_after_merge_cannot_restore_opening_delivery(tool_resources) -> None:
    # INITIAL may still be running and call the tool again after its PR was merged.
    store = ToolStore()
    store.delivery.state = DeliveryState.MERGED
    store.delivery.pr_number = 17
    service = FlowToolService(
        cast(GitHubApp, ToolGitHub()),
        database=cast(Store, store).database,
    )
    response = await service.execute(request(), worker_id="worker-a", connection_epoch="epoch-a")
    assert response.error_kind is FlowToolErrorKind.CONFLICT
    assert store.delivery.state is DeliveryState.MERGED
    assert store.calls == {}


@pytest.mark.asyncio
async def test_prepared_delivery_and_tool_receipt_roll_back_together(monkeypatch, tool_resources) -> None:
    # A crash saving the tool reply must not leave a half-prepared delivery behind.
    store = ToolStore()
    service = FlowToolService(
        cast(GitHubApp, ToolGitHub()),
        database=cast(Store, store).database,
    )
    complete = store.deliveries.complete_tool_call

    def crash(*args, **kwargs):
        complete(*args, **kwargs)
        raise RuntimeError("receipt commit interrupted")

    monkeypatch.setattr(store, "complete_tool_call", crash)
    with pytest.raises(RuntimeError, match="receipt commit interrupted"):
        await service.execute(request(), worker_id="worker-a", connection_epoch="epoch-a")
    assert store.delivery.state is DeliveryState.NONE
    assert store.calls["call-a"].response is None


@pytest.mark.asyncio
async def test_cancel_during_remote_verification_cannot_prepare_delivery(monkeypatch, tool_resources) -> None:
    # The GitHub read yields control; cancellation wins before local intent is committed.
    store = ToolStore()
    github = ToolGitHub()

    async def cancel_and_read(*args, **kwargs):
        store.flow.state = FlowState.FINALIZING
        store.flow.archive_reason = ArchiveReason.ABANDONED
        return RemoteRef(branch="flow/3", sha="abc123")

    monkeypatch.setattr(Repository, "fetch_ref", cancel_and_read)
    service = FlowToolService(
        cast(GitHubApp, github),
        database=cast(Store, store).database,
    )
    response = await service.execute(request(), worker_id="worker-a", connection_epoch="epoch-a")
    assert response.error_kind is FlowToolErrorKind.STALE_ASSIGNMENT
    assert store.delivery.state is DeliveryState.NONE
    assert store.calls["call-a"].response is None


@pytest.mark.asyncio
async def test_stale_assignment_is_distinct_from_invalid_arguments(tool_resources) -> None:
    store = ToolStore()
    store.assignment_is_active = False
    service = FlowToolService(
        cast(GitHubApp, cast(Any, ToolGitHub())),
        database=cast(Store, cast(Any, store)).database,
    )

    stale = await service.execute(request(), worker_id="worker-a", connection_epoch="epoch-a")
    active_store = ToolStore()
    active_service = FlowToolService(
        cast(GitHubApp, cast(Any, ToolGitHub())),
        database=cast(Store, cast(Any, active_store)).database,
    )
    invocation = request()
    invalid = await active_service.execute(
        invocation.model_copy(
            update={
                "arguments": invocation.arguments.model_copy(
                    update={"arguments": {"title": "", "body": "Done"}},
                )
            }
        ),
        worker_id="worker-a",
        connection_epoch="epoch-a",
    )

    assert stale.error_kind is FlowToolErrorKind.STALE_ASSIGNMENT
    assert invalid.error_kind is FlowToolErrorKind.INVALID_REQUEST
    assert stale.retryable is invalid.retryable is False


@pytest.mark.asyncio
async def test_github_rate_limit_is_returned_as_retryable(tool_resources, monkeypatch) -> None:
    async def limited(repo, branch, *, retry_transient=False):
        raise GitHubError(GitHubErrorKind.RATE_LIMITED, "get ref", status_code=429,
                          retry_after="30")

    monkeypatch.setattr(Repository, "fetch_ref", limited)
    store = ToolStore()
    service = FlowToolService(
        cast(GitHubApp, cast(Any, ToolGitHub())),
        database=cast(Store, cast(Any, store)).database,
    )

    response = await service.execute(request(), worker_id="worker-a", connection_epoch="epoch-a")

    assert response.error_kind is FlowToolErrorKind.RATE_LIMITED
    assert response.retryable is True
    assert response.retry_after == "30"


@pytest.mark.asyncio
async def test_new_call_cannot_change_existing_pr_arguments(tool_resources) -> None:
    store = ToolStore()
    github = ToolGitHub()
    service = FlowToolService(
        cast(GitHubApp, cast(Any, github)),
        database=cast(Store, cast(Any, store)).database,
    )
    assert (
        await service.execute(request(), worker_id="worker-a", connection_epoch="epoch-a")
    ).error is None
    invocation = request()
    conflicting = invocation.model_copy(
        update={
            "arguments": invocation.arguments.model_copy(
                update={
                    "call_id": "call-b",
                    "arguments": {"title": "Different", "body": "Complete."},
                }
            )
        }
    )

    response = await service.execute(conflicting, worker_id="worker-a", connection_epoch="epoch-a")

    assert response.result is None
    assert response.error == "flow.open_pr arguments conflict with the existing delivery"
    assert github.opened_count == 0


@pytest.mark.asyncio
async def test_remote_sha_mismatch_fails_before_opening_pr(tool_resources) -> None:
    store = ToolStore()
    github = ToolGitHub()
    service = FlowToolService(
        cast(GitHubApp, cast(Any, github)),
        database=cast(Store, cast(Any, store)).database,
    )
    invocation = request()
    mismatched = invocation.model_copy(
        update={
            "arguments": invocation.arguments.model_copy(
                update={"commit_sha": "different", "remote_sha": "different"},
            )
        }
    )

    response = await service.execute(mismatched, worker_id="worker-a", connection_epoch="epoch-a")

    assert response.result is None
    assert response.error == "remote branch SHA does not match the worker-verified commit"
    assert github.opened_count == 0


async def test_reconnect_during_tool_read_keeps_original_epoch(monkeypatch, tool_resources):
    # A reconnect during GitHub I/O must not authorize the old call on the new epoch.
    store = ToolStore()
    service = FlowToolService(ToolGitHub(), database=store.database)

    async def reconnect(repo, branch, *, retry_transient=False):
        store.connection_epoch = "epoch-b"
        return RemoteRef(branch=branch, sha="abc123")

    monkeypatch.setattr(Repository, "fetch_ref", reconnect)
    invocation = request()
    response = await service.execute(invocation, worker_id="worker-a", connection_epoch="epoch-a")
    assert response.error_kind is FlowToolErrorKind.STALE_ASSIGNMENT
    assert response.reply_to == invocation.message_id
    assert response.method == invocation.method and response.call_id == invocation.arguments.call_id
    assert store.delivery.state is DeliveryState.NONE
    assert store.calls["call-a"].response is None
    assert store.checked_epochs == ["epoch-a", "epoch-a"]


async def test_unknown_tool_returns_correlated_domain_error():
    store = ToolStore()
    service = FlowToolService(ToolGitHub(), database=store.database)
    invocation = request("unregistered.method")
    response = await service.execute(invocation, worker_id="worker-a", connection_epoch="epoch-a")
    assert response.error_kind is FlowToolErrorKind.INVALID_REQUEST
    assert response.reply_to == invocation.message_id and response.method == invocation.method
    assert response.call_id == invocation.arguments.call_id
    assert store.checked_epochs == [] and store.calls == {}


async def test_tool_ledger_keeps_business_arguments_and_rejects_call_id_reuse_by_another_job(
    store, recording_resources
):
    flow = store.flows.create_flow("owner/repo", 9, 42)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.base_branch = "trunk"
    store.deliveries.update_delivery(delivery)
    store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    store.workers.worker_connected("worker-a", PROTOCOL_VERSION, "epoch-a")
    job = store.workers.assign_job("worker-a", "token-a", "epoch-a")
    invocation = request()
    invocation = invocation.model_copy(
        update={
            "arguments": invocation.arguments.model_copy(
                update={
                    "flow_id": flow.id,
                    "attempt_id": attempt.id,
                    "job_id": job.id,
                    "branch": delivery.branch,
                }
            )
        }
    )
    service = FlowToolService(ToolGitHub(), database=store.database)
    response = await service.execute(invocation, worker_id="worker-a", connection_epoch="epoch-a")
    assert response.result is not None
    call = store.deliveries.get_tool_call(invocation.arguments.call_id)
    assert (call.flow_id, call.attempt_id, call.job_id, call.name) == (
        flow.id,
        attempt.id,
        job.id,
        invocation.method,
    )
    assert call.arguments == invocation.arguments.arguments
    assert call.response == response.result
    assert store.workers.finish_assignment(job.id, "worker-a", "token-a", JobState.DONE, "epoch-a")
    store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    replacement = store.workers.assign_job("worker-a", "token-b", "epoch-a")
    # Even with the same Flow and PR arguments, a later execution cannot claim an old call_id.
    conflicting = invocation.model_copy(
        update={
            "message_id": uuid4().hex,
            "arguments": invocation.arguments.model_copy(
                update={
                    "job_id": replacement.id,
                    "assignment_token": "token-b",
                }
            ),
        }
    )
    rejected = await service.execute(conflicting, worker_id="worker-a", connection_epoch="epoch-a")
    assert rejected.error_kind is FlowToolErrorKind.CONFLICT
    assert rejected.reply_to == conflicting.message_id
    assert store.deliveries.get_tool_call(invocation.arguments.call_id) == call
