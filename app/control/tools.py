"""Fenced, idempotent Flow tools executed by the control plane."""

from __future__ import annotations

import asyncio

from app.contracts.protocol import FlowToolErrorKind, FlowToolRequest, FlowToolResponse
from app.domain.delivery_models import DeliveryReceipt, OpenedDelivery, PreparedDelivery
from app.domain.github_models import GitHubAdapterError, GitHubErrorKind
from app.domain.models import TERMINAL_STATES, DeliveryState, FlowDelivery
from app.domain.ports import GitHubClient
from app.persistence.store import Store


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
    ) -> None:
        self.store = store
        self.github = github
        self._locks: dict[int, asyncio.Lock] = {}

    async def execute(self, request: FlowToolRequest, *, worker_id: str) -> FlowToolResponse:
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

    async def _open_pr(self, request: FlowToolRequest, worker_id: str) -> FlowToolResponse:
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
        if (
            execution is None
            or execution.released_at is not None
            or execution.worker_id != worker_id
        ):
            raise FlowToolRejected(
                FlowToolErrorKind.STALE_ASSIGNMENT,
                "Flow execution binding does not match worker",
            )
        if delivery is None or delivery.base_branch is None:
            raise FlowToolRejected(
                FlowToolErrorKind.CONFLICT,
                "Flow delivery is not prepared",
            )
        if delivery.state in {DeliveryState.CLOSED, DeliveryState.MERGED}:
            raise FlowToolRejected(FlowToolErrorKind.CONFLICT, "PR closed or merged; flow.open_pr is retired")
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
        if call.response is not None:
            return FlowToolResponse(call_id=request.call_id, result=call.response)

        installation_id = flow.installation_id
        if installation_id is None:
            raise FlowToolRejected(
                FlowToolErrorKind.AUTHORIZATION,
                "Flow has no GitHub installation",
            )
        remote = await self.github.get_ref(installation_id, flow.repo, delivery.branch, retry_transient=True)
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
        # The remote read may overlap cancellation or reconnect. Recheck the fence and save
        # both pieces of prepared intent on one connection before acknowledging the tool call.
        with self.store.transaction() as tx:
            current_flow = tx.lock_workflow(flow.id)
            if (
                current_flow is None
                or current_flow.state in TERMINAL_STATES
                or current_flow.current_attempt_id != request.attempt_id
                or not tx.assignment_active(
                    request.job_id,
                    worker_id,
                    request.assignment_token,
                    request.connection_epoch,
                )
            ):
                raise FlowToolRejected(
                    FlowToolErrorKind.STALE_ASSIGNMENT,
                    "Flow changed during remote verification",
                )
            current_delivery = tx.get_delivery(flow.id)
            if current_delivery != delivery:
                raise FlowToolRejected(
                    FlowToolErrorKind.CONFLICT,
                    "Flow delivery changed during remote verification",
                )
            if delivery.state is DeliveryState.OPEN:
                if delivery.head_sha != remote.sha:
                    raise FlowToolRejected(
                        FlowToolErrorKind.CONFLICT,
                        "open Flow delivery head does not match the remote branch",
                    )
                prepared_response: DeliveryReceipt = self._delivery_response(delivery)
            else:
                delivery.state = DeliveryState.OPENING
                delivery.head_sha = remote.sha
                delivery.title = title
                delivery.body = body
                tx.update_delivery(delivery)
                prepared_response = PreparedDelivery(
                    status="prepared", head_sha=remote.sha, delivery_version=delivery.version,
                )
            tx.complete_tool_call(request.call_id, prepared_response)
        return FlowToolResponse(call_id=request.call_id, result=prepared_response)

    @staticmethod
    def _delivery_response(delivery: FlowDelivery) -> OpenedDelivery:
        assert delivery.pr_number is not None and delivery.url is not None
        assert delivery.head_sha is not None
        return OpenedDelivery(pr_number=delivery.pr_number, url=delivery.url,
                              head_sha=delivery.head_sha, delivery_version=delivery.version)

    @staticmethod
    def _closing_body(body: str, issue_number: int) -> str:
        marker = f"Closes #{issue_number}"
        return body if marker.casefold() in body.casefold() else f"{marker}\n\n{body}".rstrip()

    @staticmethod
    def _github_error(request: FlowToolRequest, error: GitHubAdapterError) -> FlowToolResponse:
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
