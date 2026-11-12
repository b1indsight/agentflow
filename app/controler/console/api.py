"""Owner-only console HTTP; all blocking database units run outside the event loop."""

import asyncio
import base64
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.contracts.console import (
    ActivityPage,
    ArchiveRequest,
    ConsoleFailure,
    ContentPage,
    CreateFlowRequest,
    FlowDetail,
    FlowPage,
    FlowTarget,
    FlowTargetPage,
    InferenceNoticePage,
    JobPage,
    Phase,
    RepositoryPage,
    ResumeRequest,
    RetryRequest,
)
from app.controler.runtime import ServerRuntime
from app.controler.workflow.service import ControlService, RetryRejected
from app.github import GitHubError, GitHubErrorKind
from app.persistence.activity import ActivityStore
from app.persistence.console import ConsoleStore
from app.persistence.flows import FlowStore
from app.persistence.inference_notices import InferenceNoticeStore

router = APIRouter(prefix="/api")
FlowId = Annotated[int, Path(gt=0)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=4096)]


class _TargetCursor(BaseModel):
    """Resume one bounded inventory page, anchored by installation identity."""

    model_config = ConfigDict(extra="forbid", strict=True)
    installation_page: int = Field(default=1, gt=0)
    installation_id: int | None = Field(default=None, gt=0)
    repository_page: int = Field(default=1, gt=0)

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()


def _github_failure(error: GitHubError) -> ConsoleFailure:
    statuses = {
        GitHubErrorKind.AUTHENTICATION: 403, GitHubErrorKind.PERMISSION: 403,
        GitHubErrorKind.NOT_FOUND: 404, GitHubErrorKind.VALIDATION: 422,
        GitHubErrorKind.CONFLICT: 409, GitHubErrorKind.RATE_LIMITED: 429,
    }
    return ConsoleFailure(f"github_{error.kind.value}", statuses.get(error.kind, 503))


@router.get("/flow-targets", response_model=FlowTargetPage)
async def flow_targets(request: Request, cursor: Cursor = None) -> FlowTargetPage:
    """Read at most one installation page and one repository page; no GitHub writes."""
    runtime: ServerRuntime = request.app.state.runtime
    try:
        position = _TargetCursor.model_validate_json(
            base64.b64decode(cursor, altchars=b"-_", validate=True)
        ) if cursor else _TargetCursor()
    except ValueError as error:
        raise ConsoleFailure("cursor_invalid") from error
    following: _TargetCursor | None = None
    items: list[FlowTarget] = []
    try:
        installations = await runtime.github.fetch_installations(page=position.installation_page)
        identities = [item.installation_id for item in installations.items]
        if position.installation_id is not None and position.installation_id not in identities:
            raise ConsoleFailure("cursor_invalid")
        if installations.items:
            index = identities.index(position.installation_id) if position.installation_id else 0
            installation = installations.items[index]
            repositories = await installation.fetch_repositories(page=position.repository_page)
            items = [FlowTarget(repo=repo.full_name, installation_id=installation.installation_id)
                     for repo in repositories.items]
            if repositories.next_cursor is not None:
                following = _TargetCursor(
                    installation_page=position.installation_page,
                    installation_id=installation.installation_id,
                    repository_page=int(repositories.next_cursor),
                )
            elif index + 1 < len(identities):
                following = _TargetCursor(installation_page=position.installation_page,
                                          installation_id=identities[index + 1])
        if following is None and installations.next_cursor is not None:
            following = _TargetCursor(installation_page=int(installations.next_cursor))
    except GitHubError as error:
        raise _github_failure(error) from error
    return FlowTargetPage(items=items, next_cursor=following.encode() if following else None,
                          has_more=following is not None)


@router.get("/flows", response_model=FlowPage)
async def flows(
    request: Request,
    q: str = Query(default="", max_length=2000),
    phase: Phase | None = None,
    cursor: Cursor = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> FlowPage:
    runtime: ServerRuntime = request.app.state.runtime
    return await runtime.console.db(
        partial(ConsoleStore(runtime.database).flows, q=q, phase=phase, cursor=cursor, limit=limit)
    )


@router.get("/repos", response_model=RepositoryPage)
async def repos(
    request: Request, cursor: Cursor = None, limit: int = Query(default=100, ge=1, le=100)
) -> RepositoryPage:
    runtime: ServerRuntime = request.app.state.runtime
    return await runtime.console.db(
        partial(ConsoleStore(runtime.database).repos, cursor=cursor, limit=limit)
    )


@router.get("/flows/{flow_id}", response_model=FlowDetail)
async def detail(request: Request, flow_id: FlowId) -> FlowDetail:
    runtime: ServerRuntime = request.app.state.runtime
    store = ConsoleStore(runtime.database)
    await runtime.console.db(partial(store.request_sync, flow_id))
    return await runtime.console.db(partial(store.detail, flow_id))


def _require_same_origin_json(request: Request) -> None:
    """Protect both console commands, including behind TLS and Host-rewriting proxies."""
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    # Browser-controlled Fetch Metadata survives proxy TLS termination and Host rewriting.
    # Fall back to Origin comparison when there is no explicit same-origin signal.
    if fetch_site not in (None, "same-origin", "none") or (
        fetch_site != "same-origin"
        and origin is not None
        and origin != str(request.base_url).rstrip("/")
    ):
        raise ConsoleFailure("cross_origin_request", 403)
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise ConsoleFailure("json_required", 415)


@router.post("/flows", response_model=FlowDetail, status_code=201)
async def create_flow(request: Request, body: CreateFlowRequest) -> FlowDetail:
    """Validate current GitHub access before atomically persisting input and INITIAL."""
    _require_same_origin_json(request)
    runtime: ServerRuntime = request.app.state.runtime
    try:
        # Keep these read-only checks aligned with the admin CLI's flow create command.
        repository = await runtime.github.installation(body.installation_id).repository(body.repo).refetch()
        if repository.archived:
            raise ConsoleFailure("repository_archived")
        if body.issue_number is not None:
            await repository.fetch_issue(body.issue_number)
    except GitHubError as error:
        raise _github_failure(error) from error
    loop = asyncio.get_running_loop()

    def notify_jobs() -> None:
        loop.call_soon_threadsafe(runtime.gateway.wake_all)

    def command() -> FlowDetail:
        flow = ControlService(
            database=runtime.database,
            jobs_available=notify_jobs,
        ).create_flow(repository.full_name, body.issue_number, body.installation_id,
                      task_content=body.task_content)
        if flow is None:
            raise ConsoleFailure("flow_conflict")
        return ConsoleStore(runtime.database).detail(flow.id)

    return await runtime.console.db(command)


@router.post("/flows/{flow_id}/archive", response_model=FlowDetail)
async def archive(request: Request, flow_id: FlowId, body: ArchiveRequest) -> FlowDetail:
    """Request business finalization; a card cannot bypass stop or cleanup guards."""
    _require_same_origin_json(request)
    runtime: ServerRuntime = request.app.state.runtime
    loop = asyncio.get_running_loop()

    def notify_jobs() -> None:
        loop.call_soon_threadsafe(runtime.gateway.wake_all)

    def command() -> FlowDetail:
        flow = FlowStore(runtime.database).get_flow(flow_id)
        if flow is None:
            raise ConsoleFailure("flow_not_found", 404)
        try:
            ControlService(database=runtime.database, jobs_available=notify_jobs).finish(
                flow,
                reason=body.reason,
                summary=body.summary,
                expected_round_no=body.expected_round_no,
            )
        except ValueError as error:
            raise ConsoleFailure("archive_rejected") from error
        return ConsoleStore(runtime.database).detail(flow_id)

    return await runtime.console.db(command)


@router.post("/flows/{flow_id}/resume", response_model=FlowDetail)
async def resume(request: Request, flow_id: FlowId, body: ResumeRequest) -> FlowDetail:
    """Explicit restoration is separate from retry and historical correction."""
    _require_same_origin_json(request)
    runtime: ServerRuntime = request.app.state.runtime
    loop = asyncio.get_running_loop()

    def notify_jobs() -> None:
        loop.call_soon_threadsafe(runtime.gateway.wake_all)

    def command() -> FlowDetail:
        flow = FlowStore(runtime.database).get_flow(flow_id)
        if flow is None:
            raise ConsoleFailure("flow_not_found", 404)
        try:
            ControlService(database=runtime.database, jobs_available=notify_jobs).resume(
                flow, expected_round_no=body.expected_round_no
            )
        except ValueError as error:
            raise ConsoleFailure("resume_rejected") from error
        return ConsoleStore(runtime.database).detail(flow_id)

    return await runtime.console.db(command)


@router.post("/flows/{flow_id}/retry", response_model=FlowDetail)
async def retry(request: Request, flow_id: FlowId, body: RetryRequest) -> FlowDetail:
    """Request canonical retry, reporting refusal or no change without claiming execution."""
    _require_same_origin_json(request)
    runtime: ServerRuntime = request.app.state.runtime
    loop = asyncio.get_running_loop()

    def notify_jobs() -> None:
        # Schedule on the ASGI loop after commit, including after client disconnect.
        loop.call_soon_threadsafe(runtime.gateway.wake_all)

    def command() -> FlowDetail:
        flow = FlowStore(runtime.database).get_flow(flow_id)
        if flow is None:
            raise ConsoleFailure("flow_not_found", 404)
        service = ControlService(
            database=runtime.database,
            jobs_available=notify_jobs,
        )
        try:
            accepted = service.retry(flow)
        except RetryRejected as error:
            raise ConsoleFailure(
                error.code, 404 if error.code == "flow_not_found" else 409
            ) from error
        if not accepted:
            raise ConsoleFailure("retry_no_change")
        return ConsoleStore(runtime.database).detail(flow_id)

    return await runtime.console.db(command)


@router.get("/flows/{flow_id}/jobs", response_model=JobPage)
async def jobs(
    request: Request,
    flow_id: FlowId,
    cursor: Cursor = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> JobPage:
    runtime: ServerRuntime = request.app.state.runtime
    return await runtime.console.db(
        partial(ConsoleStore(runtime.database).jobs, flow_id, cursor=cursor, limit=limit)
    )


@router.get("/flows/{flow_id}/inference-notices", response_model=InferenceNoticePage)
async def inference_notices(
    request: Request,
    flow_id: FlowId,
    cursor: Cursor = None,
    snapshot: int | None = Query(default=None, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
) -> InferenceNoticePage:
    runtime: ServerRuntime = request.app.state.runtime
    return await runtime.console.db(partial(
        InferenceNoticeStore(runtime.database).page, flow_id,
        cursor=cursor, snapshot=snapshot, limit=limit,
    ))


@router.get("/flows/{flow_id}/activity", response_model=ActivityPage)
async def activity(
    request: Request,
    flow_id: FlowId,
    cursor: Cursor = None,
    snapshot: int | None = Query(default=None, ge=0),
    since: int | None = Query(default=None, ge=0),
    refresh: bool = False,
    parent: str | None = Query(default=None, min_length=1, max_length=512),
    limit: int = Query(default=20, ge=1, le=50),
) -> ActivityPage:
    runtime: ServerRuntime = request.app.state.runtime
    if refresh:
        await runtime.console.db(
            partial(ConsoleStore(runtime.database).request_sync, flow_id, active_only=False)
        )
    return await runtime.console.db(
        partial(
            ActivityStore(runtime.database).page,
            flow_id,
            cursor=cursor,
            snapshot=snapshot,
            since=since,
            parent=parent,
            limit=limit,
        )
    )


@router.get("/flows/{flow_id}/activity/versions", response_model=ActivityPage)
async def versions(
    request: Request,
    flow_id: FlowId,
    key: str = Query(min_length=1, max_length=512),
    cursor: Cursor = None,
    snapshot: int | None = Query(default=None, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
) -> ActivityPage:
    runtime: ServerRuntime = request.app.state.runtime
    return await runtime.console.db(
        partial(
            ActivityStore(runtime.database).versions,
            flow_id,
            key,
            cursor=cursor,
            snapshot=snapshot,
            limit=limit,
        )
    )


@router.get("/flows/{flow_id}/activity/content", response_model=ContentPage)
async def content(
    request: Request,
    flow_id: FlowId,
    key: str = Query(min_length=1, max_length=512),
    version: int = Query(gt=0),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=16384, ge=1, le=16384),
    compare: bool = False,
) -> ContentPage:
    """Page archived body text, or return a complete patch with compare=true (offset must be zero)."""
    runtime: ServerRuntime = request.app.state.runtime
    return await runtime.console.db(
        partial(
            ActivityStore(runtime.database).content,
            flow_id,
            key,
            version,
            offset,
            limit,
            compare=compare,
        )
    )
