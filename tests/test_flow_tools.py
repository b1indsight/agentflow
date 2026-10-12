from contextlib import contextmanager
from copy import deepcopy
from typing import Any, cast

import pytest

from app.contracts.protocol import FlowToolErrorKind, FlowToolRequest
from app.control.tools import FlowToolService
from app.domain.delivery_models import DeliveryReceipt, PreparedDelivery
from app.domain.models import (
    Attempt,
    DeliveryState,
    Flow,
    FlowDelivery,
    FlowExecution,
    FlowState,
    FlowToolCall,
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


class ToolStore:
    def __init__(self) -> None:
        self.flow = Flow(
            id=3,
            repo="owner/repo",
            issue_number=9,
            installation_id=42,
            state=FlowState.ACTIVE,
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

    @contextmanager
    def transaction(self):
        snapshot = deepcopy(self.__dict__)
        try:
            yield self
        except Exception:
            self.__dict__.update(snapshot)
            raise

    def lock_workflow(self, flow_id: int) -> Flow | None:
        return self.get_flow(flow_id)

    def assignment_active(self, *args: object) -> bool:
        return self.assignment_is_active

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
        job_id=11,
        flow_id=3,
        attempt_id=7,
        assignment_token="token-a",
        connection_epoch="epoch-a",
        call_id="call-a",
        name=name,
        arguments={"title": "Implement feature", "body": "Complete."},
        vcs_backend=VcsKind.GIT,
        branch="flow/3",
        commit_sha="abc123",
        remote_sha="abc123",
    )


@pytest.mark.asyncio
async def test_open_pr_prepares_and_replays_without_remote_delivery(tool_resources) -> None:
    store = ToolStore()
    github = ToolGitHub()
    service = FlowToolService(
        cast(Store, cast(Any, store)),
        cast(GitHubApp, cast(Any, github)),
    )

    first = await service.execute(request(), worker_id="worker-a")
    replay = await service.execute(request(), worker_id="worker-a")

    assert first.error is None
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
    service = FlowToolService(cast(Store, store), cast(GitHubApp, ToolGitHub()))
    response = await service.execute(request(), worker_id="worker-a")
    assert response.error_kind is FlowToolErrorKind.CONFLICT
    assert store.delivery.state is DeliveryState.MERGED
    assert store.calls == {}


@pytest.mark.asyncio
async def test_prepared_delivery_and_tool_receipt_roll_back_together(monkeypatch, tool_resources) -> None:
    # A crash saving the tool reply must not leave a half-prepared delivery behind.
    store = ToolStore()
    service = FlowToolService(cast(Store, store), cast(GitHubApp, ToolGitHub()))
    complete = store.complete_tool_call

    def crash(*args, **kwargs):
        complete(*args, **kwargs)
        raise RuntimeError("receipt commit interrupted")

    monkeypatch.setattr(store, "complete_tool_call", crash)
    with pytest.raises(RuntimeError, match="receipt commit interrupted"):
        await service.execute(request(), worker_id="worker-a")
    assert store.delivery.state is DeliveryState.NONE
    assert store.calls["call-a"].response is None


@pytest.mark.asyncio
async def test_cancel_during_remote_verification_cannot_prepare_delivery(monkeypatch, tool_resources) -> None:
    # The GitHub read yields control; cancellation wins before local intent is committed.
    store = ToolStore()
    github = ToolGitHub()

    async def cancel_and_read(*args, **kwargs):
        store.flow.state = FlowState.CANCELLED
        return RemoteRef(branch="flow/3", sha="abc123")

    monkeypatch.setattr(Repository, "fetch_ref", cancel_and_read)
    service = FlowToolService(cast(Store, store), cast(GitHubApp, github))
    response = await service.execute(request(), worker_id="worker-a")
    assert response.error_kind is FlowToolErrorKind.STALE_ASSIGNMENT
    assert store.delivery.state is DeliveryState.NONE
    assert store.calls["call-a"].response is None


@pytest.mark.asyncio
async def test_stale_assignment_is_distinct_from_invalid_arguments(tool_resources) -> None:
    store = ToolStore()
    store.assignment_is_active = False
    service = FlowToolService(
        cast(Store, cast(Any, store)),
        cast(GitHubApp, cast(Any, ToolGitHub())),
    )

    stale = await service.execute(request(), worker_id="worker-a")
    active_service = FlowToolService(
        cast(Store, cast(Any, ToolStore())),
        cast(GitHubApp, cast(Any, ToolGitHub())),
    )
    invalid = await active_service.execute(
        request().model_copy(update={"arguments": {"title": "", "body": "Done"}}),
        worker_id="worker-a",
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
    service = FlowToolService(
        cast(Store, cast(Any, ToolStore())),
        cast(GitHubApp, cast(Any, ToolGitHub())),
    )

    response = await service.execute(request(), worker_id="worker-a")

    assert response.error_kind is FlowToolErrorKind.RATE_LIMITED
    assert response.retryable is True
    assert response.retry_after == "30"


@pytest.mark.asyncio
async def test_new_call_cannot_change_existing_pr_arguments(tool_resources) -> None:
    store = ToolStore()
    github = ToolGitHub()
    service = FlowToolService(
        cast(Store, cast(Any, store)),
        cast(GitHubApp, cast(Any, github)),
    )
    assert (await service.execute(request(), worker_id="worker-a")).error is None
    conflicting = request().model_copy(
        update={
            "call_id": "call-b",
            "arguments": {"title": "Different", "body": "Complete."},
        }
    )

    response = await service.execute(conflicting, worker_id="worker-a")

    assert response.result is None
    assert response.error == "flow.open_pr arguments conflict with the existing delivery"
    assert github.opened_count == 0


@pytest.mark.asyncio
async def test_remote_sha_mismatch_fails_before_opening_pr(tool_resources) -> None:
    store = ToolStore()
    github = ToolGitHub()
    service = FlowToolService(
        cast(Store, cast(Any, store)),
        cast(GitHubApp, cast(Any, github)),
    )
    mismatched = request().model_copy(update={"commit_sha": "different", "remote_sha": "different"})

    response = await service.execute(mismatched, worker_id="worker-a")

    assert response.result is None
    assert response.error == "remote branch SHA does not match the worker-verified commit"
    assert github.opened_count == 0
