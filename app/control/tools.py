"""Fenced, idempotent Flow tools executed by the control plane."""

from __future__ import annotations

import asyncio
from typing import Protocol

from loguru import logger

from app.github_models import GitHubAdapterError, GitHubErrorKind, PullRequestSnapshot
from app.models import TERMINAL_STATES, DeliveryState, FlowDelivery
from app.plugins import get_plugin_manager
from app.ports import GitHubClient
from app.protocol import FlowToolErrorKind, FlowToolRequest, FlowToolResponse
from app.store import Store


class ToolSettings(Protocol):
    reviewers: list[str]
    in_review_label: str


class FlowToolRejected(RuntimeError):
    """Expected request/state rejection safe to return across the worker boundary."""

    def __init__(self, kind: FlowToolErrorKind, detail: str) -> None:
        self.kind = kind
        super().__init__(detail)


class FlowToolService:
    """Dispatch the small registered Flow tool surface behind assignment fences."""

    def __init__(
        self,
        store: Store,
        github: GitHubClient,
        settings: ToolSettings,
        plugins=None,
    ) -> None:
        self.store = store
        self.github = github
        self.settings = settings
        self.plugins = plugins or get_plugin_manager()
        self._locks: dict[int, asyncio.Lock] = {}

    async def execute(
        self, request: FlowToolRequest, *, worker_id: str
    ) -> FlowToolResponse:
        if request.name != "flow.open_pr":
            return self._error(
                request,
                FlowToolErrorKind.INVALID_REQUEST,
                f"unknown Flow tool: {request.name}",
            )
        async with self._locks.setdefault(request.flow_id, asyncio.Lock()):
            try:
                return await self._open_pr(request, worker_id)
            except FlowToolRejected as exc:
                return self._error(request, exc.kind, str(exc))
            except GitHubAdapterError as exc:
                return self._github_error(request, exc)

    async def _open_pr(
        self, request: FlowToolRequest, worker_id: str
    ) -> FlowToolResponse:
        if not self.store.assignment_active(
            request.job_id,
            worker_id,
            request.assignment_token,
            request.connection_epoch,
        ):
            raise FlowToolRejected(
                FlowToolErrorKind.STALE_ASSIGNMENT,
                "Flow tool assignment fence expired",
            )
        flow = self.store.get_flow(request.flow_id)
        attempt = self.store.get_attempt(request.attempt_id)
        execution = self.store.get_flow_execution(request.flow_id)
        delivery = self.store.get_delivery(request.flow_id)
        if flow is None or flow.state in TERMINAL_STATES:
            raise FlowToolRejected(FlowToolErrorKind.CONFLICT, "Flow is not active")
        if attempt is None or attempt.flow_id != flow.id or flow.current_attempt_id != attempt.id:
            raise FlowToolRejected(
                FlowToolErrorKind.STALE_ASSIGNMENT,
                "Flow tool attempt is not current",
            )
        if execution is None or execution.released_at is not None or execution.worker_id != worker_id:
            raise FlowToolRejected(
                FlowToolErrorKind.STALE_ASSIGNMENT,
                "Flow execution binding does not match worker",
            )
        if delivery is None or delivery.base_branch is None:
            raise FlowToolRejected(
                FlowToolErrorKind.CONFLICT,
                "Flow delivery is not prepared",
            )
        if request.branch != delivery.branch:
            raise FlowToolRejected(
                FlowToolErrorKind.CONFLICT,
                "worker-verified branch does not match the Flow delivery",
            )
        if request.commit_sha != request.remote_sha:
            raise FlowToolRejected(
                FlowToolErrorKind.CONFLICT,
                "worker did not verify the pushed commit",
            )
        title = request.arguments.get("title")
        body = request.arguments.get("body")
        if not isinstance(title, str) or not title.strip():
            raise FlowToolRejected(
                FlowToolErrorKind.INVALID_REQUEST,
                "flow.open_pr title must be a non-empty string",
            )
        if not isinstance(body, str):
            raise FlowToolRejected(
                FlowToolErrorKind.INVALID_REQUEST,
                "flow.open_pr body must be a string",
            )
        title = title.strip()
        body = self._closing_body(body, flow.issue_number)
        if (delivery.title is not None and delivery.title != title) or (
            delivery.body is not None and delivery.body != body
        ):
            raise FlowToolRejected(
                FlowToolErrorKind.CONFLICT,
                "flow.open_pr arguments conflict with the existing delivery",
            )
        try:
            call, _ = self.store.reserve_tool_call(
                call_id=request.call_id,
                flow_id=flow.id,
                attempt_id=attempt.id,
                job_id=request.job_id,
                name=request.name,
                arguments=request.arguments,
            )
        except ValueError as exc:
            raise FlowToolRejected(FlowToolErrorKind.CONFLICT, str(exc)) from exc
        if call.response is not None or call.error is not None:
            if call.error is not None:
                return self._error(request, FlowToolErrorKind.CONFLICT, call.error)
            return FlowToolResponse(call_id=request.call_id, result=call.response)

        installation_id = flow.installation_id
        if installation_id is None:
            raise FlowToolRejected(
                FlowToolErrorKind.AUTHORIZATION,
                "Flow has no GitHub installation",
            )
        remote = await self.github.get_ref(installation_id, flow.repo, delivery.branch)
        if remote.branch != delivery.branch:
            raise FlowToolRejected(
                FlowToolErrorKind.UPSTREAM,
                "GitHub remote returned an unexpected branch",
            )
        if remote.sha != request.commit_sha:
            raise FlowToolRejected(
                FlowToolErrorKind.CONFLICT,
                "remote branch SHA does not match the worker-verified commit",
            )
        if delivery.state is DeliveryState.OPEN:
            if delivery.head_sha != remote.sha:
                raise FlowToolRejected(
                    FlowToolErrorKind.CONFLICT,
                    "open Flow delivery head does not match the remote branch",
                )
            existing_response = self._delivery_response(delivery)
            self.store.complete_tool_call(request.call_id, existing_response)
            return FlowToolResponse(call_id=request.call_id, result=existing_response)
        delivery.state = DeliveryState.OPENING
        delivery.head_sha = remote.sha
        delivery.title = title
        delivery.body = body
        self.store.update_delivery(delivery)

        prepared_response: dict[str, object] = {
            "status": "prepared",
            "head_sha": remote.sha,
            "delivery_version": delivery.version,
        }
        self.store.complete_tool_call(request.call_id, prepared_response)
        return FlowToolResponse(call_id=request.call_id, result=prepared_response)

    async def finalize_open_pr(self, flow_id: int, call_id: str) -> dict[str, object]:
        """Create the prepared PR only after INITIAL terminal validation succeeds."""

        async with self._locks.setdefault(flow_id, asyncio.Lock()):
            flow = self.store.get_flow(flow_id)
            delivery = self.store.get_delivery(flow_id)
            call = self.store.get_tool_call(call_id)
            if flow is None or flow.state in TERMINAL_STATES:
                raise FlowToolRejected(FlowToolErrorKind.CONFLICT, "Flow is not active")
            if delivery is None or delivery.base_branch is None:
                raise FlowToolRejected(
                    FlowToolErrorKind.CONFLICT, "Flow delivery is not prepared"
                )
            if call is None or call.flow_id != flow_id or call.name != "flow.open_pr":
                raise FlowToolRejected(
                    FlowToolErrorKind.CONFLICT, "flow.open_pr call is not prepared"
                )
            if delivery.state is DeliveryState.OPEN:
                if delivery.pr_number is None or delivery.url is None or delivery.head_sha is None:
                    raise FlowToolRejected(
                        FlowToolErrorKind.CONFLICT, "open Flow delivery is incomplete"
                    )
                return self._delivery_response(delivery)
            if (
                delivery.state is not DeliveryState.OPENING
                or delivery.title is None
                or delivery.body is None
                or delivery.head_sha is None
            ):
                raise FlowToolRejected(
                    FlowToolErrorKind.CONFLICT, "Flow delivery has no prepared PR intent"
                )
            installation_id = flow.installation_id
            if installation_id is None:
                raise FlowToolRejected(
                    FlowToolErrorKind.AUTHORIZATION, "Flow has no GitHub installation"
                )

            pull_request = await self.github.find_pull_request_by_head(
                installation_id, flow.repo, delivery.branch
            )
            if pull_request is None:
                if delivery.pr_number is not None:
                    raise FlowToolRejected(
                        FlowToolErrorKind.NOT_FOUND,
                        "persisted pull request is missing from GitHub",
                    )
                pull_request = await self._create_or_find(
                    installation_id,
                    flow.repo,
                    delivery.branch,
                    delivery.base_branch,
                    delivery.title,
                    delivery.body,
                )
            self._validate_pull_request(
                pull_request,
                delivery.branch,
                delivery.base_branch,
                delivery.head_sha,
            )
            await self.github.set_labels(
                installation_id,
                flow.repo,
                pull_request.number,
                [self.settings.in_review_label],
            )
            await self.github.request_reviewers(
                installation_id, flow.repo, pull_request.number, self.settings.reviewers
            )

            delivery.state = DeliveryState.OPEN
            delivery.pr_number = pull_request.number
            delivery.url = f"https://github.com/{flow.repo}/pull/{pull_request.number}"
            delivery.version += 1
            self.store.update_delivery(delivery)
            try:
                self.plugins.hook.on_pr_opened(flow=flow, pr_number=pull_request.number)
            except Exception as exc:  # noqa: BLE001
                logger.warning("plugin hook on_pr_opened failed: {}", exc)
            return self._delivery_response(delivery)

    async def _create_or_find(
        self,
        installation_id: int,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestSnapshot:
        try:
            return await self.github.open_pull_request(
                installation_id, repo, head, base, title, body
            )
        except GitHubAdapterError as exc:
            if exc.kind not in {GitHubErrorKind.CONFLICT, GitHubErrorKind.VALIDATION}:
                raise
            existing = await self.github.find_pull_request_by_head(installation_id, repo, head)
            if existing is None:
                raise
            return existing

    @staticmethod
    def _validate_pull_request(
        pull_request: PullRequestSnapshot, branch: str, base: str, head_sha: str
    ) -> None:
        if pull_request.state.casefold() != "open":
            raise FlowToolRejected(
                FlowToolErrorKind.CONFLICT,
                f"pull request is {pull_request.state}, expected open",
            )
        if (
            pull_request.head_branch != branch
            or pull_request.base_branch != base
            or pull_request.head_sha != head_sha
        ):
            raise FlowToolRejected(
                FlowToolErrorKind.CONFLICT,
                "pull request head, base, or SHA conflicts with Flow delivery",
            )

    @staticmethod
    def _delivery_response(delivery: FlowDelivery) -> dict[str, object]:
        return {
            "pr_number": delivery.pr_number,
            "url": delivery.url,
            "head_sha": delivery.head_sha,
            "delivery_version": delivery.version,
        }

    @staticmethod
    def _closing_body(body: str, issue_number: int) -> str:
        marker = f"Closes #{issue_number}"
        return body if marker.casefold() in body.casefold() else f"{marker}\n\n{body}".rstrip()

    @staticmethod
    def _github_error(
        request: FlowToolRequest, error: GitHubAdapterError
    ) -> FlowToolResponse:
        kinds = {
            GitHubErrorKind.AUTHENTICATION: FlowToolErrorKind.AUTHORIZATION,
            GitHubErrorKind.PERMISSION: FlowToolErrorKind.AUTHORIZATION,
            GitHubErrorKind.NOT_FOUND: FlowToolErrorKind.NOT_FOUND,
            GitHubErrorKind.CONFLICT: FlowToolErrorKind.CONFLICT,
            GitHubErrorKind.VALIDATION: FlowToolErrorKind.INVALID_REQUEST,
            GitHubErrorKind.RATE_LIMITED: FlowToolErrorKind.RATE_LIMITED,
            GitHubErrorKind.TRANSIENT: FlowToolErrorKind.TRANSIENT,
            GitHubErrorKind.UNEXPECTED_RESPONSE: FlowToolErrorKind.UPSTREAM,
        }
        return FlowToolService._error(
            request,
            kinds[error.kind],
            str(error),
            retryable=error.retryable,
            retry_after=error.retry_after,
        )

    @staticmethod
    def _error(
        request: FlowToolRequest,
        kind: FlowToolErrorKind,
        detail: str,
        *,
        retryable: bool = False,
        retry_after: str | None = None,
    ) -> FlowToolResponse:
        return FlowToolResponse(
            call_id=request.call_id,
            error=detail,
            error_kind=kind,
            retryable=retryable,
            retry_after=retry_after,
        )
