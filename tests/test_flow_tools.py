from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.control.tools import FlowToolService
from app.github_models import (
    GitHubAdapterError,
    GitHubErrorKind,
    PullRequestSnapshot,
    RemoteRef,
)
from app.models import (
    Attempt,
    DeliveryState,
    Flow,
    FlowDelivery,
    FlowExecution,
    FlowState,
    FlowToolCall,
    VcsKind,
)
from app.ports import GitHubClient
from app.protocol import FlowToolErrorKind, FlowToolRequest
from app.store import Store


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
        self, call_id: str, response: dict[str, object], error: str | None = None
    ) -> None:
        call = self.calls[call_id]
        call.response = response
        call.error = error

    def get_tool_call(self, call_id: str) -> FlowToolCall | None:
        return self.calls.get(call_id)


class ToolGitHub:
    def __init__(self) -> None:
        self.opened = 0
        self.labels = 0
        self.reviews = 0

    async def get_ref(self, installation_id: int, repo: str, branch: str) -> RemoteRef:
        return RemoteRef(branch=branch, sha="abc123")

    async def find_pull_request_by_head(
        self, installation_id: int, repo: str, branch: str
    ) -> PullRequestSnapshot | None:
        return None

    async def open_pull_request(
        self,
        installation_id: int,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestSnapshot:
        self.opened += 1
        assert base == "trunk"
        assert "Closes #9" in body
        return PullRequestSnapshot(
            number=17,
            state="open",
            head_branch=head,
            head_sha="abc123",
            base_branch=base,
        )

    async def set_labels(self, *args: object) -> None:
        self.labels += 1

    async def request_reviewers(self, *args: object) -> None:
        self.reviews += 1


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
async def test_open_pr_prepares_and_replays_before_terminal_finalize() -> None:
    store = ToolStore()
    github = ToolGitHub()
    service = FlowToolService(
        cast(Store, cast(Any, store)),
        cast(GitHubClient, cast(Any, github)),
        SimpleNamespace(reviewers=["carol"], in_review_label="flow:in-review"),
    )

    first = await service.execute(request(), worker_id="worker-a")
    replay = await service.execute(request(), worker_id="worker-a")

    assert first.error is None
    assert replay.result == first.result
    assert first.result == {
        "status": "prepared",
        "head_sha": "abc123",
        "delivery_version": 0,
    }
    assert store.delivery.state is DeliveryState.OPENING
    assert store.delivery.head_sha == "abc123"
    assert github.opened == 0
    assert github.labels == github.reviews == 0

    finalized = await service.finalize_open_pr(store.flow.id, "call-a")
    replayed_finalize = await service.finalize_open_pr(store.flow.id, "call-a")

    assert finalized == replayed_finalize
    assert finalized["pr_number"] == 17
    assert store.delivery.state is DeliveryState.OPEN
    assert github.opened == 1
    assert github.labels == github.reviews == 1


@pytest.mark.asyncio
async def test_finalize_rejects_a_closed_pull_request() -> None:
    class ClosedGitHub(ToolGitHub):
        async def find_pull_request_by_head(
            self, installation_id: int, repo: str, branch: str
        ) -> PullRequestSnapshot | None:
            return PullRequestSnapshot(
                number=17,
                state="closed",
                head_branch=branch,
                head_sha="abc123",
                base_branch="trunk",
            )

    store = ToolStore()
    github = ClosedGitHub()
    service = FlowToolService(
        cast(Store, cast(Any, store)),
        cast(GitHubClient, cast(Any, github)),
        SimpleNamespace(reviewers=["carol"], in_review_label="flow:in-review"),
    )
    assert (await service.execute(request(), worker_id="worker-a")).error is None

    with pytest.raises(RuntimeError, match="pull request is closed"):
        await service.finalize_open_pr(store.flow.id, "call-a")

    assert store.delivery.state is DeliveryState.OPENING
    assert github.labels == github.reviews == 0


@pytest.mark.asyncio
async def test_unknown_flow_tool_fails_closed() -> None:
    service = FlowToolService(
        cast(Store, cast(Any, ToolStore())),
        cast(GitHubClient, cast(Any, ToolGitHub())),
        SimpleNamespace(reviewers=[], in_review_label="flow:in-review"),
    )

    response = await service.execute(request("flow.spawn"), worker_id="worker-a")

    assert response.result is None
    assert response.error == "unknown Flow tool: flow.spawn"
    assert response.error_kind is FlowToolErrorKind.INVALID_REQUEST
    assert response.retryable is False


@pytest.mark.asyncio
async def test_stale_assignment_is_distinct_from_invalid_arguments() -> None:
    store = ToolStore()
    store.assignment_is_active = False
    service = FlowToolService(
        cast(Store, cast(Any, store)),
        cast(GitHubClient, cast(Any, ToolGitHub())),
        SimpleNamespace(reviewers=[], in_review_label="flow:in-review"),
    )

    stale = await service.execute(request(), worker_id="worker-a")
    active_service = FlowToolService(
        cast(Store, cast(Any, ToolStore())),
        cast(GitHubClient, cast(Any, ToolGitHub())),
        SimpleNamespace(reviewers=[], in_review_label="flow:in-review"),
    )
    invalid = await active_service.execute(
        request().model_copy(update={"arguments": {"title": "", "body": "Done"}}),
        worker_id="worker-a",
    )

    assert stale.error_kind is FlowToolErrorKind.STALE_ASSIGNMENT
    assert invalid.error_kind is FlowToolErrorKind.INVALID_REQUEST
    assert stale.retryable is invalid.retryable is False


@pytest.mark.asyncio
async def test_github_rate_limit_is_returned_as_retryable() -> None:
    class RateLimitedGitHub(ToolGitHub):
        async def get_ref(
            self, installation_id: int, repo: str, branch: str
        ) -> RemoteRef:
            raise GitHubAdapterError(
                GitHubErrorKind.RATE_LIMITED,
                "get ref",
                status_code=429,
                retry_after="30",
            )

    service = FlowToolService(
        cast(Store, cast(Any, ToolStore())),
        cast(GitHubClient, cast(Any, RateLimitedGitHub())),
        SimpleNamespace(reviewers=[], in_review_label="flow:in-review"),
    )

    response = await service.execute(request(), worker_id="worker-a")

    assert response.error_kind is FlowToolErrorKind.RATE_LIMITED
    assert response.retryable is True
    assert response.retry_after == "30"


@pytest.mark.asyncio
async def test_new_call_cannot_change_existing_pr_arguments() -> None:
    store = ToolStore()
    github = ToolGitHub()
    service = FlowToolService(
        cast(Store, cast(Any, store)),
        cast(GitHubClient, cast(Any, github)),
        SimpleNamespace(reviewers=[], in_review_label="flow:in-review"),
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
    assert github.opened == 0


@pytest.mark.asyncio
async def test_remote_sha_mismatch_fails_before_opening_pr() -> None:
    store = ToolStore()
    github = ToolGitHub()
    service = FlowToolService(
        cast(Store, cast(Any, store)),
        cast(GitHubClient, cast(Any, github)),
        SimpleNamespace(reviewers=[], in_review_label="flow:in-review"),
    )
    mismatched = request().model_copy(
        update={"commit_sha": "different", "remote_sha": "different"}
    )

    response = await service.execute(mismatched, worker_id="worker-a")

    assert response.result is None
    assert response.error == "remote branch SHA does not match the worker-verified commit"
    assert github.opened == 0
